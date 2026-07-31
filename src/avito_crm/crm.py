from __future__ import annotations

import logging
import re
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from avito_crm.config import Settings
from avito_crm.errors import ConfigurationError, CrmError
from avito_crm.models import CrmDestination, CrmWriteResult, ItemStatus
from avito_crm.phone import canonical_avito_url, normalize_phone

LOGGER = logging.getLogger(__name__)
COMMENT_INVALID_LEAD_RETRY_DELAYS = (1.0, 2.0, 4.0)


class RateLimiter:
    """Process-local limiter kept below LPTracker's documented 3 req/s."""

    def __init__(self, requests_per_second: float = 2.0) -> None:
        self.interval = 1.0 / requests_per_second
        self._last_request = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            remaining = self.interval - (now - self._last_request)
            if remaining > 0:
                time.sleep(remaining)
            self._last_request = time.monotonic()


class LpTrackerClient:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._owns_client = client is None
        self.client = client or httpx.Client(
            base_url=settings.lptracker_base_url,
            timeout=httpx.Timeout(30.0, connect=15.0),
            headers={"Content-Type": "application/json"},
        )
        self.token = ""
        self.rate_limiter = RateLimiter(2.0)

    def __enter__(self) -> LpTrackerClient:
        return self

    def __exit__(self, *_args: object) -> None:
        if self._owns_client:
            self.client.close()

    def authenticate(self) -> None:
        self.settings.require_crm_credentials()
        payload = self._request(
            "POST",
            "/login",
            auth=False,
            json={
                "login": self.settings.lptracker_login,
                "password": self.settings.lptracker_password,
                "service": self.settings.lptracker_service_name,
                "version": "1.0",
            },
        )
        token = str(payload.get("token", ""))
        if not token:
            raise CrmError("LPTracker не вернул токен авторизации")
        self.token = token

    def list_projects(self) -> list[dict[str, Any]]:
        return _ensure_list(self._request("GET", "/projects"), "список проектов")

    def list_custom_fields(self, project_id: int) -> list[dict[str, Any]]:
        result = self._request("GET", f"/project/{project_id}/customs")
        return _ensure_list(result, "список полей проекта")

    def resolve_destination(self) -> CrmDestination:
        self.settings.require_crm_destination()
        projects = self.list_projects()
        project = self._select_project(projects)
        project_id = int(project["id"])
        fields = self.list_custom_fields(project_id)
        wanted = _normalized_name(self.settings.lptracker_field_name)
        matches = [
            field for field in fields if _normalized_name(str(field.get("name", ""))) == wanted
        ]
        if len(matches) != 1:
            available = ", ".join(str(field.get("name", "")) for field in fields[:25])
            if not matches:
                raise ConfigurationError(
                    f"Поле {self.settings.lptracker_field_name!r} не найдено. Поля: {available}"
                )
            raise ConfigurationError(
                f"Найдено несколько полей {self.settings.lptracker_field_name!r}; "
                "укажите ID проекта точнее"
            )
        field = matches[0]
        field_type = str(field.get("type", ""))
        value: Any = self.settings.lptracker_field_value
        if field_type == "cats":
            categories = field.get("categories") or []
            if categories:
                category_names = [
                    str(category.get("name", "")) if isinstance(category, dict) else str(category)
                    for category in categories
                ]
                category_map = {_normalized_name(name): name for name in category_names}
                target = category_map.get(_normalized_name(self.settings.lptracker_field_value))
                if not target:
                    raise ConfigurationError(
                        f"В поле {self.settings.lptracker_field_name!r} нет значения "
                        f"{self.settings.lptracker_field_value!r}. Доступно: "
                        f"{', '.join(category_names)}"
                    )
                value = [target] if _truthy(field.get("is_multi_select")) else target
            else:
                LOGGER.info(
                    "LPTracker не вернул варианты поля-категории в списке проекта; "
                    "используем настроенное точное значение"
                )
        return CrmDestination(
            project_id=project_id,
            project_name=str(project.get("name", "")),
            field_id=int(field["id"]),
            field_name=str(field.get("name", "")),
            field_type=field_type,
            field_value=value,
        )

    def _select_project(self, projects: list[dict[str, Any]]) -> dict[str, Any]:
        if self.settings.lptracker_project_id:
            matches = [
                project
                for project in projects
                if int(project.get("id", 0)) == self.settings.lptracker_project_id
            ]
        else:
            wanted = _normalized_name(self.settings.lptracker_project_name)
            matches = [
                project
                for project in projects
                if _normalized_name(str(project.get("name", ""))) == wanted
            ]
        if len(matches) == 1:
            return matches[0]
        available = ", ".join(
            f"{project.get('id')}:{project.get('name')}" for project in projects[:25]
        )
        if not matches:
            raise ConfigurationError(f"Проект LPTracker не найден. Доступно: {available}")
        raise ConfigurationError("Название проекта неоднозначно; задайте LPTRACKER_PROJECT_ID")

    def search_contacts(self, project_id: int, phone: str) -> list[dict[str, Any]]:
        normalized = normalize_phone(phone)
        if not normalized:
            raise CrmError("Номер перед поиском CRM имеет неверный формат")
        result = self._request(
            "GET",
            "/contact/search",
            params={"project_id": project_id, "phone": normalized.lstrip("+")},
        )
        return _ensure_list(result, "результат поиска контакта")

    def contact_leads(self, contact_id: str | int) -> list[dict[str, Any]]:
        result = self._request("GET", f"/contact/{contact_id}/leads")
        return _ensure_list(result, "список лидов контакта")

    def list_funnel_steps(self, project_id: int) -> list[dict[str, Any]]:
        result = self._request("GET", f"/project/{project_id}/funnel")
        if isinstance(result, dict):
            for key in ("funnels", "stages", "items"):
                if isinstance(result.get(key), list):
                    result = result[key]
                    break
        return _ensure_list(result, "список шагов воронки")

    def resolve_funnel_step_id(self, project_id: int, name: str) -> int:
        wanted = _normalized_name(name)
        steps = self.list_funnel_steps(project_id)
        matches = [step for step in steps if _normalized_name(str(step.get("name", ""))) == wanted]
        if len(matches) != 1:
            available = ", ".join(str(step.get("name", "")) for step in steps[:30])
            if not matches:
                raise ConfigurationError(f"Шаг воронки {name!r} не найден. Доступно: {available}")
            raise ConfigurationError(f"Найдено несколько шагов воронки {name!r}")
        try:
            return int(matches[0]["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CrmError(f"Шаг воронки {name!r} не содержит корректный ID") from exc

    def get_lead(self, lead_id: str | int) -> dict[str, Any]:
        result = self._request("GET", f"/lead/{lead_id}")
        if not isinstance(result, dict):
            raise CrmError("LPTracker вернул неожиданные данные лида")
        return result

    def get_lead_stage_name(
        self,
        lead_id: str | int,
        *,
        project_id: int,
        funnel_steps: list[dict[str, Any]] | None = None,
        lead: dict[str, Any] | None = None,
    ) -> str:
        lead_data = lead if lead is not None else self.get_lead(lead_id)
        steps = funnel_steps if funnel_steps is not None else self.list_funnel_steps(project_id)
        stage_map = {
            str(step.get("id", "")): str(step.get("name", "")).strip()
            for step in steps
            if step.get("id") is not None
        }
        stage_id, direct_name = _extract_funnel_stage(lead_data)
        if direct_name:
            return direct_name
        if stage_id and stage_id in stage_map:
            return stage_map[stage_id]
        return ""

    def first_call_delay_seconds(self, lead: dict[str, Any]) -> float | None:
        """Return first successful call delay, or None when either date is unavailable."""
        try:
            local_timezone = ZoneInfo(self.settings.lptracker_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ConfigurationError(
                f"Неизвестный часовой пояс LPTRACKER_TIMEZONE={self.settings.lptracker_timezone!r}"
            ) from exc
        created_at = _extract_lead_created_at(lead, local_timezone)
        first_call_at = _extract_first_call_at(lead, local_timezone)
        if created_at is None or first_call_at is None:
            return None
        return (first_call_at - created_at).total_seconds()

    def delete_lead(self, lead_id: str | int) -> None:
        """Delete one lead; a failed or ambiguous API response raises CrmError."""
        normalized_id = str(lead_id).strip()
        if not normalized_id:
            raise CrmError("Нельзя удалить лид без ID")
        self._request("DELETE", f"/lead/{normalized_id}")

    def find_lead_for_listing(
        self,
        project_id: int,
        phone: str,
        listing_url: str,
        *,
        repeat: bool | None = None,
    ) -> dict[str, Any] | None:
        normalized = normalize_phone(phone)
        if not normalized:
            return None
        base_name = f"Авито — {_listing_id(canonical_avito_url(listing_url))}"
        lead_names = (
            {f"{base_name} — повторный лид"}
            if repeat is True
            else {base_name}
            if repeat is False
            else {base_name, f"{base_name} — повторный лид"}
        )
        for contact in self.search_contacts(project_id, normalized):
            contact_id = contact.get("id")
            if contact_id is None:
                continue
            for lead in self.contact_leads(contact_id):
                if str(lead.get("name", "")).strip() in lead_names:
                    return lead
        return None

    def add_listing_comment(self, lead_id: str | int, listing_url: str) -> None:
        canonical_url = canonical_avito_url(listing_url)
        for attempt in range(len(COMMENT_INVALID_LEAD_RETRY_DELAYS) + 1):
            try:
                self._request("POST", f"/lead/{lead_id}/comment", json={"text": canonical_url})
                return
            except CrmError as exc:
                if "invalid lead id" not in str(exc).casefold() or attempt >= len(
                    COMMENT_INVALID_LEAD_RETRY_DELAYS
                ):
                    raise
                delay = COMMENT_INVALID_LEAD_RETRY_DELAYS[attempt]
                LOGGER.info(
                    "LPTracker ещё не видит лид %s для комментария; повтор через %.0f сек.",
                    lead_id,
                    delay,
                )
                time.sleep(delay)

    def create_for_phone(
        self,
        phone: str,
        listing_url: str,
        destination: CrmDestination,
        *,
        force_create: bool = False,
        funnel_id: int | None = None,
        repeat: bool = False,
    ) -> CrmWriteResult:
        normalized = normalize_phone(phone)
        if not normalized:
            raise CrmError("Нельзя создать лид: номер имеет неверный формат")
        canonical_url = canonical_avito_url(listing_url)
        listing_id = _listing_id(canonical_url)
        lead_name = f"Авито — {listing_id}"
        if repeat:
            lead_name += " — повторный лид"
        contacts = self.search_contacts(destination.project_id, normalized)
        contact_ids = [
            str(contact.get("id", "")).strip()
            for contact in contacts
            if str(contact.get("id", "")).strip()
        ]

        # Crash-safe recovery: the API may have created the lead while the process
        # stopped before its ID reached Google Sheets.  The deterministic listing
        # name lets the next run recover that exact lead instead of creating another.
        for existing_contact_id in contact_ids:
            existing = self._find_existing_lead(
                existing_contact_id,
                lead_name,
                destination.field_id,
            )
            if not existing:
                continue
            existing_lead_id = str(existing.get("id", "")).strip()
            if not existing_lead_id:
                continue
            detail = "Ранее созданный лид для объявления восстановлен"
            try:
                self.add_listing_comment(existing_lead_id, canonical_url)
            except CrmError as exc:
                detail += f"; ссылку в комментарий записать не удалось: {exc}"
                LOGGER.warning(
                    "Не удалось восстановить комментарий лида %s: %s",
                    existing_lead_id,
                    exc,
                )
            return CrmWriteResult(
                status=ItemStatus.DONE,
                contact_id=existing_contact_id,
                lead_id=existing_lead_id,
                detail=detail,
                created=False,
            )

        if contacts and self.settings.duplicate_policy == "skip" and not force_create:
            return CrmWriteResult(
                status=ItemStatus.DUPLICATE,
                contact_id=contact_ids[0] if contact_ids else None,
                detail="Контакт с таким номером уже существует; новый лид не создан",
                created=False,
            )
        payload: dict[str, Any] = {
            "name": lead_name,
            "callback": False,
            "custom": {str(destination.field_id): destination.field_value},
            "view": {"source": "Avito", "campaign": "Avito CRM Pipeline"},
        }
        if funnel_id is not None:
            payload["funnel"] = int(funnel_id)
        contact_id: str | None = None
        if contacts:
            if not contact_ids:
                raise CrmError("LPTracker вернул контакт без ID")
            contact_id = contact_ids[0]
            payload["contact_id"] = contact_id
        else:
            payload["contact"] = {
                "project_id": destination.project_id,
                "name": lead_name,
                "site": canonical_url,
                "details": [{"type": "phone", "data": normalized}],
            }

        result = self._request("POST", "/lead", json=payload)
        if not isinstance(result, dict) or not result.get("id"):
            raise CrmError("LPTracker создал лид, но не вернул его ID")
        detail = "Лид создан"
        try:
            self.add_listing_comment(result["id"], canonical_url)
        except CrmError as exc:
            detail = f"Лид создан; ссылку в комментарий записать не удалось: {exc}"
            LOGGER.warning("Лид %s создан без комментария со ссылкой: %s", result["id"], exc)
        returned_contact = result.get("contact_id")
        if not returned_contact and isinstance(result.get("contact"), dict):
            returned_contact = result["contact"].get("id")
        return CrmWriteResult(
            status=ItemStatus.DONE,
            contact_id=str(returned_contact or contact_id or "") or None,
            lead_id=str(result["id"]),
            detail=detail,
        )

    def _find_existing_lead(
        self, contact_id: str, lead_name: str, field_id: int
    ) -> dict[str, Any] | None:
        for lead in self.contact_leads(contact_id):
            if _normalized_name(str(lead.get("name", ""))) != _normalized_name(lead_name):
                continue
            custom = lead.get("custom") or []
            # Some LPTracker list responses omit custom fields even though the
            # detailed lead contains them. The listing-derived name is unique to
            # this worker, so an exact name is already a safe recovery key.
            if not custom:
                return lead
            if isinstance(custom, dict):
                if str(field_id) in {str(key) for key in custom}:
                    return lead
                custom = [custom] if "id" in custom else list(custom.values())
            if any(
                str(field.get("id", "")) == str(field_id)
                for field in custom
                if isinstance(field, dict)
            ):
                return lead
        return None

    def _request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if auth and not self.token:
            self.authenticate()
        reauthenticated = False
        last_error: Exception | None = None
        for attempt in range(4):
            headers = {"token": self.token} if auth else {}
            self.rate_limiter.wait()
            try:
                response = self.client.request(
                    method, path, headers=headers, json=json, params=params
                )
            except httpx.RequestError as exc:
                last_error = exc
                if attempt == 3:
                    break
                time.sleep(min(8.0, 2**attempt))
                continue
            if response.status_code in {429, 500, 502, 503, 504}:
                if attempt == 3:
                    raise CrmError(f"LPTracker временно недоступен (HTTP {response.status_code})")
                time.sleep(min(8.0, 2**attempt))
                continue
            try:
                payload = response.json()
            except ValueError as exc:
                raise CrmError(f"LPTracker вернул не-JSON (HTTP {response.status_code})") from exc
            codes = _error_codes(payload) if isinstance(payload, dict) else set()
            if auth and not reauthenticated and (response.status_code == 401 or 401 in codes):
                self.token = ""
                self.authenticate()
                reauthenticated = True
                continue
            if response.status_code >= 400:
                raise CrmError(_error_message(payload, response.status_code))
            if not isinstance(payload, dict):
                raise CrmError("LPTracker вернул неожиданный формат ответа")
            if payload.get("status") == "success":
                return payload.get("result")
            if 503 in codes and attempt < 3:
                time.sleep(min(8.0, 2**attempt))
                continue
            raise CrmError(_error_message(payload, response.status_code))
        raise CrmError(f"Не удалось связаться с LPTracker: {last_error}")


def _ensure_list(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise CrmError(f"LPTracker вернул неожиданный {label}")
    return [item for item in value if isinstance(item, dict)]


def _normalized_name(value: str) -> str:
    return " ".join(value.split()).casefold()


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _listing_id(url: str) -> str:
    numbers = re.findall(r"\d{5,}", url)
    return numbers[-1] if numbers else url.rstrip("/").rsplit("/", 1)[-1][:80]


def _extract_funnel_stage(lead: dict[str, Any]) -> tuple[str, str]:
    """Return (stage id, direct stage name) across known LPTracker response shapes."""
    for key in ("funnel", "stage", "funnel_stage"):
        value = lead.get(key)
        if isinstance(value, dict):
            stage_id = str(value.get("id", value.get("value", "")) or "").strip()
            name = str(value.get("name", value.get("title", "")) or "").strip()
            if stage_id or name:
                return stage_id, name
        elif value not in (None, ""):
            return str(value).strip(), ""
    for key in ("funnel_id", "stage_id"):
        if lead.get(key) not in (None, ""):
            return str(lead[key]).strip(), ""
    custom = lead.get("custom") or []
    if isinstance(custom, dict):
        custom = list(custom.values())
    for field in custom:
        if not isinstance(field, dict):
            continue
        if str(field.get("type", "")).casefold() not in {"funnel", "conv_funnel"}:
            continue
        value = field.get("value")
        if isinstance(value, dict):
            return (
                str(value.get("id", "") or "").strip(),
                str(value.get("name", "") or "").strip(),
            )
        if value not in (None, ""):
            return str(value).strip(), ""
    return "", ""


def _extract_lead_created_at(lead: dict[str, Any], local_timezone: ZoneInfo) -> datetime | None:
    for key in ("created_at", "lead_date", "created"):
        parsed = _parse_crm_datetime(lead.get(key), local_timezone)
        if parsed is not None:
            return parsed
    created_names = {
        "дата заявки",
        "дата лида",
        "дата создания",
        "дата создания лида",
    }
    for field in _custom_fields(lead):
        if _normalized_name(str(field.get("name", ""))) not in created_names:
            continue
        parsed = _parse_crm_datetime(field.get("value"), local_timezone)
        if parsed is not None:
            return parsed
    return None


def _extract_first_call_at(lead: dict[str, Any], local_timezone: ZoneInfo) -> datetime | None:
    records: list[Any] = []
    for key in ("calls_records", "call_records", "calls"):
        value = lead.get(key)
        if isinstance(value, list):
            records.extend(value)
    call_dates: list[datetime] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        for key in ("time", "created_at", "started_at", "date"):
            parsed = _parse_crm_datetime(record.get(key), local_timezone)
            if parsed is not None:
                call_dates.append(parsed)
                break
    return min(call_dates) if call_dates else None


def _custom_fields(lead: dict[str, Any]) -> list[dict[str, Any]]:
    custom = lead.get("custom") or []
    if isinstance(custom, dict):
        custom = [custom] if "id" in custom or "name" in custom else list(custom.values())
    return [field for field in custom if isinstance(field, dict)]


def _parse_crm_datetime(value: Any, local_timezone: ZoneInfo) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, UTC)
        except (OSError, OverflowError, ValueError):
            return None
    else:
        text = str(value).strip()
        if not text:
            return None
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            return _parse_crm_datetime(float(text), local_timezone)
        parsed = None
        with suppress(ValueError):
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed is None:
            for pattern in (
                "%d.%m.%Y %H:%M:%S",
                "%d.%m.%Y %H:%M",
                "%d.%m.%y %H:%M:%S",
                "%d.%m.%y %H:%M",
            ):
                try:
                    parsed = datetime.strptime(text, pattern)
                    break
                except ValueError:
                    continue
        if parsed is None:
            try:
                parsed = parsedate_to_datetime(text)
            except (TypeError, ValueError):
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_timezone)
    return parsed.astimezone(UTC)


def _error_codes(payload: dict[str, Any]) -> set[int]:
    codes: set[int] = set()
    for error in payload.get("errors") or []:
        if isinstance(error, dict):
            try:
                codes.add(int(error.get("code")))
            except (TypeError, ValueError):
                continue
    return codes


def _error_message(payload: Any, status_code: int) -> str:
    messages = []
    if isinstance(payload, dict):
        for error in payload.get("errors") or []:
            if isinstance(error, dict):
                code = error.get("code", status_code)
                message = str(error.get("message", "ошибка"))[:300]
                messages.append(f"{code}: {message}")
    return "LPTracker: " + ("; ".join(messages) if messages else f"HTTP {status_code}")
