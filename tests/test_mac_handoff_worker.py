from pathlib import Path
from types import SimpleNamespace

import pytest

from avito_crm import mac_handoff_worker
from avito_crm.errors import ConfigurationError
from avito_crm.robot_handoff import HandoffSummary


def test_parser_defaults_to_continuous_preview_mode():
    args = mac_handoff_worker.build_parser().parse_args([])

    assert args.apply is False
    assert args.once is False
    assert args.lead_id is None


def test_check_config_does_not_open_state_or_call_crm(tmp_path, monkeypatch, capsys):
    settings = SimpleNamespace(data_dir=tmp_path / "data", logs_dir=tmp_path / "logs")
    monkeypatch.setattr(mac_handoff_worker, "_load_settings", lambda _root: settings)
    monkeypatch.setattr(
        mac_handoff_worker,
        "run_handoff_cycle",
        lambda *_args, **_kwargs: pytest.fail("CRM не должна вызываться"),
    )

    assert mac_handoff_worker.main(["--root", str(tmp_path), "--check-config"]) == 0
    assert "настройки Mac-worker проверены" in capsys.readouterr().out
    assert not (tmp_path / "data" / "mac-handoff-worker.lock").exists()


def test_main_once_applies_one_cycle_with_an_independent_lock(tmp_path, monkeypatch):
    settings = SimpleNamespace(data_dir=tmp_path / "data", logs_dir=tmp_path / "logs")
    calls = []
    monkeypatch.setattr(mac_handoff_worker, "_load_settings", lambda _root: settings)
    monkeypatch.setattr(mac_handoff_worker, "configure_logging", lambda *_args: None)

    def fake_cycle(received, *, apply, lead_id, retry_analysis):
        calls.append((received, apply, lead_id, retry_analysis))
        return HandoffSummary(inspected=1, eligible=1, completed=1)

    monkeypatch.setattr(mac_handoff_worker, "run_handoff_cycle", fake_cycle)

    exit_code = mac_handoff_worker.main(
        ["--root", str(tmp_path), "--once", "--apply", "--lead-id", "123"]
    )

    assert exit_code == 0
    assert calls == [(settings, True, "123", False)]
    assert not (tmp_path / "data" / "mac-handoff-worker.lock").exists()


def test_main_once_returns_failure_when_cycle_reports_errors(tmp_path, monkeypatch):
    settings = SimpleNamespace(data_dir=tmp_path / "data", logs_dir=tmp_path / "logs")
    monkeypatch.setattr(mac_handoff_worker, "_load_settings", lambda _root: settings)
    monkeypatch.setattr(mac_handoff_worker, "configure_logging", lambda *_args: None)
    monkeypatch.setattr(
        mac_handoff_worker,
        "run_handoff_cycle",
        lambda *_args, **_kwargs: HandoffSummary(errors=1),
    )

    assert mac_handoff_worker.main(["--root", str(tmp_path), "--once"]) == 1


def test_load_settings_refuses_a_disabled_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOT_HANDOFF_ENABLED", "false")

    with pytest.raises(ConfigurationError, match="Mac-worker выключен"):
        mac_handoff_worker._load_settings(Path(tmp_path))


def test_mac_install_scripts_keep_secrets_out_of_launch_agent():
    root = Path(__file__).parents[1]
    installer = (root / "scripts" / "install-mac-handoff-worker.sh").read_text()
    exporter = (root / "scripts" / "export-mac-handoff-settings.ps1").read_text(
        encoding="utf-8-sig"
    )

    assert "--no-deps" in installer
    assert 'else sys.exit(0)' in installer
    assert 'else None' not in installer
    assert "--check-config" in installer
    assert "avito_crm.mac_handoff_worker" in installer
    assert '"KeepAlive": True' in installer
    assert '"/usr/bin/caffeinate"' in installer
    assert '"--apply"' in installer
    plist_section = installer.split("payload = {", maxsplit=1)[-1]
    assert "GEMINI_API_KEY" not in plist_section
    assert "GOOGLE_CREDENTIALS_FILE" not in exporter
    assert "AVITO_EXTENSION_TOKEN" not in exporter
    assert '"ROBOT_HANDOFF_ENABLED=true"' in exporter
