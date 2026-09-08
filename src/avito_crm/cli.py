from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from openpyxl import Workbook
from playwright.sync_api import sync_playwright

from avito_crm import __version__
from avito_crm.avito import open_avito_profile
from avito_crm.chrome_extension import ChromeExtensionBrowser, open_ordinary_chrome
from avito_crm.config import Settings
from avito_crm.crm import LpTrackerClient
from avito_crm.errors import AppError, ConfigurationError
from avito_crm.logging_utils import configure_logging
from avito_crm.models import ItemStatus, QueuePatch
from avito_crm.notifications import EmailNotifier, MaxNotifier, TelegramNotifier
from avito_crm.ocr import PhoneOcr
from avito_crm.phone import canonical_avito_url, mask_phone
from avito_crm.pipeline import Pipeline, request_stop
from avito_crm.queue import QueueColumns, build_queue_source
from avito_crm.reporting import format_run_report
from avito_crm.state import SingleInstanceLock, StateStore, utc_now


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="avito-crm",
        description="Последовательный Avito → OCR → LPTracker CRM конвейер",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help=argparse.SUPPRESS)
    parser.add_argument("--verbose", action="store_true", help="Подробный локальный журнал")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Создать безопасный .env и шаблон очереди")

    doctor = subparsers.add_parser("doctor", help="Проверить окружение без записи в CRM")
    _add_source_args(doctor, require_source=False, include_limit=False)
    doctor.add_argument("--online-crm", action="store_true", help="Проверить CRM API и поле")

    crm_check = subparsers.add_parser(
        "crm-check", help="Проверить проект, поле и значение без создания лида"
    )
    crm_check.set_defaults(command="crm-check")

    crm_projects = subparsers.add_parser(
        "crm-projects", help="Показать доступные проекты CRM без изменений данных"
    )
    crm_projects.set_defaults(command="crm-projects")

    subparsers.add_parser("telegram-test", help="Отправить тест основным и резервным получателям")
    subparsers.add_parser("telegram-chats", help="Показать Chat ID людей, написавших боту")
    subparsers.add_parser("email-test", help="Отправить тестовое SMTP-письмо")
    subparsers.add_parser("max-test", help="Отправить тестовое сообщение в MAX")
    subparsers.add_parser("max-recipients", help="Показать ID людей и чатов MAX")
    subparsers.add_parser(
        "avito-profile",
        help="Открыть тот же режим браузера для ручной проверки Avito",
    )
    extension_test = subparsers.add_parser(
        "avito-extension-test",
        help="Получить один номер через расширение Chrome без записи в CRM",
    )
    extension_test.add_argument("url", help="Ссылка на объявление Avito")
    extension_test.add_argument(
        "--max-clicks",
        type=int,
        choices=(1, 2),
        default=1,
        help="Число кликов; для первого теста оставьте 1",
    )

    remote = subparsers.add_parser(
        "remote-control",
        help="Запустить Google Sheets-пульт для удалённых live-запусков",
    )
    remote.add_argument(
        "--setup-only",
        action="store_true",
        help="Только создать/проверить листы пульта и завершиться",
    )
    remote.add_argument(
        "--allow-live-crm",
        action="store_true",
        help="Явно разрешить пульту создавать лиды в CRM",
    )

    cleanup = subparsers.add_parser(
        "cleanup-test-run",
        help="Проверить и удалить из CRM лиды конкретных тестовых запусков",
    )
    cleanup.add_argument(
        "--run-id",
        action="append",
        help="Run ID теста; параметр можно повторить",
    )
    cleanup.add_argument(
        "--row-id",
        action="append",
        help="Точный номер строки; используйте, если Run ID изменил CRM-монитор",
    )
    cleanup.add_argument("--sheet", help="Имя листа Google-очереди")
    cleanup.add_argument(
        "--apply",
        action="store_true",
        help="После предварительного просмотра удалить найденные лиды",
    )
    cleanup.add_argument(
        "--expected-leads",
        type=int,
        default=0,
        help="Обязательное точное число лидов для --apply",
    )

    capture = subparsers.add_parser(
        "capture", help="Только открыть/распознать номера и записать status=captured"
    )
    _add_source_args(capture, require_source=True, include_limit=True)
    capture.add_argument(
        "--interactive-check",
        action="store_true",
        help="Держать браузер открытым и попросить визуально подтвердить распознанный номер",
    )

    run = subparsers.add_parser(
        "run", help="Полный поток; без --live работает как безопасный capture"
    )
    _add_source_args(run, require_source=True, include_limit=True)
    run.add_argument(
        "--live",
        action="store_true",
        help="Разрешить создание лидов в LPTracker (по умолчанию CRM не изменяется)",
    )

    sync = subparsers.add_parser(
        "sync-crm", help="Загрузить уже распознанные номера в CRM без открытия Avito"
    )
    _add_source_args(sync, require_source=True, include_limit=True)
    sync.add_argument("--live", action="store_true", help="Обязательное подтверждение записи")
    sync.add_argument(
        "--require-goal",
        action="store_true",
        help="Вернуть ошибку, если не создано ровно запрошенное число лидов",
    )

    subparsers.add_parser("status", help="Показать итог последнего запуска без телефонов")
    subparsers.add_parser("stop", help="Мягко остановить работающий процесс после текущего шага")
    handoff = subparsers.add_parser(
        "robot-handoff",
        help="Проверить и передать лиды со шага «Лид с робота»",
    )
    handoff.add_argument("--lead-id", help="Один точный CRM ID для контролируемого теста")
    handoff.add_argument("--limit", type=int, default=0, help="Не больше N подходящих лидов")
    handoff.add_argument(
        "--apply",
        action="store_true",
        help="Разрешить замену телефона, тег и перевод шага; без флага только проверка",
    )
    handoff.add_argument(
        "--retry-analysis",
        action="store_true",
        help="Повторно распознать запись, ранее отправленную на ручную проверку",
    )
    return parser


def _add_source_args(
    parser: argparse.ArgumentParser, *, require_source: bool, include_limit: bool
) -> None:
    parser.add_argument(
        "--source",
        choices=("google", "xlsx", "csv"),
        required=require_source,
        help="Источник очереди ссылок",
    )
    parser.add_argument("--file", type=Path, help="Путь к .xlsx/.csv")
    parser.add_argument("--sheet", help="Имя листа (Google Sheets или Excel)")
    if include_limit:
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Цель успешных номеров/лидов; 0 (по умолчанию) — обработать все строки",
        )
        parser.add_argument(
            "--max-inspected",
            type=int,
            default=0,
            help=(
                "Жёсткий предел попыток обработки строк; 0 (по умолчанию) — "
                "без дополнительного предела"
            ),
        )
        parser.add_argument(
            "--retry-manual",
            action="store_true",
            help=argparse.SUPPRESS,
        )


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = Settings.load(args.root)
        settings.ensure_runtime_dirs()
        configure_logging(settings.logs_dir, args.verbose)
        code = _dispatch(args, settings)
    except (AppError, ValueError) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        code = 2
    except Exception as exc:
        print(f"НЕОЖИДАННАЯ ОШИБКА: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        code = 1
    raise SystemExit(code)


def _dispatch(args: argparse.Namespace, settings: Settings) -> int:
    if args.command == "init":
        return _init_files(settings)
    if args.command == "doctor":
        return _doctor(args, settings)
    if args.command == "crm-check":
        return _crm_check(settings)
    if args.command == "crm-projects":
        return _crm_projects(settings)
    if args.command == "telegram-test":
        return _telegram_test(settings)
    if args.command == "telegram-chats":
        return _telegram_chats(settings)
    if args.command == "email-test":
        return _email_test(settings)
    if args.command == "max-test":
        return _max_test(settings)
    if args.command == "max-recipients":
        return _max_recipients(settings)
    if args.command == "avito-profile":
        with SingleInstanceLock(settings.data_dir / "worker.lock"):
            if settings.avito_browser_driver == "chrome_extension":
                open_ordinary_chrome(incognito=settings.avito_extension_incognito)
                mode = "инкогнито" if settings.avito_extension_incognito else "обычном режиме"
                print(f"Avito открыт в Chrome в режиме: {mode}.")
            else:
                open_avito_profile(settings)
                print("Профиль браузера сохранён. Следующий запуск использует это состояние.")
        return 0
    if args.command == "avito-extension-test":
        if settings.avito_browser_driver != "chrome_extension":
            raise ConfigurationError("Сначала выполните scripts\\install-chrome-extension.ps1")
        with SingleInstanceLock(settings.data_dir / "worker.lock"):
            ocr = PhoneOcr(settings.tesseract_cmd, settings.ocr_min_agreement)
            ocr.check_available()
            with ChromeExtensionBrowser(settings, ocr) as browser:
                result = browser.reveal_phone(args.url, "manual-test", max_clicks=args.max_clicks)
        print(f"Номер получен без CRM: {result.phone} ({result.source})")
        return 0
    if args.command == "remote-control":
        from avito_crm.remote_control import run_remote_control

        run_remote_control(
            settings,
            setup_only=bool(args.setup_only),
            allow_live=bool(args.allow_live_crm),
        )
        return 0
    if args.command == "cleanup-test-run":
        return _cleanup_test_runs(args, settings)
    if args.command == "status":
        return _status(settings)
    if args.command == "stop":
        path = request_stop(settings.data_dir)
        print(f"Запрошена мягкая остановка: {path}")
        return 0
    if args.command == "robot-handoff":
        return _robot_handoff(args, settings)
    if args.command == "sync-crm" and not args.live:
        raise ConfigurationError(
            "sync-crm ничего не записал: для создания лидов требуется явный флаг --live"
        )
    if args.command in {"capture", "run", "sync-crm"}:
        source = build_queue_source(settings, args.source, args.file, args.sheet)
        mode = {
            "capture": "capture",
            "run": "full",
            "sync-crm": "crm",
        }[args.command]
        live = bool(getattr(args, "live", False))
        source_name = _source_name(args, settings)
        with (
            SingleInstanceLock(settings.data_dir / "worker.lock"),
            StateStore(settings.state_db) as state,
        ):
            summary = Pipeline(
                settings,
                source,
                state,
                source_name=source_name,
                mode=mode,
                live=live,
                include_manual=args.retry_manual,
                interactive_phone_check=bool(getattr(args, "interactive_check", False)),
            ).run(args.limit, max_inspected=args.max_inspected)
        if live:
            _publish_run_analytics(
                settings,
                summary,
                source_name=source_name,
                retry_manual=bool(args.retry_manual),
            )
        _print_summary(summary, live)
        if getattr(args, "require_goal", False) and args.limit > 0 and summary.created < args.limit:
            print(
                f"ОШИБКА: создано {summary.created} из {args.limit} запрошенных лидов.",
                file=sys.stderr,
            )
            return 4
        if summary.errors:
            return 3
        if summary.manual_required:
            return 5
        return 0
    raise ConfigurationError(f"Неизвестная команда: {args.command}")


def _robot_handoff(args: argparse.Namespace, settings: Settings) -> int:
    from avito_crm.notifications import NotificationRouter
    from avito_crm.robot_handoff import RobotLeadHandoff

    if args.limit < 0 or args.limit > 10:
        raise ConfigurationError("--limit должен быть от 0 до 10")
    limit = args.limit or settings.robot_handoff_batch_size
    notifier = NotificationRouter(settings)

    def manual_notifier(lead_id: str, reason: str) -> None:
        if notifier.enabled:
            notifier.send_robot_handoff_required(lead_id=lead_id, reason=reason)

    try:
        with (
            SingleInstanceLock(settings.data_dir / "worker.lock"),
            StateStore(settings.state_db) as state,
            RobotLeadHandoff(
                settings,
                state,
                manual_notifier=manual_notifier,
            ) as handler,
        ):
            summary = handler.run_once(
                apply=bool(args.apply),
                lead_id=args.lead_id,
                limit=limit,
                retry_analysis=bool(args.retry_analysis),
            )
    finally:
        notifier.close()
    mode = "ИЗМЕНЕНИЯ ПРИМЕНЕНЫ" if args.apply else "ПРЕДВАРИТЕЛЬНАЯ ПРОВЕРКА"
    print(f"Обработка «Лид с робота»: {mode}")
    print(f"  Проверено лидов: {summary.inspected}")
    print(f"  Подходящих: {summary.eligible}")
    print(f"  Завершено: {summary.completed}")
    print(f"  Готово к применению: {summary.ready}")
    print(f"  Нужна ручная проверка: {summary.manual_required}")
    print(f"  Технических ошибок: {summary.errors}")
    for detail in summary.details:
        print(f"  - {detail}")
    if summary.errors:
        return 3
    if summary.manual_required:
        return 5
    return 0


def _init_files(settings: Settings) -> int:
    example = settings.root_dir / ".env.example"
    env_file = settings.root_dir / ".env"
    if not env_file.exists():
        if not example.is_file():
            raise ConfigurationError(f"Не найден шаблон {example}")
        shutil.copyfile(example, env_file)
        print(f"Создан {env_file}; заполните секреты локально.")
    else:
        print(f"Существующий {env_file} сохранён без изменений.")

    sample = settings.root_dir / "queue-template.xlsx"
    if not sample.exists():
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = settings.google_worksheet
        columns = QueueColumns.from_settings(settings)
        sheet.append([columns.url, *columns.managed])
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = "A1:H1"
        sheet.column_dimensions["A"].width = 72
        for letter in "BCDEFGH":
            sheet.column_dimensions[letter].width = 22
        workbook.save(sample)
        workbook.close()
        print(f"Создан шаблон очереди {sample}")
    else:
        print(f"Существующий {sample} сохранён без изменений.")
    return 0


def _doctor(args: argparse.Namespace, settings: Settings) -> int:
    checks: list[tuple[str, str]] = []
    ocr = PhoneOcr(settings.tesseract_cmd, settings.ocr_min_agreement)
    checks.append(("Tesseract OCR", ocr.check_available().splitlines()[0]))
    if settings.avito_browser_driver == "chrome_extension":
        extension_manifest = settings.root_dir / "chrome-extension" / "manifest.json"
        extension_config = settings.root_dir / "chrome-extension" / "config.local.js"
        if not extension_manifest.is_file() or not extension_config.is_file():
            raise ConfigurationError(
                "Расширение обычного Chrome не подготовлено; выполните "
                "scripts\\install-chrome-extension.ps1"
            )
        checks.append(
            (
                "Обычный Chrome",
                f"локальное расширение; мост 127.0.0.1:{settings.avito_extension_port}",
            )
        )
    else:
        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
            if not executable.is_file():
                raise ConfigurationError(
                    "Chromium Playwright не установлен; выполните "
                    "`python -m playwright install chromium`"
                )
            checks.append(("Chromium", str(executable)))
    if args.source:
        source = build_queue_source(settings, args.source, args.file, args.sheet)
        actionable = source.list_actionable()
        checks.append(("Очередь", f"доступна, готово строк: {len(actionable)}"))
    if args.online_crm:
        with LpTrackerClient(settings) as crm:
            destination = crm.resolve_destination()
        checks.append(
            (
                "LPTracker",
                f"проект={destination.project_name}; поле={destination.field_name}; "
                f"значение={settings.lptracker_field_value}",
            )
        )
    print("Проверка окружения:")
    for name, detail in checks:
        print(f"  OK  {name}: {detail}")
    print("Запись в CRM не выполнялась.")
    return 0


def _crm_check(settings: Settings) -> int:
    with LpTrackerClient(settings) as crm:
        destination = crm.resolve_destination()
    print("CRM настроена корректно; данные не изменялись:")
    print(f"  Проект: {destination.project_id} — {destination.project_name}")
    print(f"  Поле: {destination.field_id} — {destination.field_name} ({destination.field_type})")
    print(f"  Значение: {settings.lptracker_field_value}")
    return 0


def _crm_projects(settings: Settings) -> int:
    with LpTrackerClient(settings) as crm:
        projects = crm.list_projects()
    if not projects:
        print("В аккаунте LPTracker нет доступных проектов.")
        return 0
    print("Доступные проекты LPTracker; данные не изменялись:")
    for project in projects:
        print(f"  {project.get('id')} — {project.get('name', '')}")
    print("Скопируйте нужный ID в LPTRACKER_PROJECT_ID локального .env.")
    return 0


def _cleanup_test_runs(args: argparse.Namespace, settings: Settings) -> int:
    run_ids = {str(value).strip() for value in (args.run_id or []) if str(value).strip()}
    row_ids = {str(value).strip() for value in (args.row_id or []) if str(value).strip()}
    if not run_ids and not row_ids:
        raise ConfigurationError("Укажите хотя бы один --run-id или --row-id")
    source = build_queue_source(settings, "google", None, args.sheet)
    candidates = _test_cleanup_candidates(
        source.list_all(), source.columns, run_ids=run_ids, row_ids=row_ids
    )
    unique_lead_ids = {lead_id for _item, lead_id in candidates}

    print("Тестовые лиды, подготовленные к удалению:")
    for item, lead_id in candidates:
        phone = str(item.values.get(source.columns.phone, "") or "")
        print(
            f"  Строка {item.row_id}; CRM ID {lead_id}; "
            f"номер {mask_phone(phone)}; статус {item.status}"
        )
    print(f"Итого уникальных CRM-лидов: {len(unique_lead_ids)}")

    if not args.apply:
        print("Предварительный просмотр: CRM и Google-таблица не изменялись.")
        return 0
    if args.expected_leads <= 0:
        raise ConfigurationError("Для --apply нужен --expected-leads с точным числом")
    if len(unique_lead_ids) != args.expected_leads:
        raise ConfigurationError(
            f"Очистка остановлена: ожидалось {args.expected_leads}, найдено {len(unique_lead_ids)}"
        )
    if len(candidates) != len(unique_lead_ids):
        raise ConfigurationError("Очистка остановлена: CRM ID встречается в нескольких строках")

    source_name = (
        f"google:{settings.google_spreadsheet_id}:{args.sheet or settings.google_worksheet}"
    )
    removed = 0
    with (
        SingleInstanceLock(settings.data_dir / "worker.lock"),
        LpTrackerClient(settings) as crm,
        StateStore(settings.state_db) as state,
    ):
        for item, lead_id in candidates:
            crm.delete_lead(lead_id)
            patch = QueuePatch(
                status=ItemStatus.DONE,
                attempts=item.attempts,
                phone=str(item.values.get(source.columns.phone, "") or ""),
                crm_lead_id=lead_id,
                error="Тестовый лид удалён до начала звонков",
                processed_at=utc_now(),
                run_id=str(item.values.get(source.columns.run_id, "") or ""),
                funnel_stage="Тестовый лид удалён",
                crm_create_count=1,
                repeat_crm_lead_id=str(
                    item.values.get(source.columns.repeat_crm_lead_id, "") or ""
                ),
                repeat_phone_attempts=_safe_int(
                    item.values.get(source.columns.repeat_phone_attempts)
                ),
                next_retry_at="",
            )
            source.update(item, patch)
            state.record_item(canonical_avito_url(item.url), source_name, item, patch)
            removed += 1
            print(f"  OK: CRM ID {lead_id} удалён; строка {item.row_id} закрыта.")
    print(f"Готово: удалено тестовых лидов: {removed}.")
    return 0


def _test_cleanup_candidates(items, columns, *, run_ids: set[str], row_ids: set[str]):
    allowed_statuses = {
        ItemStatus.CRM_MONITORING.value,
        ItemStatus.PROCESSING.value,
        ItemStatus.DONE.value,
    }
    result = []
    for item in items:
        item_run_id = str(item.values.get(columns.run_id, "") or "").strip()
        if item_run_id not in run_ids and str(item.row_id).strip() not in row_ids:
            continue
        lead_id = str(item.values.get(columns.crm_lead_id, "") or "").strip()
        create_count = _safe_int(item.values.get(columns.crm_create_count))
        repeat_lead_id = str(item.values.get(columns.repeat_crm_lead_id, "") or "").strip()
        if repeat_lead_id or create_count > 1:
            raise ConfigurationError(
                f"Строка {item.row_id} содержит повторный лид; "
                "автоматическая тестовая очистка остановлена"
            )
        status = str(item.status or "").strip().casefold()
        if lead_id and create_count == 1 and status in allowed_statuses:
            result.append((item, lead_id))
    return result


def _safe_int(value: object) -> int:
    try:
        return int(float(str(value or "0").strip()))
    except (TypeError, ValueError):
        return 0


def _telegram_test(settings: Settings) -> int:
    with TelegramNotifier(settings) as notifier:
        if not notifier.enabled:
            raise ConfigurationError(
                "Для проверки укажите TELEGRAM_BOT_TOKEN и TELEGRAM_PRIMARY_CHAT_IDS"
            )
        sent = notifier.send_test()
    print(f"OK: тестовое Telegram-сообщение доставлено, чатов: {sent}.")
    return 0


def _telegram_chats(settings: Settings) -> int:
    with TelegramNotifier(settings) as notifier:
        chats = notifier.recent_chats()
    if not chats:
        print(
            "Chat ID пока не найдены. Каждый получатель должен открыть бота, "
            "нажать Start или отправить /start, затем повторить поиск."
        )
        return 0
    print("Найденные Telegram-чаты (скопируйте нужный Chat ID в настройки):")
    for chat in chats:
        print(f"  {chat.chat_id} — {chat.label}")
    return 0


def _email_test(settings: Settings) -> int:
    with EmailNotifier(settings) as notifier:
        if not notifier.enabled:
            raise ConfigurationError(
                "Для проверки укажите SMTP-логин, пароль приложения и основной email"
            )
        sent = notifier.send_test()
    print(f"OK: тестовое email-сообщение доставлено, получателей: {sent}.")
    return 0


def _max_test(settings: Settings) -> int:
    with MaxNotifier(settings) as notifier:
        if not notifier.enabled:
            raise ConfigurationError("Для проверки укажите MAX_BOT_TOKEN и MAX_PRIMARY_RECIPIENTS")
        sent = notifier.send_test()
    print(f"OK: тестовое MAX-сообщение доставлено, получателей: {sent}.")
    return 0


def _max_recipients(settings: Settings) -> int:
    with MaxNotifier(settings) as notifier:
        recipients = notifier.recent_recipients()
    if not recipients:
        print(
            "MAX ID пока не найдены. Каждый получатель должен открыть бота, "
            "нажать «Начать» или отправить сообщение, затем повторить поиск."
        )
        return 0
    print("Найденные получатели MAX (скопируйте ID в настройки):")
    for recipient in recipients:
        print(f"  {recipient.target} — {recipient.label}")
    return 0


def _status(settings: Settings) -> int:
    if not settings.state_db.exists():
        print("Запусков ещё не было.")
        return 0
    with StateStore(settings.state_db) as state:
        latest = state.latest_run()
        totals = state.totals()
    print("Последний запуск:")
    print(json.dumps(latest, ensure_ascii=False, indent=2) if latest else "  нет данных")
    print("Итоги очереди:")
    print(json.dumps(totals, ensure_ascii=False, indent=2))
    return 0


def _source_name(args: argparse.Namespace, settings: Settings) -> str:
    if args.source == "google":
        return f"google:{settings.google_spreadsheet_id}:{args.sheet or settings.google_worksheet}"
    return f"{args.source}:{Path(args.file).resolve()}:{args.sheet or settings.google_worksheet}"


def _publish_run_analytics(
    settings: Settings,
    summary,
    *,
    source_name: str,
    retry_manual: bool,
) -> None:
    if settings.google_credentials_file is None or not settings.google_spreadsheet_id:
        return
    try:
        from avito_crm.remote_control import GoogleControlPanel

        panel = GoogleControlPanel.connect(settings)
        panel.ensure_layout()
        panel.record_pipeline_summary(
            summary,
            source_label=source_name.split(":", 1)[0],
            retry_manual=retry_manual,
        )
    except Exception as exc:
        print(
            "ПРЕДУПРЕЖДЕНИЕ: запуск завершён, но аналитика Google не обновлена "
            f"({exc.__class__.__name__}).",
            file=sys.stderr,
        )


def _print_summary(summary, live: bool) -> None:
    print(f"\n{format_run_report(summary, reason=summary.stopped_reason)}")
    if not live:
        print("  CRM не изменялась (без --live).")
