from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from openpyxl import Workbook
from playwright.sync_api import sync_playwright

from avito_crm import __version__
from avito_crm.config import Settings
from avito_crm.crm import LpTrackerClient
from avito_crm.errors import AppError, ConfigurationError
from avito_crm.logging_utils import configure_logging
from avito_crm.ocr import PhoneOcr
from avito_crm.pipeline import Pipeline, request_stop
from avito_crm.queue import QueueColumns, build_queue_source
from avito_crm.state import SingleInstanceLock, StateStore


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

    subparsers.add_parser("status", help="Показать итог последнего запуска без телефонов")
    subparsers.add_parser("stop", help="Мягко остановить работающий процесс после текущего шага")
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
            required=True,
            help="Сколько успешных номеров/лидов получить до остановки",
        )
        parser.add_argument(
            "--retry-manual",
            action="store_true",
            help="Повторить строки manual_required после ручной проверки",
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
    if args.command == "status":
        return _status(settings)
    if args.command == "stop":
        path = request_stop(settings.data_dir)
        print(f"Запрошена мягкая остановка: {path}")
        return 0
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
            ).run(args.limit)
        _print_summary(summary, live)
        return 0 if summary.errors == 0 and summary.manual_required == 0 else 3
    raise ConfigurationError(f"Неизвестная команда: {args.command}")


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


def _print_summary(summary, live: bool) -> None:
    print("\nИтог запуска:")
    print(f"  Run ID: {summary.run_id}")
    print(f"  Проверено строк: {summary.inspected}")
    print(f"  Номеров распознано: {summary.captured}")
    print(f"  Лидов создано: {summary.created}")
    print(f"  Дубликатов: {summary.duplicates}")
    print(f"  Ошибок: {summary.errors}")
    print(f"  Требуют ручного действия: {summary.manual_required}")
    print(f"  Остановка: {summary.stopped_reason}")
    if not live:
        print("  CRM не изменялась (без --live).")
