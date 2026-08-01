import json
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from avito_crm.crm import (
    LpTrackerClient,
    RateLimiter,
    format_avito_lead_name,
    normalize_moscow_offset,
)
from avito_crm.errors import ConfigurationError, CrmError
from avito_crm.models import CrmDestination, ItemStatus


def test_crm_resolves_category_and_creates_lead(settings):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/login":
            return _success({"token": "temporary-test-token"})
        if path == "/projects":
            return _success([{"id": 1, "name": "Ремонт", "domain": "example.test"}])
        if path == "/project/1/customs":
            return _success(
                [
                    {
                        "id": 42,
                        "name": "Тег+ для новых с Ав и Ян",
                        "type": "cats",
                        "is_multi_select": 1,
                        "categories": [{"name": "Сбор № лпр (Ав, ремонт кв. под ключ)"}],
                    }
                ]
            )
        if path == "/contact/search":
            assert request.url.params["phone"] == "79991234567"
            return _success([])
        if path == "/lead":
            body = json.loads(request.content)
            assert body["custom"] == {"42": ["Сбор № лпр (Ав, ремонт кв. под ключ)"]}
            assert body["contact"]["details"] == [{"type": "phone", "data": "+79991234567"}]
            return _success({"id": 777, "contact_id": 555})
        if path == "/lead/777/comment":
            assert json.loads(request.content) == {
                "text": "https://www.avito.ru/moskva/item_123456789"
            }
            return _success(None)
        raise AssertionError(f"unexpected request: {request.method} {path}")

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url=settings.lptracker_base_url)
    with LpTrackerClient(settings, http) as crm:
        crm.rate_limiter = RateLimiter(100_000)
        destination = crm.resolve_destination()
        result = crm.create_for_phone(
            "+79991234567",
            "https://www.avito.ru/moskva/item_123456789",
            destination,
        )

    assert destination.field_id == 42
    assert result.status == ItemStatus.DONE
    assert result.lead_id == "777"
    assert len(requests) == 6


def test_lead_name_contains_approved_moscow_offset_prefix():
    assert format_avito_lead_name("123456789", moscow_offset=2) == "2 Авито — 123456789"
    assert format_avito_lead_name("123456789", moscow_offset=-1) == "0 Авито — 123456789"
    assert normalize_moscow_offset("0") == 0
    assert normalize_moscow_offset("3") == 3
    assert normalize_moscow_offset("2.5") == 0


def test_crm_handoff_write_contracts_match_lptracker_api(settings):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/login":
            return _success({"token": "temporary-test-token"})
        if request.url.path == "/contact/details/501":
            assert request.method == "PUT"
            assert json.loads(request.content) == {"value": "+79991234567"}
            return _success({"id": "501", "type": "phone", "data": "+79991234567"})
        if request.url.path == "/lead/700":
            assert request.method == "PUT"
            assert json.loads(request.content) == {
                "custom": {"42": ["Предлагаем бесплатный аудит авито"]}
            }
            return _success({"id": 700})
        if request.url.path == "/lead/700/funnel":
            assert request.method == "PUT"
            assert json.loads(request.content) == {"funnel": 20}
            return _success({"id": 700, "funnel": 20})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=settings.lptracker_base_url,
    )
    with LpTrackerClient(settings, http) as crm:
        crm.rate_limiter = RateLimiter(100_000)
        crm.update_contact_detail(501, "+79991234567")
        crm.update_lead_custom(
            700,
            CrmDestination(
                project_id=1,
                project_name="Project",
                field_id=42,
                field_name="Тег+ для новых с Ав и Ян",
                field_type="cats",
                field_value=["Предлагаем бесплатный аудит авито"],
            ),
        )
        crm.set_lead_funnel(700, 20)

    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/login"),
        ("PUT", "/contact/details/501"),
        ("PUT", "/lead/700"),
        ("PUT", "/lead/700/funnel"),
    ]


def test_crm_recent_lead_scan_uses_documented_pagination_and_filter(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return _success({"token": "temporary-test-token"})
        if request.url.path == "/lead/1/list":
            assert request.url.params["offset"] == "100"
            assert request.url.params["limit"] == "50"
            assert request.url.params["sort[updated_at]"] == "3"
            assert request.url.params["filter[updated_at_from]"] == "123456"
            assert request.url.params["is_deal"] == "false"
            return _success([{"id": 700}])
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=settings.lptracker_base_url,
    )
    with LpTrackerClient(settings, http) as crm:
        crm.rate_limiter = RateLimiter(100_000)
        leads = crm.list_recent_leads(1, updated_from=123456, limit=50, offset=100)

    assert leads == [{"id": 700}]


def test_crm_forced_repeat_creates_new_lead_with_funnel_and_listing_comment(settings):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/login":
            return _success({"token": "temporary-test-token"})
        if path == "/contact/search":
            return _success([{"id": 99, "project_id": 1}])
        if path == "/contact/99/leads":
            return _success([])
        if path == "/lead":
            body = json.loads(request.content)
            assert body["name"] == "0 Авито — 123456789 — повторный лид"
            assert body["contact_id"] == "99"
            assert body["funnel"] == 88
            return _success({"id": 778, "contact_id": 99})
        if path == "/lead/778/comment":
            assert json.loads(request.content) == {
                "text": "https://www.avito.ru/moskva/item_123456789"
            }
            return _success(None)
        raise AssertionError(f"unexpected request: {request.method} {path}")

    http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=settings.lptracker_base_url,
    )
    with LpTrackerClient(settings, http) as crm:
        crm.rate_limiter = RateLimiter(100_000)
        result = crm.create_for_phone(
            "+79991234567",
            "https://www.avito.ru/moskva/item_123456789",
            _destination(),
            force_create=True,
            funnel_id=88,
            repeat=True,
        )

    assert result.status == ItemStatus.DONE
    assert result.lead_id == "778"
    assert [request.url.path for request in requests] == [
        "/login",
        "/contact/search",
        "/contact/99/leads",
        "/lead",
        "/lead/778/comment",
    ]


def test_created_lead_retries_comment_while_lptracker_indexes_id(settings, monkeypatch):
    requests = []
    comment_attempts = 0
    delays = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal comment_attempts
        requests.append(request)
        if request.url.path == "/login":
            return _success({"token": "temporary-test-token"})
        if request.url.path == "/contact/search":
            return _success([])
        if request.url.path == "/lead":
            return _success({"id": 779, "contact_id": 100})
        if request.url.path == "/lead/779/comment":
            comment_attempts += 1
            if comment_attempts <= 3:
                return _failure(400, "Invalid lead ID")
            return _success(None)
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    monkeypatch.setattr("avito_crm.crm.time.sleep", delays.append)
    http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=settings.lptracker_base_url,
    )
    with LpTrackerClient(settings, http) as crm:
        crm.rate_limiter = RateLimiter(100_000)
        result = crm.create_for_phone(
            "+79991234567",
            "https://www.avito.ru/moskva/item_123456789",
            _destination(),
        )

    assert result.status == ItemStatus.DONE
    assert result.lead_id == "779"
    assert result.detail == "Лид создан"
    assert delays == [1.0, 2.0, 4.0]
    assert [request.url.path for request in requests].count("/lead") == 1
    assert [request.url.path for request in requests].count("/lead/779/comment") == 4


def test_comment_does_not_retry_other_lptracker_400_errors(settings, monkeypatch):
    requests = []
    delays = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/login":
            return _success({"token": "temporary-test-token"})
        if request.url.path == "/lead/779/comment":
            return _failure(400, "Comment is forbidden")
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    monkeypatch.setattr("avito_crm.crm.time.sleep", delays.append)
    http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=settings.lptracker_base_url,
    )
    with LpTrackerClient(settings, http) as crm:
        crm.rate_limiter = RateLimiter(100_000)
        with pytest.raises(CrmError, match="Comment is forbidden"):
            crm.add_listing_comment(779, "https://www.avito.ru/moskva/item_123456789")

    assert delays == []
    assert [request.url.path for request in requests].count("/lead/779/comment") == 1


def test_created_lead_stays_successful_after_comment_retries_are_exhausted(settings, monkeypatch):
    requests = []
    delays = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/login":
            return _success({"token": "temporary-test-token"})
        if request.url.path == "/contact/search":
            return _success([])
        if request.url.path == "/lead":
            return _success({"id": 779, "contact_id": 100})
        if request.url.path == "/lead/779/comment":
            return _failure(400, "Invalid lead ID")
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    monkeypatch.setattr("avito_crm.crm.time.sleep", delays.append)
    http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=settings.lptracker_base_url,
    )
    with LpTrackerClient(settings, http) as crm:
        crm.rate_limiter = RateLimiter(100_000)
        result = crm.create_for_phone(
            "+79991234567",
            "https://www.avito.ru/moskva/item_123456789",
            _destination(),
        )

    assert result.status == ItemStatus.DONE
    assert result.lead_id == "779"
    assert "ссылку в комментарий записать не удалось" in result.detail
    assert "Invalid lead ID" in result.detail
    assert delays == [1.0, 2.0, 4.0]
    assert [request.url.path for request in requests].count("/lead") == 1
    assert [request.url.path for request in requests].count("/lead/779/comment") == 4


def test_crm_reads_funnel_stage_from_lead(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return _success({"token": "temporary-test-token"})
        if request.url.path == "/lead/777":
            return _success({"id": 777, "funnel": 12})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=settings.lptracker_base_url,
    )
    with LpTrackerClient(settings, http) as crm:
        crm.rate_limiter = RateLimiter(100_000)
        stage = crm.get_lead_stage_name(
            777,
            project_id=1,
            funnel_steps=[{"id": 12, "name": "Автоответчик"}],
        )

    assert stage == "Автоответчик"


def test_crm_calculates_first_call_delay_in_configured_timezone(settings):
    timezone = ZoneInfo(settings.lptracker_timezone)
    created_at = datetime(2026, 7, 25, 12, 0, tzinfo=timezone)
    first_call_at = datetime(2026, 7, 25, 12, 11, tzinfo=timezone)
    later_call_at = datetime(2026, 7, 25, 12, 20, tzinfo=timezone)
    lead = {
        "created_at": created_at.timestamp(),
        "calls_records": [
            {"time": later_call_at.timestamp()},
            {"time": first_call_at.timestamp()},
        ],
    }

    with httpx.Client() as http, LpTrackerClient(settings, http) as crm:
        delay = crm.first_call_delay_seconds(lead)

    assert delay == 11 * 60


def test_crm_returns_no_delay_when_first_call_date_is_missing(settings):
    lead = {"created_at": "25.07.2026 12:00:00", "calls_records": []}

    with httpx.Client() as http, LpTrackerClient(settings, http) as crm:
        delay = crm.first_call_delay_seconds(lead)

    assert delay is None


def test_crm_rejects_unknown_timezone_only_when_call_delay_is_needed(settings):
    invalid_settings = replace(settings, lptracker_timezone="Missing/Timezone")

    with (
        httpx.Client() as http,
        LpTrackerClient(invalid_settings, http) as crm,
        pytest.raises(ConfigurationError, match="LPTRACKER_TIMEZONE"),
    ):
        crm.first_call_delay_seconds({"created_at": "25.07.2026 12:00:00"})


def test_crm_deletes_lead_only_after_successful_api_response(settings):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/login":
            return _success({"token": "temporary-test-token"})
        if request.url.path == "/lead/777" and request.method == "DELETE":
            return _success(None)
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=settings.lptracker_base_url,
    )
    with LpTrackerClient(settings, http) as crm:
        crm.rate_limiter = RateLimiter(100_000)
        crm.delete_lead(777)

    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/login"),
        ("DELETE", "/lead/777"),
    ]


def test_crm_skips_existing_contact_by_default(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return _success({"token": "temporary-test-token"})
        if request.url.path == "/contact/search":
            return _success([{"id": 99, "project_id": 1}])
        if request.url.path == "/contact/99/leads":
            return _success([])
        raise AssertionError(f"unexpected write: {request.method} {request.url.path}")

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url=settings.lptracker_base_url)
    with LpTrackerClient(settings, http) as crm:
        crm.rate_limiter = RateLimiter(100_000)
        result = crm.create_for_phone(
            "+79991234567",
            "https://www.avito.ru/moskva/item_123456789",
            destination=_destination(),
        )

    assert result.status == ItemStatus.DUPLICATE
    assert result.contact_id == "99"


def test_crm_recovers_exact_listing_lead_after_interrupted_write(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return _success({"token": "temporary-test-token"})
        if request.url.path == "/contact/search":
            return _success([{"id": 99, "project_id": 1}])
        if request.url.path == "/contact/99/leads":
            return _success(
                [
                    {
                        "id": 701,
                        "name": "Авито — 123456789",
                    }
                ]
            )
        if request.url.path == "/lead/701/comment":
            return _success(None)
        raise AssertionError(f"unexpected write: {request.method} {request.url.path}")

    http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=settings.lptracker_base_url,
    )
    with LpTrackerClient(settings, http) as crm:
        crm.rate_limiter = RateLimiter(100_000)
        result = crm.create_for_phone(
            "+79991234567",
            "https://www.avito.ru/moskva/item_123456789",
            destination=_destination(),
        )

    assert result.status == ItemStatus.DONE
    assert result.lead_id == "701"
    assert result.created is False


def test_crm_projects_can_be_listed_before_project_is_configured(settings):
    settings_without_project = replace(
        settings,
        lptracker_project_id=None,
        lptracker_project_name="",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return _success({"token": "temporary-test-token"})
        if request.url.path == "/projects":
            return _success([{"id": 1, "name": "Ремонт"}])
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url=settings.lptracker_base_url)
    with LpTrackerClient(settings_without_project, http) as crm:
        crm.rate_limiter = RateLimiter(100_000)
        projects = crm.list_projects()

    assert projects == [{"id": 1, "name": "Ремонт"}]


def test_crm_accepts_configured_category_when_project_list_omits_options(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return _success({"token": "temporary-test-token"})
        if request.url.path == "/projects":
            return _success([{"id": 1, "name": "Ремонт"}])
        if request.url.path == "/project/1/customs":
            return _success(
                [
                    {
                        "id": 42,
                        "name": "Тег+ для новых с Ав и Ян",
                        "type": "cats",
                    }
                ]
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url=settings.lptracker_base_url)
    with LpTrackerClient(settings, http) as crm:
        crm.rate_limiter = RateLimiter(100_000)
        destination = crm.resolve_destination()

    assert destination.field_id == 42
    assert destination.field_value == "Сбор № лпр (Ав, ремонт кв. под ключ)"


def _success(result):
    return httpx.Response(200, json={"status": "success", "result": result})


def _failure(code, message):
    return httpx.Response(
        code,
        json={"status": "error", "errors": [{"code": code, "message": message}]},
    )


def _destination():
    from avito_crm.models import CrmDestination

    return CrmDestination(
        project_id=1,
        project_name="Ремонт",
        field_id=42,
        field_name="Тег+ для новых с Ав и Ян",
        field_type="cats",
        field_value=["Сбор № лпр (Ав, ремонт кв. под ключ)"],
    )
