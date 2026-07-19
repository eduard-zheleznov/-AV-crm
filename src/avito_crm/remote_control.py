from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
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
HISTORY_HEADERS = (
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
    base_manual_required: int = 0
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
    manual_required: int = 0
    inspected: int = 0
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
                self._apply_sheet_rules(self.control)
            history_values = self.history.get("A1:L1")
            history_headers = tuple(history_values[0]) if history_values else ()
            if history_headers and history_headers != HISTORY_HEADERS:
                raise SourceError(
                    f"Лист {self.settings.google_history_worksheet!r} уже занят "
                    "другими данными. Переименуйте его или задайте "
                    "другой GOOGLE_HISTORY_WORKSHEET."
                )
            if not history_headers:
                self.history.update([list(HISTORY_HEADERS)], "A1:L1", value_input_option="RAW")
                self._format_history()
        except Exception as exc:
            raise SourceError(f"Не удалось подготовить листы пульта: {exc}") from exc

    def read_command(self) -> PanelCommand:
        control = self._require_control()
        try:
            values = control.get("A1:F12")
            raw_limit = self._cell(values, 6, 2) or "10"
            try:
                limit = int(float(str(raw_limit).replace(",", ".")))
            except ValueError:
                limit = 0
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
                "E6": f"0 / {state.target}",
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
                "E6": f"{progress.created} / {state.target}",
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
                "E6": f"{int(result.get('created', 0))} / {state.target}",
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
                    ]
                ],
                f"A{row_number}:L{row_number}",
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
                    ]
                ],
                f"F{state.history_row}:L{state.history_row}",
                value_input_option="RAW",
            )
        except Exception as exc:
            raise SourceError(f"Не удалось завершить запись истории: {exc}") from exc

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
        counts: dict[str, int] = {}
        for row in values[1:]:
            run_value = row[run_index].strip() if run_index < len(row) else ""
            if run_value != command_id:
                continue
            status = row[status_index].strip().lower() if status_index < len(row) else ""
            counts[status] = counts.get(status, 0) + 1
        return ProgressSnapshot(
            created=counts.get(ItemStatus.DONE.value, 0),
            captured=counts.get(ItemStatus.CAPTURED.value, 0),
            duplicates=counts.get(ItemStatus.DUPLICATE.value, 0),
            errors=counts.get(ItemStatus.ERROR.value, 0),
            invalid=counts.get(ItemStatus.INVALID.value, 0),
            manual_required=counts.get(ItemStatus.MANUAL_REQUIRED.value, 0),
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
            "1. Добавьте ссылки в лист очереди.  2. Укажите лимит.  "
            "3. Поставьте галочку «ЗАПУСТИТЬ В CRM»."
        )
        matrix[3] = ["ЗАПУСТИТЬ В CRM", False, "", "Статус", "ГОТОВ", ""]
        matrix[4] = ["Остановить", False, "", "Команда ID", "", ""]
        matrix[5] = ["Лимит новых лидов", 10, "", "Прогресс", "0 / 0", ""]
        matrix[6] = ["Лист очереди", self.settings.google_worksheet, "", "Запущено", "", ""]
        matrix[7] = ["Повторить manual_required", False, "", "Heartbeat", utc_now(), ""]
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
                "A1:L1",
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
                    (3, 4, 110),
                    (4, 6, 170),
                    (6, 7, 170),
                    (7, 10, 95),
                    (10, 11, 320),
                    (11, 12, 170),
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
                            "type": "NUMBER_BETWEEN",
                            "values": [
                                {"userEnteredValue": "1"},
                                {"userEnteredValue": str(self.settings.remote_control_max_limit)},
                            ],
                        },
                        "inputMessage": (
                            f"Целое число от 1 до {self.settings.remote_control_max_limit}"
                        ),
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
            except Exception as exc:
                LOGGER.error("Ошибка цикла удалённого пульта: %s", exc)
                delay = min(max(delay * 2, 10), 120)
            self._stop_event.wait(delay)

    def tick(self) -> None:
        if self._state and self._state.phase == "finalizing":
            self._finalize_remote()
            return

        command = self.panel.read_command()
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
        if not 1 <= command.limit <= self.settings.remote_control_max_limit:
            self.panel.reject_start(
                f"Лимит должен быть от 1 до {self.settings.remote_control_max_limit}."
            )
            raise ConfigurationError("Некорректный лимит удалённой команды")
        reserved = {
            self.settings.google_control_worksheet.casefold(),
            self.settings.google_history_worksheet.casefold(),
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
        state.base_manual_required = recovered.manual_required
        self._progress = recovered
        remaining = state.target - recovered.created
        if remaining <= 0:
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
            source = build_queue_source(self.settings, "google", None, state.worksheet)
            with (
                SingleInstanceLock(self.settings.data_dir / "worker.lock"),
                StateStore(self.settings.state_db) as store,
            ):
                summary = Pipeline(
                    self.settings,
                    source,
                    store,
                    source_name=(f"google:{self.settings.google_spreadsheet_id}:{state.worksheet}"),
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

    def _progress_callback(self, state: CommandState, summary: RunSummary, row_id: str) -> None:
        with self._worker_guard:
            self._progress = ProgressSnapshot(
                created=state.base_created + summary.created,
                captured=state.base_captured + summary.captured,
                duplicates=state.base_duplicates + summary.duplicates,
                errors=state.base_errors + summary.errors,
                invalid=state.base_invalid + summary.invalid,
                manual_required=state.base_manual_required + summary.manual_required,
                inspected=summary.inspected,
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
        progress = self._progress
        if state.stop_requested or "останов" in summary.stopped_reason.casefold():
            status = "ОСТАНОВЛЕНО"
        elif progress.created >= state.target:
            status = "ЗАВЕРШЕНО" if progress.errors == 0 else "ЗАВЕРШЕНО С ОШИБКАМИ"
        elif progress.manual_required:
            status = "ТРЕБУЕТ ВНИМАНИЯ"
        elif progress.errors:
            status = "ЗАВЕРШЕНО С ОШИБКАМИ"
        else:
            status = "НЕДОСТАТОЧНО ССЫЛОК"
        message = (
            f"Создано {progress.created} из {state.target}; "
            f"дубликатов {progress.duplicates}; ошибок {progress.errors}. "
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
            "manual_required": progress.manual_required,
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
            f"{settings.google_history_worksheet!r} подготовлены."
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


def _checked(value: str) -> bool:
    return str(value or "").strip().casefold() in {"true", "1", "yes", "да", "запуск"}


def _new_command_id() -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


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
