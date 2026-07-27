from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import tomllib
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from avito_crm import __version__
from avito_crm.config import Settings
from avito_crm.errors import ConfigurationError, InstanceAlreadyRunning, SourceError
from avito_crm.models import ItemStatus, RunSummary
from avito_crm.pipeline import Pipeline, request_stop
from avito_crm.queue import build_queue_source
from avito_crm.state import SingleInstanceLock, StateStore, utc_now

LOGGER = logging.getLogger(__name__)

CONTROL_MARKER = "AVITO CRM — УДАЛЁННЫЙ ПУЛЬТ"
ANALYTICS_MARKER_V1 = "AVITO CRM — АНАЛИТИКА"
ANALYTICS_MARKER = "AVITO CRM — АНАЛИТИКА v2"
ANALYTICS_WORKSHEET = "Аналитика"
LEGACY_HISTORY_HEADERS = (
    "Команда ID",
    "Лимит",
    "Лист очереди",
    "Повтор manual",
    "Запущено",
    "Завершено",
    "Статус",
    "Создано лидов",
    "Дубликатов",
    "Ошибок",
    "Сообщение",
    "Компьютер",
)
HISTORY_HEADERS = (
    "Команда ID",
    "Лимит",
    "Лист очереди",
    "Повторить после ручной проверки",
    "Запущено",
    "Завершено",
    "Статус",
    "Создано лидов",
    "Дубликатов",
    "Технических ошибок",
    "Сообщение",
    "Компьютер",
    "Обработано ссылок",
    "Всего попыток",
    "Номеров открыто",
    "Неактивных",
    "Без кнопки телефона",
    "Не открыто после попыток",
    "Ручная проверка",
    "Повторных попыток",
    "Некорректных ссылок",
    "Дата завершения",
    "Строк со статусом «Недозвон»",
    "Предупреждений синхронизации CRM",
)


class _ControllerReloadRequired(RuntimeError):
    """Signal a clean Scheduled Task restart after an on-disk application update."""


@dataclass(frozen=True, slots=True)
class PanelCommand:
    start: bool
    stop: bool
    limit: int
    worksheet: str
    retry_manual: bool


@dataclass(slots=True)
class CommandState:
    command_id: str
    target: int
    worksheet: str
    retry_manual: bool
    phase: str
    started_at: str
    history_row: int = 0
    stop_requested: bool = False
    base_created: int = 0
    base_captured: int = 0
    base_duplicates: int = 0
    base_errors: int = 0
    base_invalid: int = 0
    base_inactive: int = 0
    base_unavailable: int = 0
    base_phone_failed: int = 0
    base_retries: int = 0
    base_manual_required: int = 0
    base_processed: int = 0
    base_inspected: int = 0
    base_no_answer_synced: int = 0
    result: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CommandState:
        fields = cls.__dataclass_fields__
        return cls(**{name: value[name] for name in fields if name in value})


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    created: int = 0
    captured: int = 0
    duplicates: int = 0
    errors: int = 0
    invalid: int = 0
    inactive: int = 0
    unavailable: int = 0
    phone_failed: int = 0
    retries: int = 0
    manual_required: int = 0
    processed: int = 0
    inspected: int = 0
    no_answer_synced: int = 0
    crm_sync_errors: int = 0
    row_id: str = ""


@dataclass(frozen=True, slots=True)
class WorkerResult:
    kind: str
    summary: RunSummary | None = None
    message: str = ""


class GoogleControlPanel:
    """Small, explicit Google Sheet UI. It never stores application secrets."""

    def __init__(self, settings: Settings, spreadsheet: Any, worksheet_not_found: type[Exception]):
        self.settings = settings
        self.spreadsheet = spreadsheet
        self.worksheet_not_found = worksheet_not_found
        self.control: Any | None = None
        self.history: Any | None = None
        self.analytics: Any | None = None
        self._analytics_period: int | None = None

    @classmethod
    def connect(cls, settings: Settings) -> GoogleControlPanel:
        if settings.google_credentials_file is None:
            raise ConfigurationError("Не задан GOOGLE_CREDENTIALS_FILE")
        if not settings.google_credentials_file.is_file():
            raise ConfigurationError(
                f"Файл Google service account не найден: {settings.google_credentials_file}"
            )
        if not settings.google_spreadsheet_id:
            raise ConfigurationError("Не задан GOOGLE_SPREADSHEET_ID")
        try:
            import gspread

            client = gspread.service_account(filename=str(settings.google_credentials_file))
            spreadsheet = client.open_by_key(settings.google_spreadsheet_id)
            return cls(settings, spreadsheet, gspread.WorksheetNotFound)
        except (ConfigurationError, SourceError):
            raise
        except Exception as exc:
            raise SourceError(f"Не удалось открыть Google-таблицу: {exc}") from exc

    def ensure_layout(self) -> None:
        self.control = self._worksheet_or_create(
            self.settings.google_control_worksheet, rows=30, cols=8
        )
        self.history = self._worksheet_or_create(
            self.settings.google_history_worksheet, rows=1000, cols=len(HISTORY_HEADERS)
        )
        self.analytics = self._worksheet_or_create(ANALYTICS_WORKSHEET, rows=110, cols=8)
        try:
            marker = self._cell(self.control.get("A1:F12"), 1, 1)
            if marker != CONTROL_MARKER:
                if marker:
                    raise SourceError(
                        f"Лист {self.settings.google_control_worksheet!r} уже занят "
                        "другими данными. Переименуйте его или задайте "
                        "другой GOOGLE_CONTROL_WORKSHEET."
                    )
                self._initialize_control()
            else:
                self._upgrade_control_labels()
                self._apply_sheet_rules(self.control)
            history_values = self.history.get(f"A1:{_column_letter(len(HISTORY_HEADERS))}1")
            history_headers = tuple(history_values[0]) if history_values else ()
            if history_headers and history_headers not in {
                LEGACY_HISTORY_HEADERS,
                HISTORY_HEADERS[:-2],
                HISTORY_HEADERS[:-1],
                HISTORY_HEADERS,
            }:
                raise SourceError(
                    f"Лист {self.settings.google_history_worksheet!r} уже занят "
                    "другими данными. Переименуйте его или задайте "
                    "другой GOOGLE_HISTORY_WORKSHEET."
                )
            with suppress(Exception):
                self.history.resize(
                    rows=max(1000, int(getattr(self.history, "row_count", 0))),
                    cols=len(HISTORY_HEADERS),
                )
            if history_headers != HISTORY_HEADERS:
                self.history.update(
                    [list(HISTORY_HEADERS)],
                    f"A1:{_column_letter(len(HISTORY_HEADERS))}1",
                    value_input_option="RAW",
                )
            self._format_history()
            self._backfill_history_dates()
            analytics_marker = self._cell(self.analytics.get("A1:H2"), 1, 1)
            if analytics_marker and analytics_marker not in {
                ANALYTICS_MARKER_V1,
                ANALYTICS_MARKER,
            }:
                raise SourceError(
                    f"Лист {ANALYTICS_WORKSHEET!r} уже занят другими данными. "
                    "Переименуйте его и повторите настройку пульта."
                )
            if analytics_marker != ANALYTICS_MARKER:
                self._initialize_analytics(add_chart=not bool(analytics_marker))
            self.refresh_analytics(force=True)
        except Exception as exc:
            raise SourceError(f"Не удалось подготовить листы пульта: {exc}") from exc

    def read_command(self) -> PanelCommand:
        control = self._require_control()
        try:
            values = control.get("A1:F12")
            raw_limit = self._cell(values, 6, 2)
            limit = _parse_panel_limit(raw_limit)
            worksheet = self._cell(values, 7, 2).strip() or self.settings.google_worksheet
            return PanelCommand(
                start=_checked(self._cell(values, 4, 2)),
                stop=_checked(self._cell(values, 5, 2)),
                limit=limit,
                worksheet=worksheet,
                retry_manual=_checked(self._cell(values, 8, 2)),
            )
        except (ConfigurationError, SourceError):
            raise
        except Exception as exc:
            raise SourceError(f"Не удалось прочитать пульт: {exc}") from exc

    def claim(self, state: CommandState) -> None:
        self._batch_control(
            {
                "B4": False,
                "E4": "ПРИНЯТО",
                "E5": state.command_id,
                "E6": f"0 / {_target_label(state.target)}",
                "E7": state.started_at,
                "E8": utc_now(),
                "E9": "Команда зафиксирована; запускаем рабочий процесс.",
                "E11": _computer_name(self.settings),
                "E12": __version__,
            }
        )

    def acknowledge_stop(self, *, clear_start: bool = False) -> None:
        values: dict[str, Any] = {"B5": False, "E8": utc_now()}
        if clear_start:
            values["B4"] = False
        self._batch_control(values)

    def reject_start(self, message: str) -> None:
        self._batch_control({"B4": False, "E9": message, "E8": utc_now()})

    def update_active(
        self,
        state: CommandState,
        progress: ProgressSnapshot,
        *,
        status: str = "РАБОТАЕТ",
        message: str = "",
    ) -> None:
        detail = message or "Обрабатываем очередь по одной строке."
        if progress.row_id:
            detail = f"Строка {progress.row_id}. {detail}"
        self._batch_control(
            {
                "E4": status,
                "E5": state.command_id,
                "E6": f"{progress.created} / {_target_label(state.target)}",
                "E7": state.started_at,
                "E8": utc_now(),
                "E9": detail[:500],
                "E11": _computer_name(self.settings),
                "E12": __version__,
            }
        )

    def finish(self, state: CommandState) -> None:
        result = state.result
        self._batch_control(
            {
                "E4": str(result.get("status", "ЗАВЕРШЕНО")),
                "E5": state.command_id,
                "E6": (
                    f"{int(result.get('created', 0))} / {_target_label(state.target)}"
                ),
                "E8": utc_now(),
                "E9": str(result.get("message", ""))[:500],
                "E11": _computer_name(self.settings),
                "E12": __version__,
            }
        )
        self.finish_history(state)

    def heartbeat(self) -> None:
        self._batch_control(
            {"E8": utc_now(), "E11": _computer_name(self.settings), "E12": __version__}
        )

    def append_history(self, state: CommandState) -> int:
        history = self._require_history()
        try:
            existing = history.get_all_values()
            for row_number, row in enumerate(existing, start=1):
                if row and str(row[0]).strip() == state.command_id:
                    return row_number
            row_number = len(existing) + 1
            history.update(
                [
                    [
                        state.command_id,
                        state.target,
                        state.worksheet,
                        "да" if state.retry_manual else "нет",
                        state.started_at,
                        "",
                        "ПРИНЯТО",
                        0,
                        0,
                        0,
                        "",
                        _computer_name(self.settings),
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                        "",
                        0,
                        0,
                    ]
                ],
                f"A{row_number}:X{row_number}",
                value_input_option="RAW",
            )
            return row_number
        except Exception as exc:
            raise SourceError(f"Не удалось записать историю запуска: {exc}") from exc

    def finish_history(self, state: CommandState) -> None:
        if state.history_row < 2:
            return
        result = state.result
        history = self._require_history()
        try:
            history.update(
                [
                    [
                        str(result.get("finished_at", utc_now())),
                        str(result.get("status", "")),
                        int(result.get("created", 0)),
                        int(result.get("duplicates", 0)),
                        int(result.get("errors", 0)),
                        str(result.get("message", ""))[:500],
                        _computer_name(self.settings),
                        int(result.get("processed", 0)),
                        int(result.get("inspected", 0)),
                        int(result.get("captured", 0)),
                        int(result.get("inactive", 0)),
                        int(result.get("unavailable", 0)),
                        int(result.get("phone_failed", 0)),
                        int(result.get("manual_required", 0)),
                        int(result.get("retries", 0)),
                        int(result.get("invalid", 0)),
                        str(result.get("finished_at", utc_now()))[:10],
                        int(result.get("no_answer_synced", 0)),
                        int(result.get("crm_sync_errors", 0)),
                    ]
                ],
                f"F{state.history_row}:X{state.history_row}",
                value_input_option="RAW",
            )
            self.refresh_analytics(force=True)
        except Exception as exc:
            raise SourceError(f"Не удалось завершить запись истории: {exc}") from exc

    def record_pipeline_summary(
        self,
        summary: RunSummary,
        *,
        source_label: str,
        retry_manual: bool,
    ) -> None:
        """Upsert a GUI/CLI run so the analytics sheet includes every production launch."""
        history = self._require_history()
        try:
            existing = history.get_all_values()
            row_number = next(
                (
                    index
                    for index, row in enumerate(existing, start=1)
                    if row and str(row[0]).strip() == summary.run_id
                ),
                len(existing) + 1,
            )
            if summary.errors:
                status = "ЗАВЕРШЕНО С ТЕХНИЧЕСКИМИ ОШИБКАМИ"
            elif summary.manual_required:
                status = "ТРЕБУЕТ ВНИМАНИЯ"
            elif "останов" in summary.stopped_reason.casefold():
                status = "ОСТАНОВЛЕНО"
            elif summary.crm_sync_errors:
                status = "ЗАВЕРШЕНО С ПРЕДУПРЕЖДЕНИЯМИ"
            else:
                status = "ЗАВЕРШЕНО"
            finished_at = utc_now()
            message = (
                f"Создано {summary.created}; открыто номеров {summary.captured}; "
                f"неактивных {summary.inactive}; без кнопки {summary.unavailable}; "
                f"не открыто после попыток {summary.phone_failed}; "
                f"технических ошибок {summary.errors}; "
                f"предупреждений синхронизации CRM {summary.crm_sync_errors}. "
                f"Остановка: {summary.stopped_reason or 'не указана'}."
            )
            row = [
                summary.run_id,
                summary.requested,
                source_label[:145],
                "да" if retry_manual else "нет",
                finished_at,
                finished_at,
                status,
                summary.created,
                summary.duplicates,
                summary.errors,
                message[:500],
                _computer_name(self.settings),
                summary.processed,
                summary.inspected,
                summary.captured,
                summary.inactive,
                summary.unavailable,
                summary.phone_failed,
                summary.manual_required,
                summary.retries,
                summary.invalid,
                finished_at[:10],
                summary.no_answer_synced,
                summary.crm_sync_errors,
            ]
            history.update(
                [row],
                f"A{row_number}:X{row_number}",
                value_input_option="RAW",
            )
            self.refresh_analytics(force=True)
        except Exception as exc:
            raise SourceError(f"Не удалось записать итог запуска в аналитику: {exc}") from exc

    def count_command(self, worksheet: str, command_id: str) -> ProgressSnapshot:
        try:
            sheet = self.spreadsheet.worksheet(worksheet)
            values = sheet.get_all_values()
        except Exception as exc:
            raise SourceError(f"Не удалось прочитать лист {worksheet!r}: {exc}") from exc
        if not values:
            return ProgressSnapshot()
        headers = [str(value).strip() for value in values[0]]
        if self.settings.run_id_column not in headers or self.settings.status_column not in headers:
            return ProgressSnapshot()
        run_index = headers.index(self.settings.run_id_column)
        status_index = headers.index(self.settings.status_column)
        attempts_index = (
            headers.index(self.settings.attempts_column)
            if self.settings.attempts_column in headers
            else -1
        )
        funnel_stage_index = (
            headers.index(self.settings.funnel_stage_column)
            if self.settings.funnel_stage_column in headers
            else -1
        )
        counts: dict[str, int] = {}
        attempts = 0
        processed = 0
        no_answer_synced = 0
        for row in values[1:]:
            run_value = row[run_index].strip() if run_index < len(row) else ""
            if run_value != command_id:
                continue
            processed += 1
            status = row[status_index].strip().lower() if status_index < len(row) else ""
            counts[status] = counts.get(status, 0) + 1
            if funnel_stage_index >= 0 and funnel_stage_index < len(row):
                normalized_stage = " ".join(row[funnel_stage_index].split()).casefold()
                if normalized_stage in {
                    " ".join(
                        self.settings.lptracker_no_answer_funnel_name.split()
                    ).casefold(),
                    "недозвон",
                    "не дозвон",
                }:
                    no_answer_synced += 1
            if attempts_index >= 0 and attempts_index < len(row):
                with suppress(ValueError):
                    attempts += max(0, int(float(row[attempts_index] or 0)))
        return ProgressSnapshot(
            created=counts.get(ItemStatus.DONE.value, 0),
            captured=(
                counts.get(ItemStatus.CAPTURED.value, 0)
                + counts.get(ItemStatus.DONE.value, 0)
                + counts.get(ItemStatus.DUPLICATE.value, 0)
            ),
            duplicates=counts.get(ItemStatus.DUPLICATE.value, 0),
            errors=counts.get(ItemStatus.ERROR.value, 0),
            invalid=counts.get(ItemStatus.INVALID.value, 0),
            inactive=counts.get(ItemStatus.INACTIVE.value, 0),
            unavailable=counts.get(ItemStatus.UNAVAILABLE.value, 0),
            phone_failed=counts.get(ItemStatus.NO_PHONE.value, 0),
            retries=max(0, attempts - processed),
            manual_required=counts.get(ItemStatus.MANUAL_REQUIRED.value, 0),
            processed=processed,
            inspected=attempts,
            no_answer_synced=no_answer_synced,
        )

    def _worksheet_or_create(self, title: str, *, rows: int, cols: int) -> Any:
        try:
            return self.spreadsheet.worksheet(title)
        except self.worksheet_not_found:
            return self.spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)

    def _initialize_control(self) -> None:
        control = self._require_control()
        matrix: list[list[Any]] = [[""] * 6 for _ in range(12)]
        matrix[0][0] = CONTROL_MARKER
        matrix[1][0] = (
            "1. Добавьте ссылки в лист очереди.  2. Укажите цель (0 = все строки).  "
            "3. Поставьте галочку «ЗАПУСТИТЬ В CRM»."
        )
        matrix[3] = ["ЗАПУСТИТЬ В CRM", False, "", "Статус", "ГОТОВ", ""]
        matrix[4] = ["Остановить", False, "", "Команда ID", "", ""]
        matrix[5] = ["Цель по новым лидам (0 = все)", 0, "", "Прогресс", "0 / все", ""]
        matrix[6] = ["Лист очереди", self.settings.google_worksheet, "", "Запущено", "", ""]
        matrix[7] = [
            "Повторить строки после ручной проверки",
            False,
            "",
            "Последняя связь с компьютером",
            utc_now(),
            "",
        ]
        matrix[8] = ["", "", "", "Сообщение", "Пульт ожидает команду.", ""]
        matrix[10] = [
            "Капча не решается автоматически. По уведомлению нужно зайти на удалённый ПК.",
            "",
            "",
            "Компьютер",
            _computer_name(self.settings),
            "",
        ]
        matrix[11][3:] = ["Версия", __version__, ""]
        control.update(matrix, "A1:F12", value_input_option="RAW")
        self._format_control()

    def _upgrade_control_labels(self) -> None:
        control = self._require_control()
        with suppress(Exception):
            control.batch_update(
                [
                    {
                        "range": "A6",
                        "values": [["Цель по новым лидам (0 = все)"]],
                    },
                    {
                        "range": "A8",
                        "values": [["Повторить строки после ручной проверки"]],
                    },
                    {
                        "range": "D8",
                        "values": [["Последняя связь с компьютером"]],
                    },
                ],
                value_input_option="RAW",
            )

    def _backfill_history_dates(self) -> None:
        history = self._require_history()
        with suppress(Exception):
            rows = history.get_all_values()
            updates = []
            date_index = HISTORY_HEADERS.index("Дата завершения")
            for row_number, row in enumerate(rows[1:], start=2):
                finished_at = str(row[5] if len(row) > 5 else "").strip()
                existing_date = str(row[date_index] if len(row) > date_index else "").strip()
                if finished_at and not existing_date:
                    updates.append({"range": f"V{row_number}", "values": [[finished_at[:10]]]})
            if updates:
                history.batch_update(updates, value_input_option="USER_ENTERED")

    def _initialize_analytics(self, *, add_chart: bool = True) -> None:
        analytics = self._require_analytics()
        current_period = self._analytics_period_value(default=30)
        rows: list[list[Any]] = [[""] * 8 for _ in range(107)]
        rows[0][0] = ANALYTICS_MARKER
        rows[1][0] = (
            "Показатели всех запусков. Период сравнивается с предыдущим периодом "
            "такой же длины; данные рассчитывает программа без формул."
        )
        rows[3][:5] = ["Период, дней", current_period, "", "Обновлено", ""]
        rows[5][:4] = ["Показатель", "Текущий период", "Предыдущий период", "Изменение"]
        rows[16][:5] = [
            "Дата",
            "Создано лидов",
            "Открыто номеров",
            "Тех. ошибок",
            "Недозвон",
        ]
        with suppress(Exception):
            analytics.resize(rows=110, cols=8)
        with suppress(Exception):
            analytics.unmerge_cells("A1:H1")
            analytics.unmerge_cells("A2:H2")
        analytics.update(rows, "A1:H107", value_input_option="RAW")
        self._format_analytics(add_chart=add_chart)

    def refresh_analytics(self, *, force: bool = False) -> None:
        analytics = self._require_analytics()
        history = self._require_history()
        period = self._analytics_period_value(default=30)
        if not force and self._analytics_period == period:
            return
        today = datetime.now(UTC).date()
        current_start = today - timedelta(days=period - 1)
        previous_start = today - timedelta(days=2 * period - 1)
        previous_end = current_start - timedelta(days=1)
        rows = history.get_all_values()
        records: list[tuple[date, list[str]]] = []
        for row in rows[1:]:
            finished = _history_date(row)
            if finished is not None:
                records.append((finished, row))

        def selected(start: date, end: date) -> list[list[str]]:
            return [row for finished, row in records if start <= finished <= end]

        current = selected(current_start, today)
        previous = selected(previous_start, previous_end)

        def total(values: list[list[str]], index: int) -> int:
            return sum(_history_int(row, index) for row in values)

        def snapshot(values: list[list[str]]) -> list[float | int]:
            processed = total(values, 12)
            created = total(values, 7)
            return [
                len(values),
                processed,
                total(values, 13),
                total(values, 14),
                created,
                total(values, 15) + total(values, 16),
                total(values, 17),
                total(values, 9),
                total(values, 22),
                created / processed if processed else 0.0,
            ]

        current_values = snapshot(current)
        previous_values = snapshot(previous)
        labels = [
            "Запусков",
            "Обработано ссылок",
            "Всего попыток",
            "Открыто номеров",
            "Создано лидов",
            "Неактивных / без кнопки",
            "Не открыто после попыток",
            "Технических ошибок",
            "Строк со статусом «Недозвон»",
            "Конверсия ссылок в лиды",
        ]
        metrics = []
        for label, current_value, previous_value in zip(
            labels, current_values, previous_values, strict=True
        ):
            change = (
                0.0
                if previous_value == 0 and current_value == 0
                else 1.0
                if previous_value == 0
                else (current_value - previous_value) / previous_value
            )
            metrics.append([label, current_value, previous_value, change])

        daily_count = min(period, 90)
        daily_start = today - timedelta(days=daily_count - 1)
        daily: list[list[Any]] = []
        for offset in range(daily_count):
            day = daily_start + timedelta(days=offset)
            day_rows = [row for finished, row in records if finished == day]
            daily.append(
                [
                    day.strftime("%d.%m.%Y"),
                    total(day_rows, 7),
                    total(day_rows, 14),
                    total(day_rows, 9),
                    total(day_rows, 22),
                ]
            )
        daily.extend([["", "", "", "", ""]] * (90 - len(daily)))
        analytics.update(metrics, "A7:D16", value_input_option="RAW")
        analytics.update(daily, "A18:E107", value_input_option="RAW")
        analytics.batch_update(
            [
                {"range": "B4", "values": [[period]]},
                {
                    "range": "E4",
                    "values": [[datetime.now().astimezone().strftime("%d.%m.%Y %H:%M:%S")]],
                },
            ],
            value_input_option="RAW",
        )
        self._analytics_period = period

    def refresh_analytics_if_needed(self) -> None:
        self.refresh_analytics(force=False)

    def _analytics_period_value(self, *, default: int) -> int:
        analytics = self._require_analytics()
        try:
            raw = self._cell(analytics.get("B4"), 1, 1)
            value = int(float(raw)) if raw else default
        except (TypeError, ValueError):
            value = default
        return value if value in {7, 30, 90, 365} else default

    def _format_analytics(self, *, add_chart: bool = True) -> None:
        analytics = self._require_analytics()
        with suppress(Exception):
            analytics.freeze(rows=6)
            analytics.merge_cells("A1:H1")
            analytics.merge_cells("A2:H2")
            analytics.format(
                "A1:H1",
                {
                    "backgroundColor": {"red": 0.05, "green": 0.09, "blue": 0.16},
                    "textFormat": {
                        "bold": True,
                        "fontSize": 17,
                        "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                    },
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE",
                },
            )
            analytics.format(
                "A2:H2",
                {
                    "backgroundColor": {"red": 0.92, "green": 0.95, "blue": 1},
                    "textFormat": {"foregroundColor": {"red": 0.22, "green": 0.3, "blue": 0.45}},
                    "horizontalAlignment": "CENTER",
                },
            )
            analytics.format(
                "A6:D6",
                {
                    "backgroundColor": {"red": 0.12, "green": 0.24, "blue": 0.42},
                    "textFormat": {
                        "bold": True,
                        "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                    },
                },
            )
            analytics.format(
                "A7:D16",
                {
                    "backgroundColor": {"red": 0.97, "green": 0.98, "blue": 1},
                    "verticalAlignment": "MIDDLE",
                },
            )
            analytics.format("A7:A16", {"textFormat": {"bold": True}})
            analytics.format(
                "D7:D16",
                {
                    "numberFormat": {
                        "type": "PERCENT",
                        "pattern": "+0.0%;-0.0%;0.0%",
                    }
                },
            )
            analytics.format("B16:C16", {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}})
            analytics.format(
                "A17:E17",
                {
                    "backgroundColor": {"red": 0.12, "green": 0.24, "blue": 0.42},
                    "textFormat": {
                        "bold": True,
                        "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                    },
                },
            )
            analytics.format(
                "A18:A107",
                {"numberFormat": {"type": "DATE", "pattern": "dd.mm.yyyy"}},
            )

        sheet_id = getattr(analytics, "id", None)
        if sheet_id is None:
            return
        with suppress(Exception):
            requests: list[dict[str, Any]] = [
                _hide_gridlines_request(sheet_id),
                _row_height_request(sheet_id, 0, 1, 50),
                _row_height_request(sheet_id, 1, 2, 36),
                _column_width_request(sheet_id, 0, 1, 260),
                _column_width_request(sheet_id, 1, 5, 135),
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 3,
                            "endRowIndex": 4,
                            "startColumnIndex": 1,
                            "endColumnIndex": 2,
                        },
                        "rule": {
                            "condition": {
                                "type": "ONE_OF_LIST",
                                "values": [
                                    {"userEnteredValue": value}
                                    for value in ("7", "30", "90", "365")
                                ],
                            },
                            "strict": True,
                            "showCustomUi": True,
                        },
                    }
                },
            ]
            if add_chart:
                requests.append(_analytics_chart_request(sheet_id))
            self.spreadsheet.batch_update({"requests": requests})

    def _format_control(self) -> None:
        control = self._require_control()
        with suppress(Exception):
            control.freeze(rows=2)
            for cell_range in ("A1:F1", "A2:F2"):
                control.merge_cells(cell_range)
            output_ranges = (
                "E4:F4",
                "E5:F5",
                "E6:F6",
                "E7:F7",
                "E8:F8",
                "E9:F10",
                "E11:F11",
                "E12:F12",
            )
            for cell_range in output_ranges:
                control.merge_cells(cell_range)
            control.merge_cells("A11:C12")
            control.format(
                "A1:F1",
                {
                    "backgroundColor": {"red": 0.05, "green": 0.09, "blue": 0.16},
                    "textFormat": {
                        "bold": True,
                        "fontSize": 16,
                        "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                    },
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE",
                },
            )
            control.format(
                "A4:B8",
                {"backgroundColor": {"red": 0.95, "green": 0.97, "blue": 1.0}},
            )
            control.format(
                "D4:F12",
                {"backgroundColor": {"red": 0.97, "green": 0.98, "blue": 0.99}},
            )
            control.format("A4:A8", {"textFormat": {"bold": True}})
            control.format("D4:D12", {"textFormat": {"bold": True}})
            control.format("B4:B5", {"backgroundColor": {"red": 0.86, "green": 0.94, "blue": 1.0}})
            control.format("A1:F12", {"verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"})
        self._apply_sheet_rules(control, include_conditionals=True)

    def _format_history(self) -> None:
        history = self._require_history()
        with suppress(Exception):
            history.freeze(rows=1)
            history.set_basic_filter()
            history.format(
                f"A1:{_column_letter(len(HISTORY_HEADERS))}1",
                {
                    "backgroundColor": {"red": 0.05, "green": 0.09, "blue": 0.16},
                    "textFormat": {
                        "bold": True,
                        "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                    },
                    "verticalAlignment": "MIDDLE",
                },
            )
        sheet_id = getattr(history, "id", None)
        if sheet_id is not None:
            with suppress(Exception):
                requests: list[dict[str, Any]] = [
                    _hide_gridlines_request(sheet_id),
                    _row_height_request(sheet_id, 0, 1, 42),
                ]
                for start, end, size in (
                    (0, 1, 190),
                    (1, 2, 75),
                    (2, 3, 145),
                    (3, 4, 220),
                    (4, 6, 170),
                    (6, 7, 170),
                    (7, 10, 95),
                    (10, 11, 320),
                    (11, 12, 170),
                    (12, len(HISTORY_HEADERS), 130),
                ):
                    requests.append(_column_width_request(sheet_id, start, end, size))
                requests.extend(
                    _status_conditional_requests(
                        sheet_id,
                        start_row=1,
                        end_row=1000,
                        start_column=6,
                        end_column=7,
                    )
                )
                self.spreadsheet.batch_update({"requests": requests})

    def _apply_sheet_rules(self, control: Any, *, include_conditionals: bool = False) -> None:
        sheet_id = getattr(control, "id", None)
        if sheet_id is None:
            return
        requests: list[dict[str, Any]] = []
        requests.append(_hide_gridlines_request(sheet_id))
        for start, end, size in (
            (0, 1, 46),
            (1, 2, 54),
            (2, 3, 18),
            (3, 8, 38),
            (8, 10, 34),
            (10, 12, 38),
        ):
            requests.append(_row_height_request(sheet_id, start, end, size))
        for row_index in (3, 4, 7):
            requests.append(
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": row_index,
                            "endRowIndex": row_index + 1,
                            "startColumnIndex": 1,
                            "endColumnIndex": 2,
                        },
                        "rule": {
                            "condition": {"type": "BOOLEAN"},
                            "strict": True,
                            "showCustomUi": True,
                        },
                    }
                }
            )
        requests.append(
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 5,
                        "endRowIndex": 6,
                        "startColumnIndex": 1,
                        "endColumnIndex": 2,
                    },
                    "rule": {
                        "condition": {
                            "type": "NUMBER_GREATER_THAN_EQ",
                            "values": [{"userEnteredValue": "0"}],
                        },
                        "inputMessage": "Целое число от 0; 0 означает обработать все строки",
                        "strict": True,
                        "showCustomUi": True,
                    },
                }
            }
        )
        widths = ((0, 1, 280), (1, 2, 150), (2, 3, 30), (3, 4, 150), (4, 6, 220))
        for start, end, size in widths:
            requests.append(_column_width_request(sheet_id, start, end, size))
        if include_conditionals:
            requests.extend(
                _status_conditional_requests(
                    sheet_id,
                    start_row=3,
                    end_row=4,
                    start_column=4,
                    end_column=6,
                )
            )
        with suppress(Exception):
            self.spreadsheet.batch_update({"requests": requests})

    def _batch_control(self, values: dict[str, Any]) -> None:
        control = self._require_control()
        data = [{"range": address, "values": [[value]]} for address, value in values.items()]
        try:
            control.batch_update(data, value_input_option="RAW")
        except Exception as exc:
            raise SourceError(f"Не удалось обновить пульт: {exc}") from exc

    def _require_control(self) -> Any:
        if self.control is None:
            raise SourceError("Лист пульта ещё не подготовлен")
        return self.control

    def _require_history(self) -> Any:
        if self.history is None:
            raise SourceError("Лист истории ещё не подготовлен")
        return self.history

    def _require_analytics(self) -> Any:
        if self.analytics is None:
            raise SourceError("Лист аналитики ещё не подготовлен")
        return self.analytics

    @staticmethod
    def _cell(values: list[list[Any]], row: int, column: int) -> str:
        row_index = row - 1
        column_index = column - 1
        if row_index >= len(values) or column_index >= len(values[row_index]):
            return ""
        return str(values[row_index][column_index] or "").strip()


class RemoteController:
    def __init__(self, settings: Settings, panel: GoogleControlPanel):
        self.settings = settings
        self.panel = panel
        self.state_path = settings.data_dir / "remote-control.json"
        self._state: CommandState | None = None
        self._worker: threading.Thread | None = None
        self._worker_result: WorkerResult | None = None
        self._worker_guard = threading.Lock()
        self._progress = ProgressSnapshot()
        self._stop_event = threading.Event()

    def setup(self) -> None:
        self.panel.ensure_layout()
        self.panel.heartbeat()

    def run_forever(self) -> None:
        self.setup()
        self._state = self._load_state()
        LOGGER.info(
            "Удалённый пульт запущен; опрос Google каждые %.0f сек.",
            self.settings.remote_control_poll_seconds,
        )
        delay = self.settings.remote_control_poll_seconds
        while not self._stop_event.is_set():
            try:
                self.tick()
                delay = self.settings.remote_control_poll_seconds
            except _ControllerReloadRequired:
                LOGGER.warning(
                    "На диске установлена новая версия Avito CRM; "
                    "завершаем контроллер для автоматического перезапуска."
                )
                raise
            except Exception as exc:
                LOGGER.error("Ошибка цикла удалённого пульта: %s", exc)
                delay = min(max(delay * 2, 10), 120)
            self._stop_event.wait(delay)

    def tick(self) -> None:
        if self._state is None and self._source_version_changed():
            raise _ControllerReloadRequired

        if self._state and self._state.phase == "finalizing":
            self._finalize_remote()
            return

        command = self.panel.read_command()
        try:
            self.panel.refresh_analytics_if_needed()
        except Exception as exc:
            LOGGER.warning("Не удалось обновить период аналитики: %s", exc)
        if command.stop:
            self._handle_stop(clear_start=command.start)
            if command.start:
                return

        self._collect_worker_result()
        if self._state and self._state.phase == "finalizing":
            self._finalize_remote()
            return

        if self._state:
            self._continue_existing_state()

        if command.start:
            if self._state:
                self.panel.reject_start(
                    f"Уже выполняется команда {self._state.command_id}; новый запуск отклонён."
                )
            else:
                self._accept(command)

        if self._state:
            if self._worker and self._worker.is_alive():
                status = "ОСТАНАВЛИВАЕТСЯ" if self._state.stop_requested else "РАБОТАЕТ"
                self.panel.update_active(self._state, self._progress, status=status)
            elif self._state.phase == "waiting_busy":
                self.panel.update_active(
                    self._state,
                    self._progress,
                    status="ОЖИДАЕТ",
                    message="На компьютере ещё работает другой запуск; пульт подождёт.",
                )
            else:
                self.panel.heartbeat()
        else:
            self.panel.heartbeat()

    def stop(self) -> None:
        self._stop_event.set()

    def _accept(self, command: PanelCommand) -> None:
        self._validate_command(command)
        state = CommandState(
            command_id=_new_command_id(),
            target=command.limit,
            worksheet=command.worksheet,
            retry_manual=command.retry_manual,
            phase="claiming",
            started_at=utc_now(),
        )
        self._state = state
        self._save_state(state)
        self._continue_existing_state()

    def _validate_command(self, command: PanelCommand) -> None:
        if command.limit < 0:
            self.panel.reject_start("Цель должна быть целым числом от 0; 0 означает «все».")
            raise ConfigurationError("Некорректный лимит удалённой команды")
        reserved = {
            self.settings.google_control_worksheet.casefold(),
            self.settings.google_history_worksheet.casefold(),
            ANALYTICS_WORKSHEET.casefold(),
        }
        if not command.worksheet.strip() or command.worksheet.casefold() in reserved:
            self.panel.reject_start("Укажите отдельный лист очереди, например «Лист1».")
            raise ConfigurationError("Некорректный лист очереди")

    def _continue_existing_state(self) -> None:
        state = self._state
        if state is None or self._worker and self._worker.is_alive():
            return
        if state.stop_requested:
            self._prepare_stopped_result(state)
            return
        if state.phase == "claiming":
            self.panel.claim(state)
            state.phase = "claimed"
            self._save_state(state)
        if state.history_row < 2:
            state.history_row = self.panel.append_history(state)
            self._save_state(state)
        recovered = self.panel.count_command(state.worksheet, state.command_id)
        state.base_created = recovered.created
        state.base_captured = recovered.captured
        state.base_duplicates = recovered.duplicates
        state.base_errors = recovered.errors
        state.base_invalid = recovered.invalid
        state.base_inactive = recovered.inactive
        state.base_unavailable = recovered.unavailable
        state.base_phone_failed = recovered.phone_failed
        state.base_retries = recovered.retries
        state.base_manual_required = recovered.manual_required
        state.base_processed = recovered.processed
        state.base_inspected = recovered.inspected
        state.base_no_answer_synced = recovered.no_answer_synced
        self._progress = recovered
        remaining = max(0, state.target - recovered.created)
        if state.target > 0 and remaining == 0:
            state.result = self._result_dict(
                state,
                status="ЗАВЕРШЕНО",
                message="Лимит уже достигнут; повторный запуск не потребовался.",
                progress=recovered,
            )
            state.phase = "finalizing"
            self._save_state(state)
            return
        state.phase = "running"
        self._save_state(state)
        self._worker_result = None
        self._worker = threading.Thread(
            target=self._run_worker,
            args=(replace(state), remaining),
            name=f"avito-crm-{state.command_id}",
            daemon=True,
        )
        self._worker.start()

    def _run_worker(self, state: CommandState, remaining: int) -> None:
        try:
            # The controller is a long-running scheduled task.  Reload the
            # local .env for every accepted command so notification recipients,
            # tokens and other GUI settings take effect without a Windows logoff
            # or manual task restart.
            worker_settings = self._load_worker_settings()
            source = build_queue_source(worker_settings, "google", None, state.worksheet)
            with (
                SingleInstanceLock(worker_settings.data_dir / "worker.lock"),
                StateStore(worker_settings.state_db) as store,
            ):
                summary = Pipeline(
                    worker_settings,
                    source,
                    store,
                    source_name=(
                        f"google:{worker_settings.google_spreadsheet_id}:{state.worksheet}"
                    ),
                    mode="full",
                    live=True,
                    include_manual=state.retry_manual,
                ).run(
                    remaining,
                    run_id=state.command_id,
                    progress=lambda current, row_id: self._progress_callback(
                        state, current, row_id
                    ),
                )
            result = WorkerResult(kind="finished", summary=summary)
        except InstanceAlreadyRunning as exc:
            result = WorkerResult(kind="busy", message=str(exc))
        except Exception as exc:
            LOGGER.exception("Рабочий процесс пульта завершился с ошибкой")
            result = WorkerResult(
                kind="error", message=f"{exc.__class__.__name__}: {str(exc).strip()}"[:500]
            )
        with self._worker_guard:
            self._worker_result = result

    def _load_worker_settings(self) -> Settings:
        return Settings.load(self.settings.root_dir, refresh_env=True)

    def _source_version_changed(self) -> bool:
        """Let Windows reload Python modules once an update is safely idle."""
        project_file = self.settings.root_dir / "pyproject.toml"
        try:
            with project_file.open("rb") as handle:
                disk_version = str(tomllib.load(handle)["project"]["version"]).strip()
        except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
            return False
        return bool(disk_version and disk_version != __version__)

    def _progress_callback(self, state: CommandState, summary: RunSummary, row_id: str) -> None:
        with self._worker_guard:
            self._progress = ProgressSnapshot(
                created=state.base_created + summary.created,
                captured=state.base_captured + summary.captured,
                duplicates=state.base_duplicates + summary.duplicates,
                errors=state.base_errors + summary.errors,
                invalid=state.base_invalid + summary.invalid,
                inactive=state.base_inactive + summary.inactive,
                unavailable=state.base_unavailable + summary.unavailable,
                phone_failed=state.base_phone_failed + summary.phone_failed,
                retries=state.base_retries + summary.retries,
                manual_required=state.base_manual_required + summary.manual_required,
                processed=state.base_processed + summary.processed,
                inspected=state.base_inspected + summary.inspected,
                no_answer_synced=(
                    state.base_no_answer_synced + summary.no_answer_synced
                ),
                crm_sync_errors=summary.crm_sync_errors,
                row_id=row_id,
            )

    def _collect_worker_result(self) -> None:
        if self._worker is None or self._worker.is_alive():
            return
        with self._worker_guard:
            result = self._worker_result
        self._worker = None
        self._worker_result = None
        if self._state is None or result is None:
            return
        if result.kind == "busy":
            self._state.phase = "waiting_busy"
            self._save_state(self._state)
            return
        if result.kind == "error":
            self._state.result = self._result_dict(
                self._state,
                status="ОШИБКА",
                message=result.message,
                progress=self._progress,
            )
        else:
            self._state.result = self._classify_summary(
                self._state, result.summary or RunSummary(self._state.command_id, 0)
            )
        self._state.phase = "finalizing"
        self._save_state(self._state)

    def _handle_stop(self, *, clear_start: bool) -> None:
        self.panel.acknowledge_stop(clear_start=clear_start)
        request_stop(self.settings.data_dir)
        if self._state:
            self._state.stop_requested = True
            self._state.phase = "stopping"
            self._save_state(self._state)
            self.panel.update_active(
                self._state,
                self._progress,
                status="ОСТАНАВЛИВАЕТСЯ",
                message="Остановка принята; текущий безопасный шаг завершается.",
            )
        else:
            self.panel.reject_start("Команда STOP передана; активного запуска пульта нет.")

    def _prepare_stopped_result(self, state: CommandState) -> None:
        progress = self.panel.count_command(state.worksheet, state.command_id)
        self._progress = progress
        state.result = self._result_dict(
            state,
            status="ОСТАНОВЛЕНО",
            message="Запуск остановлен по команде оператора.",
            progress=progress,
        )
        state.phase = "finalizing"
        self._save_state(state)

    def _classify_summary(self, state: CommandState, summary: RunSummary) -> dict[str, Any]:
        progress = replace(self._progress, crm_sync_errors=summary.crm_sync_errors)
        self._progress = progress
        if state.stop_requested or "останов" in summary.stopped_reason.casefold():
            status = "ОСТАНОВЛЕНО"
        elif progress.manual_required:
            status = "ТРЕБУЕТ ВНИМАНИЯ"
        elif progress.errors:
            status = "ЗАВЕРШЕНО С ТЕХНИЧЕСКИМИ ОШИБКАМИ"
        elif progress.crm_sync_errors:
            status = "ЗАВЕРШЕНО С ПРЕДУПРЕЖДЕНИЯМИ"
        else:
            status = "ЗАВЕРШЕНО"
        goal_message = (
            f"Создано {progress.created}; обработаны все доступные строки; "
            if state.target == 0
            else f"Создано {progress.created} из цели {state.target}; "
        )
        message = (
            goal_message
            +
            f"открыто номеров {progress.captured}; "
            f"неактивных {progress.inactive}; без кнопки {progress.unavailable}; "
            f"статус «Недозвон» {progress.no_answer_synced}; "
            f"не открыто после попыток {progress.phone_failed}; "
            f"технических ошибок {progress.errors}; "
            f"предупреждений синхронизации CRM {progress.crm_sync_errors}. "
            f"Остановка: {summary.stopped_reason or 'не указана'}."
        )
        return self._result_dict(state, status=status, message=message, progress=progress)

    @staticmethod
    def _result_dict(
        state: CommandState,
        *,
        status: str,
        message: str,
        progress: ProgressSnapshot,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "message": message,
            "finished_at": utc_now(),
            "created": progress.created,
            "captured": progress.captured,
            "duplicates": progress.duplicates,
            "errors": progress.errors,
            "invalid": progress.invalid,
            "inactive": progress.inactive,
            "unavailable": progress.unavailable,
            "phone_failed": progress.phone_failed,
            "retries": progress.retries,
            "manual_required": progress.manual_required,
            "processed": progress.processed,
            "inspected": progress.inspected,
            "no_answer_synced": progress.no_answer_synced,
            "crm_sync_errors": progress.crm_sync_errors,
            "command_id": state.command_id,
        }

    def _finalize_remote(self) -> None:
        if self._state is None:
            return
        self.panel.finish(self._state)
        LOGGER.info(
            "Команда %s завершена: %s",
            self._state.command_id,
            self._state.result.get("status", ""),
        )
        self.state_path.unlink(missing_ok=True)
        self._state = None
        self._progress = ProgressSnapshot()

    def _load_state(self) -> CommandState | None:
        if not self.state_path.is_file():
            return None
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return CommandState.from_dict(value)
        except Exception as exc:
            damaged = self.state_path.with_name(f"remote-control.invalid-{int(time.time())}.json")
            with suppress(OSError):
                os.replace(self.state_path, damaged)
            raise SourceError(
                "Локальное состояние пульта повреждено; файл сохранён для диагностики"
            ) from exc

    def _save_state(self, state: CommandState) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, self.state_path)


def run_remote_control(settings: Settings, *, setup_only: bool, allow_live: bool) -> None:
    panel = GoogleControlPanel.connect(settings)
    controller = RemoteController(settings, panel)
    if setup_only:
        controller.setup()
        print(
            f"Готово: листы {settings.google_control_worksheet!r} и "
            f"{settings.google_history_worksheet!r}, а также {ANALYTICS_WORKSHEET!r} "
            "подготовлены."
        )
        return
    if not allow_live:
        raise ConfigurationError("Удалённый пульт создаёт лиды: нужен явный флаг --allow-live-crm")
    with SingleInstanceLock(settings.data_dir / "remote-control.lock"):
        try:
            controller.run_forever()
        except KeyboardInterrupt:
            controller.stop()
            LOGGER.info("Удалённый пульт остановлен")


def _analytics_chart_request(sheet_id: int) -> dict[str, Any]:
    def source_range(column: int) -> dict[str, Any]:
        return {
            "sourceRange": {
                "sources": [
                    {
                        "sheetId": sheet_id,
                        "startRowIndex": 16,
                        "endRowIndex": 107,
                        "startColumnIndex": column,
                        "endColumnIndex": column + 1,
                    }
                ]
            }
        }

    return {
        "addChart": {
            "chart": {
                "spec": {
                    "title": "Динамика по дням",
                    "basicChart": {
                        "chartType": "LINE",
                        "legendPosition": "BOTTOM_LEGEND",
                        "headerCount": 1,
                        "domains": [{"domain": source_range(0)}],
                        "series": [
                            {"series": source_range(column), "targetAxis": "LEFT_AXIS"}
                            for column in (1, 2, 3, 4)
                        ],
                        "axis": [
                            {"position": "BOTTOM_AXIS", "title": "Дата"},
                            {"position": "LEFT_AXIS", "title": "Количество"},
                        ],
                    },
                },
                "position": {
                    "overlayPosition": {
                        "anchorCell": {
                            "sheetId": sheet_id,
                            "rowIndex": 16,
                            "columnIndex": 5,
                        },
                        "widthPixels": 720,
                        "heightPixels": 380,
                    }
                },
            }
        }
    }


def _column_letter(number: int) -> str:
    if number < 1:
        raise ValueError("Номер колонки должен быть положительным")
    result = ""
    value = number
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _checked(value: str) -> bool:
    return str(value or "").strip().casefold() in {"true", "1", "yes", "да", "запуск"}


def _parse_panel_limit(value: object) -> int:
    """Parse a non-negative integer without silently truncating decimal input."""
    raw = str(value or "").strip().replace("\u00a0", "")
    if not raw:
        return 0
    normalized = raw.replace(",", ".")
    try:
        numeric = float(normalized)
    except ValueError:
        return -1
    if not numeric.is_integer() or numeric < 0:
        return -1
    return int(numeric)


def _target_label(target: int) -> str:
    return "все" if target == 0 else str(target)


def _new_command_id() -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def _history_int(row: list[str], index: int) -> int:
    if index >= len(row):
        return 0
    raw = str(row[index] or "").strip().replace("\u00a0", "").replace(",", ".")
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        return 0


def _history_date(row: list[str]) -> date | None:
    # V is the stable date-only column. Older rows may only have F/E timestamps.
    for index in (21, 5, 4):
        if index >= len(row):
            continue
        raw = str(row[index] or "").strip()
        if not raw:
            continue
        iso_candidate = raw[:10]
        with suppress(ValueError):
            return date.fromisoformat(iso_candidate)
        for pattern in ("%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y"):
            with suppress(ValueError):
                return datetime.strptime(raw[:10], pattern).date()
    return None


def _computer_name(settings: Settings) -> str:
    return settings.notification_computer_name or socket.gethostname()


def _hide_gridlines_request(sheet_id: int) -> dict[str, Any]:
    return {
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "gridProperties": {"hideGridlines": True},
            },
            "fields": "gridProperties.hideGridlines",
        }
    }


def _row_height_request(sheet_id: int, start: int, end: int, size: int) -> dict[str, Any]:
    return {
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "ROWS",
                "startIndex": start,
                "endIndex": end,
            },
            "properties": {"pixelSize": size},
            "fields": "pixelSize",
        }
    }


def _column_width_request(sheet_id: int, start: int, end: int, size: int) -> dict[str, Any]:
    return {
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": start,
                "endIndex": end,
            },
            "properties": {"pixelSize": size},
            "fields": "pixelSize",
        }
    }


def _status_conditional_requests(
    sheet_id: int,
    *,
    start_row: int,
    end_row: int,
    start_column: int,
    end_column: int,
) -> list[dict[str, Any]]:
    cell_range = {
        "sheetId": sheet_id,
        "startRowIndex": start_row,
        "endRowIndex": end_row,
        "startColumnIndex": start_column,
        "endColumnIndex": end_column,
    }
    styles = (
        ("TEXT_EQ", "ЗАВЕРШЕНО", (0.86, 0.96, 0.9), (0.08, 0.42, 0.2)),
        ("TEXT_EQ", "РАБОТАЕТ", (0.86, 0.92, 1.0), (0.08, 0.31, 0.72)),
        ("TEXT_EQ", "ПРИНЯТО", (0.86, 0.92, 1.0), (0.08, 0.31, 0.72)),
        ("TEXT_CONTAINS", "ОШИБ", (1.0, 0.9, 0.9), (0.7, 0.1, 0.1)),
        ("TEXT_CONTAINS", "ВНИМАНИЯ", (1.0, 0.95, 0.8), (0.55, 0.32, 0.03)),
        ("TEXT_CONTAINS", "НЕДОСТАТОЧНО", (1.0, 0.95, 0.8), (0.55, 0.32, 0.03)),
        ("TEXT_CONTAINS", "ОСТАНОВ", (0.92, 0.93, 0.95), (0.28, 0.32, 0.4)),
    )
    requests = []
    for condition_type, value, background, foreground in styles:
        requests.append(
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [cell_range],
                        "booleanRule": {
                            "condition": {
                                "type": condition_type,
                                "values": [{"userEnteredValue": value}],
                            },
                            "format": {
                                "backgroundColor": {
                                    "red": background[0],
                                    "green": background[1],
                                    "blue": background[2],
                                },
                                "textFormat": {
                                    "bold": True,
                                    "foregroundColor": {
                                        "red": foreground[0],
                                        "green": foreground[1],
                                        "blue": foreground[2],
                                    },
                                },
                            },
                        },
                    },
                    "index": 0,
                }
            }
        )
    return requests
