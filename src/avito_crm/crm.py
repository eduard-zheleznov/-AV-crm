from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Iterable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from avito_crm.config import Settings
from avito_crm.errors import ConfigurationError, CrmError
from avito_crm.models import CrmDestination, CrmWriteResult, FirstCallSlaAssessment, ItemStatus
from avito_crm.phone import canonical_avito_url, normalize_phone

LOGGER = logging.getLogger(__name__)
COMMENT_INVALID_LEAD_RETRY_DELAYS = (1.0, 2.0, 4.0)
LPTRACKER_WEB_BASE_URL = "https://my.lptracker.ru"


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
        self.web_token = ""
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

    def list_staff(self) -> list[dict[str, Any]]:
        return _ensure_list(self._request("GET", "/staff"), "список сотрудников")

    def resolve_staff_id(self, name: str) -> int:
        wanted = _normalized_name(name)
        if not wanted:
            raise ConfigurationError("Имя владельца лида не заполнено")
        staff = self.list_staff()
        matches = [
            member for member in staff if _normalized_name(str(member.get("name", ""))) == wanted
        ]
        if len(matches) != 1:
            available = ", ".join(
                str(member.get("name", "")).strip()
                for member in staff[:25]
                if str(member.get("name", "")).strip()
            )
            if not matches:
                raise ConfigurationError(
                    f"Сотрудник {name!r} не найден. Сотрудники: {available or 'список пуст'}"
                )
            raise ConfigurationError(
                f"Найдено несколько сотрудников {name!r}; требуется уникальное имя"
            )
        try:
            staff_id = int(matches[0]["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CrmError(f"LPTracker не вернул ID сотрудника {name!r}") from exc
        if staff_id <= 0:
            raise CrmError(f"LPTracker вернул некорректный ID сотрудника {name!r}")
        return staff_id

    def list_custom_fields(self, project_id: int) -> list[dict[str, Any]]:
        result = self._request("GET", f"/project/{project_id}/customs")
        return _ensure_list(result, "список полей проекта")

    def resolve_destination(self) -> CrmDestination:
        self.settings.require_crm_destination()
        projects = self.list_projects()
        project = self._select_project(projects)
        project_id = int(project["id"])
        return self.resolve_custom_destination(
            project_id,
            self.settings.lptracker_field_name,
            self.settings.lptracker_field_value,
            project_name=str(project.get("name", "")),
        )

    def resolve_custom_destination(
        self,
        project_id: int,
        field_name: str,
        field_value: str,
        *,
        project_name: str = "",
    ) -> CrmDestination:
        fields = self.list_custom_fields(project_id)
        wanted = _normalized_name(field_name)
        matches = [
            field for field in fields if _normalized_name(str(field.get("name", ""))) == wanted
        ]
        if len(matches) != 1:
            available = ", ".join(str(field.get("name", "")) for field in fields[:25])
            if not matches:
                raise ConfigurationError(f"Поле {field_name!r} не найдено. Поля: {available}")
            raise ConfigurationError(
                f"Найдено несколько полей {field_name!r}; укажите ID проекта точнее"
            )
        field = matches[0]
        field_type = str(field.get("type", ""))
        value: Any = field_value
        if field_type == "cats":
            categories = field.get("categories") or []
            if categories:
                category_names = [
                    str(category.get("name", "")) if isinstance(category, dict) else str(category)
                    for category in categories
                ]
                category_map = {_normalized_name(name): name for name in category_names}
                target = category_map.get(_normalized_name(field_value))
                if not target:
                    raise ConfigurationError(
                        f"В поле {field_name!r} нет значения "
                        f"{field_value!r}. Доступно: "
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
            project_name=project_name,
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

    def get_lead_call_records(
        self,
        lead_id: str | int,
        *,
        project_id: int | None = None,
        max_pages: int = 3,
    ) -> list[dict[str, Any]]:
        """Return call entries from the same feed used by the LPTracker lead card.

        The documented Direct API lead payload does not include recordings for
        every account. LPTracker's own card loads them from a separate feed,
        authenticated with a web bearer token issued from the same credentials.
        """
        records: list[dict[str, Any]] = []
        page_count = max(1, min(int(max_pages), 10))
        for page in range(1, page_count + 1):
            params: dict[str, Any] = {"page": page, "direction": "desc"}
            if project_id:
                params["project_id"] = int(project_id)
            payload = self._web_request(
                "GET",
                f"/rest/leads/feed/{lead_id}",
                params=params,
            )
            if not isinstance(payload, dict):
                raise CrmError("LPTracker вернул неожиданный формат истории лида")
            items = payload.get("data")
            if not isinstance(items, list):
                raise CrmError("LPTracker не вернул список событий истории лида")
            records.extend(
                _normalize_web_call_record(item)
                for item in items
                if isinstance(item, dict) and str(item.get("item_type", "")) == "call"
            )
            meta = payload.get("_meta")
            total_pages = 1
            if isinstance(meta, dict):
                with suppress(TypeError, ValueError):
                    total_pages = max(1, int(meta.get("countPages", 1)))
            if page >= total_pages:
                break
        return records

    def list_recent_leads(
        self,
        project_id: int,
        *,
        updated_from: int,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 200:
            raise ValueError("limit списка лидов должен быть от 1 до 200")
        if offset < 0:
            raise ValueError("offset списка лидов не может быть отрицательным")
        result = self._request(
            "GET",
            f"/lead/{project_id}/list",
            params={
                "offset": offset,
                "limit": limit,
                "sort[updated_at]": 3,
                "filter[updated_at_from]": int(updated_from),
                "is_deal": "false",
            },
        )
        return _ensure_list(result, "список лидов")

    def update_contact_detail(self, detail_id: str | int, phone: str) -> None:
        normalized = normalize_phone(phone)
        if not normalized:
            raise CrmError("Нельзя заменить контакт: номер имеет неверный формат")
        self._request(
            "PUT",
            f"/contact/details/{str(detail_id).strip()}",
            json={"value": normalized},
        )

    def update_lead_custom(self, lead_id: str | int, destination: CrmDestination) -> None:
        self._request(
            "PUT",
            f"/lead/{str(lead_id).strip()}",
            json={"custom": {str(destination.field_id): destination.field_value}},
        )

    def set_lead_funnel(self, lead_id: str | int, funnel_id: int) -> None:
        self._request(
            "PUT",
            f"/lead/{str(lead_id).strip()}/funnel",
            json={"funnel": int(funnel_id)},
        )

    def set_lead_owner(self, lead_id: str | int, owner_id: int) -> None:
        self._request(
            "PUT",
            f"/lead/{str(lead_id).strip()}/owner",
            json={"owner": int(owner_id)},
        )

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

    def assess_first_outgoing_call_sla(
        self,
        project_id: int,
        lead_ids: Iterable[str | int],
        *,
        now: datetime | None = None,
        sample_size: int = 10,
        min_timely: int = 8,
        max_delay_seconds: float = 300.0,
        lookback_hours: float = 24.0,
    ) -> FirstCallSlaAssessment:
        """Assess the latest mature leads from one batch without mutating CRM."""
        if sample_size < 1 or min_timely < 1 or min_timely > sample_size:
            raise ValueError("Некорректные параметры выборки SLA первого звонка")
        checked_at = (now or datetime.now(UTC)).astimezone(UTC)
        wanted_ids = {str(lead_id).strip() for lead_id in lead_ids if str(lead_id).strip()}
        if not wanted_ids:
            return FirstCallSlaAssessment(
                "insufficient", 0, 0, 0, 0, sample_size, min_timely, max_delay_seconds
            )
        try:
            local_timezone = ZoneInfo(self.settings.lptracker_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ConfigurationError(
                f"Неизвестный часовой пояс LPTRACKER_TIMEZONE={self.settings.lptracker_timezone!r}"
            ) from exc

        lookback_start = checked_at - timedelta(hours=lookback_hours)
        mature_before = checked_at - timedelta(seconds=max_delay_seconds)
        candidates: list[tuple[datetime, dict[str, Any]]] = []
        for offset in (0, 200):
            leads = self.list_recent_leads(
                project_id,
                updated_from=int(lookback_start.timestamp()),
                limit=200,
                offset=offset,
            )
            for lead in leads:
                lead_id = str(lead.get("id", "")).strip()
                if lead_id not in wanted_ids:
                    continue
                created_at = _extract_lead_created_at(lead, local_timezone)
                if created_at is None or not (lookback_start <= created_at < mature_before):
                    continue
                candidates.append((created_at, lead))
            if len(leads) < 200 or len(candidates) >= sample_size:
                break

        candidates.sort(key=lambda candidate: candidate[0], reverse=True)
        selected = candidates[:sample_size]
        sampled = len(selected)
        if sampled < sample_size:
            return FirstCallSlaAssessment(
                status="insufficient",
                eligible=len(candidates),
                sampled=sampled,
                timely=0,
                late=0,
                sample_size=sample_size,
                min_timely=min_timely,
                max_delay_seconds=max_delay_seconds,
            )
        timely = 0
        for created_at, lead in selected:
            records = self.get_lead_call_records(
                str(lead.get("id", "")).strip(),
                project_id=project_id,
                max_pages=2,
            )
            first_call_at = _extract_first_outgoing_call_at(records, local_timezone)
            delay = None if first_call_at is None else (first_call_at - created_at).total_seconds()
            if delay is not None and 0 <= delay <= max_delay_seconds:
                timely += 1

        late = sampled - timely
        status = "passed" if timely >= min_timely else "failed"
        return FirstCallSlaAssessment(
            status=status,
            eligible=len(candidates),
            sampled=sampled,
            timely=timely,
            late=late,
            sample_size=sample_size,
            min_timely=min_timely,
            max_delay_seconds=max_delay_seconds,
        )

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
        listing_id = _listing_id(canonical_avito_url(listing_url))
        for contact in self.search_contacts(project_id, normalized):
            contact_id = contact.get("id")
            if contact_id is None:
                continue
            for lead in self.contact_leads(contact_id):
                if _lead_name_matches_listing(str(lead.get("name", "")), listing_id, repeat=repeat):
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
        moscow_offset: object = 0,
    ) -> CrmWriteResult:
        normalized = normalize_phone(phone)
        if not normalized:
            raise CrmError("Нельзя создать лид: номер имеет неверный формат")
        canonical_url = canonical_avito_url(listing_url)
        listing_id = _listing_id(canonical_url)
        lead_name = format_avito_lead_name(
            listing_id,
            moscow_offset=moscow_offset,
            repeat=repeat,
        )
        contacts = self.search_contacts(destination.project_id, normalized)
        contact_ids = [
            str(contact.get("id", "")).strip()
            for contact in contacts
            if str(contact.get("id", "")).strip()
        ]
        leads_by_contact: dict[str, list[dict[str, Any]]] = {}

        # Crash-safe recovery: the API may have created the lead while the process
        # stopped before its ID reached Google Sheets.  The deterministic listing
        # name lets the next run recover that exact lead instead of creating another.
        for existing_contact_id in contact_ids:
            contact_leads = self.contact_leads(existing_contact_id)
            leads_by_contact[existing_contact_id] = contact_leads
            existing = self._find_existing_lead(
                contact_leads,
                listing_id,
                repeat,
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

        expired_phone_match = False
        if contacts and self.settings.duplicate_policy == "skip" and not force_create:
            window_days = self.settings.crm_duplicate_window_days
            newest_lead_at = _newest_lead_created_at(
                (lead for leads in leads_by_contact.values() for lead in leads),
                self.settings.lptracker_timezone,
            )
            age_days = (
                max(0.0, (datetime.now(UTC) - newest_lead_at).total_seconds() / 86_400)
                if newest_lead_at is not None
                else None
            )
            duplicate_is_recent = window_days > 0 and (age_days is None or age_days < window_days)
            if duplicate_is_recent:
                if age_days is None:
                    detail = (
                        "Номер уже есть в CRM; дату последнего лида "
                        "определить не удалось, новый лид не создан"
                    )
                else:
                    detail = (
                        f"Номер уже есть в CRM; последнему лиду {age_days:.1f} дн. "
                        f"(< {window_days:g} дн.), новый лид не создан"
                    )
                return CrmWriteResult(
                    status=ItemStatus.DUPLICATE,
                    contact_id=contact_ids[0] if contact_ids else None,
                    detail=detail,
                    created=False,
                )
            expired_phone_match = True
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
        if expired_phone_match:
            detail += (
                f"; совпадение по номеру старше "
                f"{self.settings.crm_duplicate_window_days:g} дней и не блокирует новое объявление"
            )
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
        self,
        leads: list[dict[str, Any]],
        listing_id: str,
        repeat: bool,
        field_id: int,
    ) -> dict[str, Any] | None:
        for lead in leads:
            if not _lead_name_matches_listing(str(lead.get("name", "")), listing_id, repeat=repeat):
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

    def _authenticate_web(self) -> None:
        self.settings.require_crm_credentials()
        self.rate_limiter.wait()
        try:
            response = self.client.post(
                f"{LPTRACKER_WEB_BASE_URL}/rest/system/login",
                json={
                    "email": self.settings.lptracker_login,
                    "password": self.settings.lptracker_password,
                },
            )
        except httpx.RequestError as exc:
            raise CrmError(f"Не удалось авторизоваться в истории LPTracker: {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise CrmError(
                f"История LPTracker вернула не-JSON при авторизации (HTTP {response.status_code})"
            ) from exc
        result = payload.get("result") if isinstance(payload, dict) else None
        data = result.get("data") if isinstance(result, dict) else None
        token = str(data.get("token", "")) if isinstance(data, dict) else ""
        if response.status_code >= 400 or not token:
            raise CrmError("LPTracker не разрешил доступ к истории звонков")
        self.web_token = token
        # Audio tags in LPTracker's own card use this cookie, while feed XHRs
        # use the Authorization header. Keeping both also supports record URLs
        # that are protected instead of public.
        self.client.cookies.set("bearer_token", token, domain=".lptracker.ru", path="/")

    def _web_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if not self.web_token:
            self._authenticate_web()
        url = urljoin(f"{LPTRACKER_WEB_BASE_URL}/", path.lstrip("/"))
        for auth_attempt in range(2):
            last_error: Exception | None = None
            for attempt in range(4):
                self.rate_limiter.wait()
                try:
                    response = self.client.request(
                        method,
                        url,
                        headers={"Authorization": f"Bearer {self.web_token}"},
                        params=params,
                    )
                except httpx.RequestError as exc:
                    last_error = exc
                    if attempt == 3:
                        break
                    time.sleep(min(8.0, 2**attempt))
                    continue
                if response.status_code == 401 and auth_attempt == 0:
                    self.web_token = ""
                    self._authenticate_web()
                    break
                if response.status_code in {429, 500, 502, 503, 504}:
                    if attempt == 3:
                        raise CrmError(
                            f"История LPTracker временно недоступна (HTTP {response.status_code})"
                        )
                    time.sleep(min(8.0, 2**attempt))
                    continue
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise CrmError(
                        f"История LPTracker вернула не-JSON (HTTP {response.status_code})"
                    ) from exc
                if response.status_code >= 400:
                    raise CrmError(_web_error_message(payload, response.status_code))
                return _unwrap_web_payload(payload)
            else:
                continue
            if last_error is not None:
                raise CrmError(f"Не удалось получить историю LPTracker: {last_error}")
        raise CrmError("LPTracker отклонил повторную авторизацию для истории звонков")

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


def _unwrap_web_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        raise CrmError("История LPTracker вернула неожиданный формат ответа")
    status = payload.get("status")
    if status in (1, "1", True, "success"):
        result = payload.get("result")
        if isinstance(result, dict):
            data = result.get("data")
            # Login uses result.data, while feed deployments may return either
            # result itself or result.data with adjacent pagination metadata.
            if isinstance(data, dict) and ("data" in data or "_meta" in data):
                return data
            return result
    if "data" in payload:
        return payload
    raise CrmError(_web_error_message(payload, 200))


def _web_error_message(payload: Any, status_code: int) -> str:
    if isinstance(payload, dict):
        for key in ("message", "error", "description"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return f"LPTracker: {value.strip()}"
    return f"История LPTracker вернула ошибку HTTP {status_code}"


def _normalize_web_call_record(item: dict[str, Any]) -> dict[str, Any]:
    record = dict(item)
    path = str(record.get("record_path", "") or "").strip()
    if path:
        record["record"] = urljoin(f"{LPTRACKER_WEB_BASE_URL}/", path.lstrip("/"))
    if not record.get("type") and not record.get("direction"):
        call_type = record.get("call_type_text") or record.get("call_type")
        if call_type not in (None, ""):
            record["direction"] = call_type
    if not record.get("duration"):
        record["duration"] = record.get("record_time")
    if not record.get("time_src") and record.get("sort_field"):
        record["time_src"] = record.get("sort_field")
    return record


def _normalized_name(value: str) -> str:
    return " ".join(value.split()).casefold()


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _listing_id(url: str) -> str:
    numbers = re.findall(r"\d{5,}", url)
    return numbers[-1] if numbers else url.rstrip("/").rsplit("/", 1)[-1][:80]


def normalize_moscow_offset(value: object) -> int:
    """Return the approved non-negative lead prefix; Kaliningrad maps to zero."""
    try:
        parsed = float(str(value if value is not None else 0).strip().replace(",", "."))
    except (TypeError, ValueError):
        return 0
    if parsed <= 0 or not parsed.is_integer():
        return 0
    return int(parsed)


def format_avito_lead_name(
    listing_id: str,
    *,
    moscow_offset: object = 0,
    repeat: bool = False,
) -> str:
    name = f"{normalize_moscow_offset(moscow_offset)} Авито — {listing_id}"
    return f"{name} — повторный лид" if repeat else name


def is_managed_avito_lead(lead: dict[str, Any]) -> bool:
    """Identify only leads created by this application, including legacy names."""
    name = str(lead.get("name", "")).strip()
    if not re.fullmatch(r"(?:\d+\s+)?Авито\s+—\s+\S+(?:\s+—\s+повторный лид)?", name):
        return False
    view = lead.get("view")
    if not isinstance(view, dict) or not str(view.get("campaign", "")).strip():
        return True
    return _normalized_name(str(view.get("campaign", ""))) == _normalized_name("Avito CRM Pipeline")


def _lead_name_matches_listing(
    name: str,
    listing_id: str,
    *,
    repeat: bool | None,
) -> bool:
    normalized = " ".join(str(name).split())
    suffix = " — повторный лид"
    is_repeat = normalized.endswith(suffix)
    if repeat is not None and is_repeat is not repeat:
        return False
    if is_repeat:
        normalized = normalized[: -len(suffix)]
    prefixes = (f"Авито — {listing_id}",)
    if normalized in prefixes:
        return True
    return bool(re.fullmatch(rf"\d+\s+Авито\s+—\s+{re.escape(listing_id)}", normalized))


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


def _newest_lead_created_at(
    leads: Iterable[dict[str, Any]],
    timezone_name: str,
) -> datetime | None:
    try:
        local_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigurationError(
            f"Неизвестный часовой пояс LPTRACKER_TIMEZONE={timezone_name!r}"
        ) from exc
    dates = [
        created_at
        for lead in leads
        if (created_at := _extract_lead_created_at(lead, local_timezone)) is not None
    ]
    return max(dates) if dates else None


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


def _extract_first_outgoing_call_at(
    records: Iterable[dict[str, Any]], local_timezone: ZoneInfo
) -> datetime | None:
    call_dates: list[datetime] = []
    for record in records:
        direction = _normalized_name(
            str(
                record.get("direction")
                or record.get("call_type_text")
                or record.get("call_type")
                or ""
            )
        )
        if "исход" not in direction and direction not in {
            "out",
            "outgoing",
            "outbound",
            "to",
        }:
            continue
        for key in ("time_src", "sort_field", "time", "created_at", "started_at", "date"):
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
