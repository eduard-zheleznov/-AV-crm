from avito_crm.cli import build_parser


def test_run_requires_limit_and_accepts_live():
    args = build_parser().parse_args(["run", "--source", "google", "--limit", "10", "--live"])
    assert args.command == "run"
    assert args.limit == 10
    assert args.live is True
