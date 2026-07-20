import json
from dataclasses import replace

import httpx

from avito_crm.crm import LpTrackerClient, RateLimiter
from avito_crm.models import ItemStatus


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
            assert body["name"] == "Авито — 123456789 — повторный лид"
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
