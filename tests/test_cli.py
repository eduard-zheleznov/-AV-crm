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
