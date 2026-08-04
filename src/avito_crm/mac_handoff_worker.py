from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from avito_crm.config import Settings
from avito_crm.errors import AppError, ConfigurationError, InstanceAlreadyRunning
from avito_crm.logging_utils import configure_logging
from avito_crm.notifications import NotificationRouter
from avito_crm.robot_handoff import HandoffSummary, RobotLeadHandoff
from avito_crm.state import SingleInstanceLock, StateStore

LOGGER = logging.getLogger(__name__)
MAX_ERROR_BACKOFF_SECONDS = 600.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="avito-crm-handoff",
        description=(
            "Лёгкий фоновый обработчик LPTracker «Лид с робота» для macOS. "
            "Браузер и Avito не запускаются."
        ),
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Применить безопасные изменения в CRM; без флага выполняется проверка",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Выполнить один цикл и завершиться",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Проверить локальные настройки без запросов в CRM и выйти",
    )
    parser.add_argument("--lead-id", help="Проверить только один лид")
    parser.add_argument(
        "--retry-analysis",
        action="store_true",
        help="Повторно распознать запись после статуса ручной проверки",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def run_handoff_cycle(
    settings: Settings,
    *,
    apply: bool,
    lead_id: str | None = None,
    retry_analysis: bool = False,
) -> HandoffSummary:
    notifier = NotificationRouter(settings)

    def manual_notifier(item_id: str, reason: str) -> None:
        if notifier.enabled:
            notifier.send_robot_handoff_required(lead_id=item_id, reason=reason)

    try:
        with (
            StateStore(settings.state_db) as store,
            RobotLeadHandoff(
                settings,
                store,
                manual_notifier=manual_notifier,
            ) as handler,
        ):
            return handler.run_once(
                apply=apply,
                lead_id=lead_id,
                retry_analysis=retry_analysis,
            )
    finally:
        notifier.close()


def _load_settings(root: Path) -> Settings:
    settings = Settings.load(root, refresh_env=True)
    if not settings.robot_handoff_enabled:
        raise ConfigurationError(
            "Mac-worker выключен: задайте ROBOT_HANDOFF_ENABLED=true в его локальном .env"
        )
    settings.require_crm()
    settings.ensure_runtime_dirs()
    return settings


def _log_summary(summary: HandoffSummary, *, apply: bool) -> None:
    mode = "ПРИМЕНЕНИЕ" if apply else "ПРОВЕРКА"
    LOGGER.info(
        "Цикл «Лид с робота» (%s): проверено=%s, подходит=%s, обработано=%s, "
        "готово=%s, ручная проверка=%s, ошибок=%s",
        mode,
        summary.inspected,
        summary.eligible,
        summary.completed,
        summary.ready,
        summary.manual_required,
        summary.errors,
    )
    for detail in summary.details:
        LOGGER.info("- %s", detail)


def _run_once(args: argparse.Namespace, settings: Settings) -> int:
    summary = run_handoff_cycle(
        settings,
        apply=args.apply,
        lead_id=args.lead_id,
        retry_analysis=args.retry_analysis,
    )
    _log_summary(summary, apply=args.apply)
    return 1 if summary.errors else 0


def _run_forever(args: argparse.Namespace, settings: Settings) -> int:
    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    error_backoff = 30.0
    LOGGER.info(
        "Mac-worker «Лид с робота» запущен; опрос каждые %.0f сек.; режим=%s",
        settings.robot_handoff_poll_seconds,
        "CRM" if args.apply else "проверка",
    )
    while not stop_event.is_set():
        delay = settings.robot_handoff_poll_seconds
        try:
            # Reload the local file so a key or safe setting can be changed
            # without reinstalling the LaunchAgent.
            settings = _load_settings(args.root)
            summary = run_handoff_cycle(
                settings,
                apply=args.apply,
                lead_id=args.lead_id,
                retry_analysis=args.retry_analysis,
            )
            _log_summary(summary, apply=args.apply)
            error_backoff = 30.0
        except ConfigurationError:
            raise
        except Exception as exc:
            LOGGER.exception(
                "Цикл Mac-worker завершился ошибкой (%s); повтор через %.0f сек.",
                exc.__class__.__name__,
                error_backoff,
            )
            delay = error_backoff
            error_backoff = min(error_backoff * 2, MAX_ERROR_BACKOFF_SECONDS)
        stop_event.wait(delay)
    LOGGER.info("Mac-worker «Лид с робота» безопасно остановлен")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.root = args.root.expanduser().resolve()
    try:
        settings = _load_settings(args.root)
        if args.check_config:
            print("ГОТОВО: настройки Mac-worker проверены")
            return 0
        configure_logging(settings.logs_dir, args.verbose)
        lock_path = settings.data_dir / "mac-handoff-worker.lock"
        with SingleInstanceLock(lock_path):
            if args.once:
                return _run_once(args, settings)
            return _run_forever(args, settings)
    except InstanceAlreadyRunning as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 3
    except (AppError, ConfigurationError, ValueError) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        LOGGER.exception("Mac-worker аварийно завершился")
        print(f"НЕОЖИДАННАЯ ОШИБКА: {exc.__class__.__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
