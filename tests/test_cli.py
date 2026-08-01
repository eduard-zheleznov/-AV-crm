import pytest

from avito_crm import cli
from avito_crm.cli import build_parser


def test_run_requires_limit_and_accepts_live():
    args = build_parser().parse_args(["run", "--source", "google", "--limit", "10", "--live"])
    assert args.command == "run"
    assert args.limit == 10
    assert args.live is True


def test_first_test_commands_are_available():
    projects = build_parser().parse_args(["crm-projects"])
    capture = build_parser().parse_args(
        [
            "capture",
            "--source",
            "xlsx",
            "--file",
            "test.xlsx",
            "--limit",
            "1",
            "--interactive-check",
        ]
    )

    assert projects.command == "crm-projects"
    assert capture.interactive_check is True

    sync = build_parser().parse_args(
        [
            "sync-crm",
            "--source",
            "xlsx",
            "--file",
            "test.xlsx",
            "--limit",
            "1",
            "--live",
            "--require-goal",
        ]
    )
    assert sync.require_goal is True


def test_notification_setup_commands_are_available():
    assert build_parser().parse_args(["max-test"]).command == "max-test"
    assert build_parser().parse_args(["max-recipients"]).command == "max-recipients"
    assert build_parser().parse_args(["email-test"]).command == "email-test"


def test_avito_profile_command_is_available_without_login_arguments():
    args = build_parser().parse_args(["avito-profile"])

    assert args.command == "avito-profile"


def test_safe_extension_test_command_is_available():
    args = build_parser().parse_args(
        ["avito-extension-test", "https://www.avito.ru/moskva/test_123"]
    )

    assert args.command == "avito-extension-test"
    assert args.max_clicks == 1


def test_avito_profile_starts_even_when_crm_timezone_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("LPTRACKER_TIMEZONE", "Missing/Timezone")
    opened_profiles = []
    monkeypatch.setattr(cli, "open_avito_profile", opened_profiles.append)

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--root", str(tmp_path), "avito-profile"])

    assert exit_info.value.code == 0
    assert len(opened_profiles) == 1


def test_remote_control_requires_an_explicit_live_flag_at_runtime():
    setup = build_parser().parse_args(["remote-control", "--setup-only"])
    live = build_parser().parse_args(["remote-control", "--allow-live-crm"])

    assert setup.setup_only is True
    assert setup.allow_live_crm is False
    assert live.allow_live_crm is True
