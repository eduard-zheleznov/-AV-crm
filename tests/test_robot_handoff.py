from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from avito_crm.errors import (
    AppError,
    ConfigurationError,
    CrmError,
    ManualReviewRequired,
)
from avito_crm.models import CrmDestination
from avito_crm.robot_handoff import (
    GeminiPhoneTranscriber,
    RobotLeadHandoff,
    TranscriptionResult,
    _latest_successful_outgoing_record,
)
from avito_crm.state import StateStore


def _lead() -> dict:
    return {
        "id": 700,
        "name": "2 Авито — 123456789",
        "view": {"campaign": "Avito CRM Pipeline"},
        "funnel": {"id": 10, "name": "⚙️ Лид с робота"},
        "contact": {
            "details": [
                {"id": 501, "type": "phone", "data": "+79990000000"},
            ]
        },
        "custom": [],
        "calls_records": [
            {
                "linkedid": "test-call",
                "type": "outgoing",
                "duration": 32,
                "time": 100,
                "record": "https://records.example.test/call.mp3",
            }
        ],
    }


def test_record_selection_requires_explicit_successful_outgoing_call():
    lead = {
        "calls_records": [
            {
                "type": "incoming",
                "duration": 40,
                "time": 300,
                "record": "https://records.example.test/incoming.mp3",
            },
            {
                "duration": 40,
                "time": 250,
                "record": "https://records.example.test/unknown.mp3",
            },
            {
                "type": "outgoing",
                "duration": 0,
                "time": 200,
                "record": "https://records.example.test/failed.mp3",
            },
            {
                "direction": "исходящий звонок",
                "duration": 25,
                "time": 100,
                "record": "https://records.example.test/safe.mp3",
            },
        ]
    }

    selected = _latest_successful_outgoing_record(lead)

    assert selected is not None
    assert selected["record"].endswith("safe.mp3")


class FakeTranscriber:
    def __init__(self, result: TranscriptionResult) -> None:
        self.result = result
        self.calls = 0

    def transcribe(self, _url: str) -> TranscriptionResult:
        self.calls += 1
        return self.result

    def close(self) -> None:
        return None


class FakeCrm:
    def __init__(self, lead: dict) -> None:
        self.lead = deepcopy(lead)
        self.events: list[str] = []

    def __exit__(self, *_args: object) -> None:
        return None

    def resolve_destination(self) -> CrmDestination:
        return CrmDestination(1, "Project", 42, "Source", "cats", "Source value")

    def resolve_custom_destination(
        self, project_id: int, field_name: str, field_value: str, *, project_name: str = ""
    ) -> CrmDestination:
        if field_name == "Дата шага":
            return CrmDestination(
                project_id, project_name, 100, field_name, "funnel_date", field_value
            )
        return CrmDestination(project_id, project_name, 99, field_name, "cats", [field_value])

    def list_funnel_steps(self, _project_id: int) -> list[dict]:
        return [
            {"id": 10, "name": "⚙️ Лид с робота"},
            {"id": 20, "name": "Новый лид"},
        ]

    def list_recent_leads(self, _project_id: int, **_kwargs: object) -> list[dict]:
        return [{"id": self.lead["id"]}]

    def get_lead(self, _lead_id: str | int) -> dict:
        return deepcopy(self.lead)

    def get_lead_stage_name(self, _lead_id: str | int, *, lead: dict, **_kwargs: object) -> str:
        funnel = lead.get("funnel", {})
        return str(funnel.get("name", "")) if isinstance(funnel, dict) else ""

    def update_contact_detail(self, _detail_id: str | int, phone: str) -> None:
        self.events.append("phone")
        self.lead["contact"]["details"][0]["data"] = phone

    def update_lead_custom(self, _lead_id: str | int, destination: CrmDestination) -> None:
        self.events.append(
            "date" if destination.field_type in {"date", "funnel_date"} else "custom"
        )
        custom = [
            item
            for item in self.lead["custom"]
            if str(item.get("id", "")) != str(destination.field_id)
        ]
        custom.append({"id": destination.field_id, "value": destination.field_value})
        self.lead["custom"] = custom

    def set_lead_funnel(self, _lead_id: str | int, funnel_id: int) -> None:
        self.events.append("funnel")
        self.lead["funnel"] = {"id": funnel_id, "name": "Новый лид"}


class StagePrefilterCrm(FakeCrm):
    def __init__(self, lead: dict) -> None:
        super().__init__(lead)
        self.get_lead_calls: list[str] = []
        self.list_calls = 0

    def list_recent_leads(self, _project_id: int, **_kwargs: object) -> list[dict]:
        self.list_calls += 1
        offset = int(_kwargs.get("offset", 0))
        if offset == 0:
            return [
                {
                    "id": 800 + index,
                    "name": f"2 Авито — {800000000 + index}",
                    "view": {"campaign": "Avito CRM Pipeline"},
                    "stage_id": None,
                    "custom": [{"type": "funnel", "value": "20"}],
                }
                for index in range(100)
            ]
        if offset == 100:
            return [
                {
                    "id": 702,
                    "name": "Обычный лид",
                    "view": {"campaign": "Другой источник"},
                    "stage_id": None,
                    "custom": [{"type": "funnel", "value": "10"}],
                },
                {
                    "id": 700,
                    "name": "2 Авито — 123456789",
                    "view": {"campaign": "Avito CRM Pipeline"},
                    "stage_id": None,
                    "custom": [{"type": "funnel", "value": "10"}],
                },
            ]
        return []

    def get_lead(self, lead_id: str | int) -> dict:
        self.get_lead_calls.append(str(lead_id))
        return super().get_lead(lead_id)

    def get_lead_stage_name(
        self, _lead_id: str | int, *, lead: dict, **_kwargs: object
    ) -> str:
        direct = super().get_lead_stage_name(_lead_id, lead=lead)
        if direct:
            return direct
        for field in lead.get("custom") or []:
            if field.get("type") == "funnel":
                return {
                    "10": "⚙️ Лид с робота",
                    "20": "Новый лид",
                }.get(str(field.get("value", "")), "")
        return ""


class FailCustomOnceCrm(FakeCrm):
    def __init__(self, lead: dict) -> None:
        super().__init__(lead)
        self.failed = False

    def update_lead_custom(self, lead_id: str | int, destination: CrmDestination) -> None:
        if not self.failed:
            self.failed = True
            self.events.append("custom-failed")
            raise CrmError("temporary CRM failure")
        super().update_lead_custom(lead_id, destination)


class FailFunnelOnceCrm(FakeCrm):
    def __init__(self, lead: dict) -> None:
        super().__init__(lead)
        self.failed = False

    def set_lead_funnel(self, lead_id: str | int, funnel_id: int) -> None:
        if not self.failed:
            self.failed = True
            self.events.append("funnel-failed")
            raise CrmError("temporary funnel failure")
        super().set_lead_funnel(lead_id, funnel_id)


class FailTranscriptionOnce:
    def __init__(self, result: TranscriptionResult) -> None:
        self.result = result
        self.calls = 0

    def transcribe(self, _url: str) -> TranscriptionResult:
        self.calls += 1
        if self.calls == 1:
            raise AppError("temporary Gemini failure")
        return self.result

    def close(self) -> None:
        return None


def test_handoff_replaces_phone_then_tag_then_funnel(settings):
    configured = replace(settings, gemini_api_key="test-only-key")
    crm = FakeCrm(_lead())
    transcriber = FakeTranscriber(
        TranscriptionResult("ok", "+79991234567", 0.99, 1, "девять девять...")
    )
    with StateStore(configured.state_db) as state:
        handler = RobotLeadHandoff(
            configured,
            state,
            crm=crm,
            transcriber=transcriber,
            now_provider=lambda: datetime(2026, 8, 1, 12, 0, tzinfo=ZoneInfo("UTC")),
        )
        summary = handler.run_once(apply=True, lead_id=700)

        saved = state.get_robot_handoff(700)

    assert crm.events == ["phone", "custom", "date", "funnel"]
    assert summary.completed == 1
    assert summary.manual_required == 0
    assert saved["status"] == "completed"
    assert saved["stage_due_date"] == "03.08.2026 15:00"
    assert {item["id"]: item["value"] for item in crm.lead["custom"]}[100] == (
        "03.08.2026 15:00"
    )
    assert transcriber.calls == 1


def test_handoff_prefilters_list_by_stage_before_loading_full_lead(settings):
    configured = replace(settings, gemini_api_key="test-only-key")
    crm = StagePrefilterCrm(_lead())
    transcriber = FakeTranscriber(
        TranscriptionResult("ok", "+79991234567", 0.99, 1, "номер +79991234567")
    )
    with StateStore(configured.state_db) as state:
        handler = RobotLeadHandoff(
            configured,
            state,
            crm=crm,
            transcriber=transcriber,
        )

        summary = handler.run_once(apply=False, limit=1)

    assert summary.inspected == 102
    assert summary.eligible == 1
    assert summary.ready == 1
    assert crm.list_calls == 2
    assert crm.get_lead_calls == ["700"]
    assert transcriber.calls == 1


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (
            lambda lead: lead.update(name="Обычный лид"),
            "имя или источник не совпадают",
        ),
        (
            lambda lead: lead.update(funnel={"id": 20, "name": "Новый лид"}),
            "текущий шаг «Новый лид»",
        ),
        (
            lambda lead: lead.update(calls_records=[]),
            "нет успешной исходящей записи",
        ),
    ],
)
def test_exact_lead_preview_explains_why_lead_is_skipped(settings, change, reason):
    configured = replace(settings, gemini_api_key="test-only-key")
    lead = _lead()
    change(lead)
    crm = FakeCrm(lead)
    transcriber = FakeTranscriber(
        TranscriptionResult("ok", "+79991234567", 0.99, 1, "номер +79991234567")
    )
    with StateStore(configured.state_db) as state:
        handler = RobotLeadHandoff(
            configured,
            state,
            crm=crm,
            transcriber=transcriber,
        )

        summary = handler.run_once(apply=False, lead_id=700)

    assert summary.inspected == 1
    assert summary.eligible == 0
    assert summary.skipped == 1
    assert len(summary.details) == 1
    assert reason in summary.details[0]
    assert transcriber.calls == 0


def test_handoff_does_not_mutate_ambiguous_multi_phone_lead(settings):
    configured = replace(settings, gemini_api_key="test-only-key")
    lead = _lead()
    lead["contact"]["details"].append(
        {"id": 502, "type": "phone", "data": "+79991111111"}
    )
    crm = FakeCrm(lead)
    transcriber = FakeTranscriber(
        TranscriptionResult("ok", "+79991234567", 0.99, 1, "")
    )
    notices: list[tuple[str, str]] = []
    with StateStore(configured.state_db) as state:
        handler = RobotLeadHandoff(
            configured,
            state,
            crm=crm,
            transcriber=transcriber,
            manual_notifier=lambda lead_id, reason: notices.append((lead_id, reason)),
        )
        summary = handler.run_once(apply=True, lead_id=700)

    assert crm.events == []
    assert summary.completed == 0
    assert summary.manual_required == 1
    assert notices and notices[0][0] == "700"


def test_handoff_resumes_after_partial_crm_failure_without_retranscription(settings):
    configured = replace(settings, gemini_api_key="test-only-key")
    crm = FailCustomOnceCrm(_lead())
    transcriber = FakeTranscriber(
        TranscriptionResult("ok", "+79991234567", 0.99, 1, "номер +79991234567")
    )
    with StateStore(configured.state_db) as state:
        handler = RobotLeadHandoff(
            configured,
            state,
            crm=crm,
            transcriber=transcriber,
        )

        first = handler.run_once(apply=True, lead_id=700)
        second = handler.run_once(apply=True, lead_id=700)
        saved = state.get_robot_handoff(700)

    assert first.errors == 1
    assert second.completed == 1
    assert crm.events == ["phone", "custom-failed", "custom", "date", "funnel"]
    assert transcriber.calls == 1
    assert saved["status"] == "completed"


def test_handoff_keeps_original_due_date_when_retry_crosses_midnight(settings):
    configured = replace(settings, gemini_api_key="test-only-key")
    crm = FailFunnelOnceCrm(_lead())
    transcriber = FakeTranscriber(
        TranscriptionResult("ok", "+79991234567", 0.99, 1, "номер +79991234567")
    )
    moments = iter(
        [
            datetime(2026, 8, 1, 23, 59, tzinfo=ZoneInfo("Europe/Moscow")),
            datetime(2026, 8, 2, 0, 1, tzinfo=ZoneInfo("Europe/Moscow")),
        ]
    )
    with StateStore(configured.state_db) as state:
        handler = RobotLeadHandoff(
            configured,
            state,
            crm=crm,
            transcriber=transcriber,
            now_provider=lambda: next(moments),
        )

        first = handler.run_once(apply=True, lead_id=700)
        second = handler.run_once(apply=True, lead_id=700)
        saved = state.get_robot_handoff(700)

    assert first.errors == 1
    assert second.completed == 1
    assert crm.events == ["phone", "custom", "date", "funnel-failed", "funnel"]
    assert saved["stage_due_date"] == "03.08.2026 23:59"
    assert transcriber.calls == 1


def test_handoff_rejects_non_date_stage_field_before_any_mutation(settings):
    configured = replace(settings, gemini_api_key="test-only-key")

    class WrongDateFieldCrm(FakeCrm):
        def resolve_custom_destination(
            self,
            project_id: int,
            field_name: str,
            field_value: str,
            *,
            project_name: str = "",
        ) -> CrmDestination:
            destination = super().resolve_custom_destination(
                project_id, field_name, field_value, project_name=project_name
            )
            if field_name == "Дата шага":
                destination.field_type = "text"
            return destination

    crm = WrongDateFieldCrm(_lead())
    transcriber = FakeTranscriber(
        TranscriptionResult("ok", "+79991234567", 0.99, 1, "номер +79991234567")
    )
    with StateStore(configured.state_db) as state:
        handler = RobotLeadHandoff(
            configured,
            state,
            crm=crm,
            transcriber=transcriber,
        )

        with pytest.raises(ConfigurationError, match="date или funnel_date"):
            handler.run_once(apply=True, lead_id=700)

    assert crm.events == []
    assert transcriber.calls == 0


def test_transient_transcription_error_is_retried_automatically(settings):
    configured = replace(settings, gemini_api_key="test-only-key")
    crm = FakeCrm(_lead())
    transcriber = FailTranscriptionOnce(
        TranscriptionResult("ok", "+79991234567", 0.99, 1, "номер +79991234567")
    )
    with StateStore(configured.state_db) as state:
        handler = RobotLeadHandoff(
            configured,
            state,
            crm=crm,
            transcriber=transcriber,
        )

        first = handler.run_once(apply=True, lead_id=700)
        second = handler.run_once(apply=True, lead_id=700)

    assert first.errors == 1
    assert first.manual_required == 0
    assert second.completed == 1
    assert transcriber.calls == 2


def test_manual_transcription_result_is_not_retried_without_explicit_override(settings):
    configured = replace(settings, gemini_api_key="test-only-key")
    crm = FakeCrm(_lead())

    class AmbiguousTranscriber:
        def __init__(self) -> None:
            self.calls = 0

        def transcribe(self, _url: str) -> TranscriptionResult:
            self.calls += 1
            raise ManualReviewRequired("ambiguous phone")

    transcriber = AmbiguousTranscriber()
    with StateStore(configured.state_db) as state:
        handler = RobotLeadHandoff(
            configured,
            state,
            crm=crm,
            transcriber=transcriber,
        )

        first = handler.run_once(apply=True, lead_id=700)
        second = handler.run_once(apply=True, lead_id=700)

    assert first.manual_required == 1
    assert second.manual_required == 1
    assert transcriber.calls == 1


def test_completed_lead_is_idempotently_skipped_on_second_run(settings):
    configured = replace(settings, gemini_api_key="test-only-key")
    crm = FakeCrm(_lead())
    transcriber = FakeTranscriber(
        TranscriptionResult("ok", "+79991234567", 0.99, 1, "номер +79991234567")
    )
    with StateStore(configured.state_db) as state:
        handler = RobotLeadHandoff(
            configured,
            state,
            crm=crm,
            transcriber=transcriber,
        )

        first = handler.run_once(apply=True, lead_id=700)
        second = handler.run_once(apply=True, lead_id=700)

    assert first.completed == 1
    assert second.completed == 0
    assert second.skipped == 1
    assert crm.events == ["phone", "custom", "date", "funnel"]
    assert transcriber.calls == 1


def test_gemini_uses_structured_json_and_never_puts_key_in_url(settings):
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "records.example.test":
            return httpx.Response(
                200,
                content=b"fake-audio",
                headers={"content-type": "audio/mpeg"},
            )
        assert request.url.host == "generativelanguage.googleapis.com"
        body = json.loads(request.content)
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "status": "ok",
                                            "phone": "+79991234567",
                                            "confidence": 0.99,
                                            "phone_count": 1,
                                            "transcript": "мой номер +7 999 123-45-67",
                                        }
                                    )
                                }
                            ]
                        }
                    }
                ]
            },
        )

    configured = replace(settings, gemini_api_key="secret-test-key")
    client = httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=False)
    transcriber = GeminiPhoneTranscriber(configured, client=client)

    result = transcriber.transcribe("https://records.example.test/call.mp3")

    assert result.phone == "+79991234567"
    gemini_request = requests[-1]
    assert gemini_request.headers["x-goog-api-key"] == "secret-test-key"
    assert "secret-test-key" not in str(gemini_request.url)


def test_gemini_rejects_result_without_numeric_control_fragment(settings):
    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.host == "records.example.test":
            return httpx.Response(200, content=b"audio", headers={"content-type": "audio/mpeg"})
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "status": "ok",
                                            "phone": "+79991234567",
                                            "confidence": 0.99,
                                            "phone_count": 1,
                                            "transcript": "номер продиктован словами",
                                        }
                                    )
                                }
                            ]
                        }
                    }
                ]
            },
        )

    configured = replace(settings, gemini_api_key="secret-test-key")
    transcriber = GeminiPhoneTranscriber(
        configured,
        client=httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=False),
    )

    with pytest.raises(ManualReviewRequired, match="Контрольный фрагмент"):
        transcriber.transcribe("https://records.example.test/call.mp3")
