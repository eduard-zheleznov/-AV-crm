from __future__ import annotations

import json
import os
import zipfile

from avito_crm import diagnostics


def test_missing_configuration_still_produces_readable_report(tmp_path):
    archive = diagnostics.save_report(tmp_path)
    with zipfile.ZipFile(archive) as bundle:
        assert set(bundle.namelist()) == {"report.json", "README.txt"}
        report = json.loads(bundle.read("report.json"))
        assert report["configuration"]["env_present"] is False
        assert report["controller"]["process_running"] is False
        assert report["bridge"]["state"] == "not_used"
    assert diagnostics.save_report(tmp_path) != archive


def test_no_raw_secrets_or_personal_data_are_exported(tmp_path):
    secret = "synthetic-private-sentinel"
    (tmp_path / ".env").write_text(f"CRM_PASSWORD={secret}\nEMAIL_PRIMARY_TO=person@example.com\n")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "avito-crm.log").write_text(
        f"2026-10-09 15:00:00,123 ERROR: нет ответа расширения {secret} +79991234567\n"
        f"2026-10-09 15:01:00,123 ERROR: arbitrary secret {secret}\n"
    )
    (logs / "chrome-extension-health.jsonl").write_text(
        json.dumps(
            {
                "timestamp": "2026-10-09T15:00:00+00:00",
                "event": "extension_result",
                "status": "phone",
                "phone": "+79991234567",
                "reason": secret,
                "url": "https://private.invalid/" + secret,
                "command_id": secret,
                "diagnostics": {"stage": secret, "token": secret},
            }
        )
        + "\ncorrupted JSON\n"
    )
    with zipfile.ZipFile(diagnostics.save_report(tmp_path)) as bundle:
        exported = bundle.read("report.json").decode()
        assert secret not in exported
        assert "+79991234567" not in exported
        assert "person@example.com" not in exported
        assert "private.invalid" not in exported
        report = json.loads(exported)
        assert report["recent_issues"][0]["code"] == "extension_no_reply"
        assert report["recent_issues"][1]["code"] == "other_error"
        assert "stage" not in report["chrome_events"][0]


def test_process_status_and_known_stage_are_collected_read_only(tmp_path):
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    data.mkdir()
    logs.mkdir()
    lock = data / "worker.lock"
    payload = json.dumps({"pid": os.getpid(), "started_at": "2026-10-09T15:00:00+00:00"})
    lock.write_text(payload)
    (logs / "chrome-extension-health.jsonl").write_text(
        json.dumps(
            {
                "event": "extension_status",
                "status": "command_progress",
                "row_id": "2387",
                "diagnostics": {"stage": "tabs.update"},
            }
        )
    )
    report = diagnostics.collect_report(tmp_path)
    assert report["worker"]["process_running"] is True
    assert report["chrome_events"][0]["stage"] == "tabs.update"
    assert lock.read_text() == payload
    assert not (data / "STOP").exists()


def test_invalid_and_unavailable_local_bridge_are_safe(monkeypatch):
    assert diagnostics._bridge({"AVITO_EXTENSION_PORT": "oops"})["state"] == "unavailable"
    assert diagnostics._bridge({})["state"] == "not_configured"

    def fail(_handler):
        raise OSError("secret exception detail must not be exported")

    monkeypatch.setattr(diagnostics.urllib.request, "build_opener", fail)
    assert diagnostics._bridge({"AVITO_EXTENSION_TOKEN": "synthetic"}) == {"state": "unavailable"}


def test_live_health_uses_only_loopback_without_proxies(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, size):
            assert size == 65537
            return json.dumps(
                {
                    "connected": True,
                    "extension_version": "1.0.33",
                    "unexpected_secret": "must-not-export",
                }
            ).encode()

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "http://127.0.0.1:8765/v1/health"
            assert timeout == 2
            return Response()

    def opener(handler):
        assert handler.proxies == {}
        return Opener()

    monkeypatch.setattr(diagnostics.urllib.request, "build_opener", opener)
    health = diagnostics._bridge({"AVITO_EXTENSION_TOKEN": "synthetic"})
    assert health["extension_version"] == "1.0.33"
    assert "must-not-export" not in json.dumps(health)


def test_corrupted_runtime_files_do_not_prevent_report(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "worker.lock").write_text("bad JSON")
    (data / "remote-control.json").write_text("[]")
    report = diagnostics.collect_report(tmp_path)
    assert report["worker"]["file_present"] is True
    assert report["worker"]["process_running"] is False
    assert report["pending_command"] is False
