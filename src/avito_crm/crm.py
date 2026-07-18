from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

import httpx

from avito_crm.config import Settings
from avito_crm.errors import ConfigurationError, CrmError
from avito_crm.models import CrmDestination, CrmWriteResult, ItemStatus
from avito_crm.phone import canonical_avito_url, normalize_phone

LOGGER = logging.getLogger(__name__)


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

    def create_for_phone(
        self,
        phone: str,
        listing_url: str,
        destination: CrmDestination,
    ) -> CrmWriteResult:
        normalized = normalize_phone(phone)
        if not normalized:
            raise CrmError("Нельзя создать лид: номер имеет неверный формат")
        canonical_url = canonical_avito_url(listing_url)
        contacts = self.search_contacts(destination.project_id, normalized)
        if contacts and self.settings.duplicate_policy == "skip":
            return CrmWriteResult(
                status=ItemStatus.DUPLICATE,
                contact_id=str(contacts[0].get("id", "")) or None,
                detail="Контакт с таким номером уже существует; новый лид не создан",
            )

        listing_id = _listing_id(canonical_url)
        lead_name = f"Авито — {listing_id}"
        payload: dict[str, Any] = {
            "name": lead_name,
            "callback": False,
            "custom": {str(destination.field_id): destination.field_value},
            "view": {"source": "Avito", "campaign": "Avito CRM Pipeline"},
        }
        contact_id: str | None = None
        if contacts:
            contact_id = str(contacts[0].get("id", ""))
            if not contact_id:
                raise CrmError("LPTracker вернул контакт без ID")
            existing = self._find_existing_lead(contact_id, lead_name, destination.field_id)
            if existing:
                return CrmWriteResult(
                    status=ItemStatus.DUPLICATE,
                    contact_id=contact_id,
                    lead_id=str(existing.get("id", "")) or None,
                    detail="Лид для этого объявления уже существует",
                )
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
        returned_contact = result.get("contact_id")
        if not returned_contact and isinstance(result.get("contact"), dict):
            returned_contact = result["contact"].get("id")
        return CrmWriteResult(
            status=ItemStatus.DONE,
            contact_id=str(returned_contact or contact_id or "") or None,
            lead_id=str(result["id"]),
            detail="Лид создан",
        )

    def _find_existing_lead(
        self, contact_id: str, lead_name: str, field_id: int
    ) -> dict[str, Any] | None:
        for lead in self.contact_leads(contact_id):
            if str(lead.get("name", "")) != lead_name:
                continue
            custom = lead.get("custom") or []
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
