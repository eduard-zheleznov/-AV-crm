import pytest

from avito_crm import cli
from avito_crm.cli import build_parser
from avito_crm.models import QueueItem
from avito_crm.queue import QueueColumns


def test_run_requires_limit_and_accepts_live():
    args = build_parser().parse_args(
        [
            "run",
            "--source",
            "google",
            "--limit",
            "10",
            "--max-inspected",
            "12",
            "--live",
        ]
    )
    assert args.command == "run"
    assert args.limit == 10
    assert args.max_inspected == 12
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


def test_cleanup_test_run_is_preview_only_by_default():
    args = build_parser().parse_args(
        [
            "cleanup-test-run",
            "--run-id",
            "first-run",
            "--run-id",
            "volume-run",
        ]
    )

    assert args.run_id == ["first-run", "volume-run"]
    assert args.apply is False
    assert args.expected_leads == 0


def test_cleanup_candidates_include_only_created_active_test_leads(settings):
    columns = QueueColumns.from_settings(settings)
    created = QueueItem(
        row_id="503",
        url="https://www.avito.ru/moskva/test_123456789",
        status="crm_monitoring",
        attempts=1,
        values={
            columns.run_id: "test-run",
            columns.crm_lead_id: "777001",
            columns.crm_create_count: "1",
            columns.repeat_crm_lead_id: "",
        },
    )
    retry_without_lead = QueueItem(
        row_id="504",
        url="https://www.avito.ru/moskva/test_223456789",
        status="retry_phone",
        attempts=1,
        values={columns.run_id: "test-run", columns.crm_create_count: "0"},
    )
    unrelated = QueueItem(
        row_id="505",
        url="https://www.avito.ru/moskva/test_323456789",
        status="crm_monitoring",
        attempts=1,
        values={
            columns.run_id: "other-run",
            columns.crm_lead_id: "777002",
            columns.crm_create_count: "1",
        },
    )

    candidates = cli._test_cleanup_candidates(
        [created, retry_without_lead, unrelated],
        columns,
        run_ids={"test-run"},
        row_ids=set(),
    )

    assert candidates == [(created, "777001")]


def test_cleanup_can_target_exact_row_after_crm_monitor_changed_run_id(settings):
    columns = QueueColumns.from_settings(settings)
    created = QueueItem(
        row_id="503",
        url="https://www.avito.ru/moskva/test_423456789",
        status="done",
        attempts=1,
        values={
            columns.run_id: "crm-monitor",
            columns.crm_lead_id: "777003",
            columns.crm_create_count: "1",
            columns.repeat_crm_lead_id: "",
        },
    )

    candidates = cli._test_cleanup_candidates([created], columns, run_ids=set(), row_ids={"503"})

    assert candidates == [(created, "777003")]
