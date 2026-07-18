import json

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
    assert len(requests) == 5


def test_crm_skips_existing_contact_by_default(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return _success({"token": "temporary-test-token"})
        if request.url.path == "/contact/search":
            return _success([{"id": 99, "project_id": 1}])
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
