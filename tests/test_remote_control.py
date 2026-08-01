from __future__ import annotations

import json
import re

import pytest

from avito_crm.models import RunSummary
from avito_crm.remote_control import (
    ANALYTICS_MARKER,
    ANALYTICS_WORKSHEET,
    HISTORY_HEADERS,
    LEGACY_HISTORY_HEADERS,
    PREVIOUS_HISTORY_HEADERS,
    CommandState,
    GoogleControlPanel,
    PanelCommand,
    ProgressSnapshot,
    RemoteController,
    WorkerResult,
)


class MatrixSheet:
    def __init__(self, values=None):
        self.values = values or []
        self.batch = []

    def get(self, range_name):
        start_row, start_col, end_row, end_col = _range_bounds(range_name)
        end_row = end_row or len(self.values)
        end_col = end_col or max((len(row) for row in self.values), default=0)
        result = [
            [
                self.values[row][column]
                if row < len(self.values) and column < len(self.values[row])
                else ""
                for column in range(start_col, end_col)
            ]
            for row in range(start_row, end_row)
        ]
        for row in result:
            while row and row[-1] == "":
                row.pop()
        while result and not result[-1]:
            result.pop()
        return result

    def batch_update(self, data, **_kwargs):
        self.batch.extend(data)
        for item in data:
            if isinstance(item, dict) and "range" in item and "values" in item:
                self.update(item["values"], item["range"])

    def update(self, values, range_name, **_kwargs):
        start_row, start_col, _end_row, _end_col = _range_bounds(range_name)
        required_rows = start_row + len(values)
        while len(self.values) < required_rows:
            self.values.append([])
        for row_offset, row_values in enumerate(values):
            target = self.values[start_row + row_offset]
            required_cols = start_col + len(row_values)
            if len(target) < required_cols:
                target.extend([""] * (required_cols - len(target)))
            target[start_col:required_cols] = list(row_values)

    def get_all_values(self):
        return [row[:] for row in self.values]


def _range_bounds(value):
    matches = re.findall(r"([A-Z]+)(\d+)", value.upper())
    if not matches:
        return 0, 0, None, None

    def column_number(letters):
        result = 0
        for letter in letters:
            result = result * 26 + ord(letter) - 64
        return result - 1

    start_col, start_row = column_number(matches[0][0]), int(matches[0][1]) - 1
    if len(matches) == 1:
        return start_row, start_col, start_row + 1, start_col + 1
    end_col, end_row = column_number(matches[1][0]) + 1, int(matches[1][1])
    return start_row, start_col, end_row, end_col


class MissingWorksheet(Exception):
    pass


class FakeSpreadsheet:
    def __init__(self, worksheets=None):
        self.worksheets = worksheets or {}

    def worksheet(self, title):
        if title not in self.worksheets:
            raise MissingWorksheet(title)
        return self.worksheets[title]

    def add_worksheet(self, *, title, rows, cols):
        del rows, cols
        sheet = MatrixSheet()
        self.worksheets[title] = sheet
        return sheet


class FakePanel:
    def __init__(self, command: PanelCommand, recovered: ProgressSnapshot | None = None):
        self.command = command
        self.recovered = recovered or ProgressSnapshot()
        self.claims = []
        self.history_appends = 0
        self.finishes = []
        self.stop_acks = 0
        self.rejections = []
        self.active_updates = []
        self.heartbeats = 0

    def ensure_layout(self):
        return None

    def heartbeat(self):
        self.heartbeats += 1

    def read_command(self):
        return self.command

    def claim(self, state):
        self.claims.append(state.command_id)
        self.command = PanelCommand(
            False,
            self.command.stop,
            self.command.limit,
            self.command.worksheet,
            False,
            self.command.max_inspected,
        )

    def append_history(self, _state):
        self.history_appends += 1
        return 2

    def count_command(self, _worksheet, _command_id):
        return self.recovered

    def update_active(self, state, progress, **kwargs):
        self.active_updates.append((state.command_id, progress, kwargs))

    def finish(self, state):
        self.finishes.append(state.result.copy())

    def acknowledge_stop(self, **_kwargs):
        self.stop_acks += 1
        self.command = PanelCommand(False, False, 1, "Лист1", False)

    def reject_start(self, message):
        self.rejections.append(message)
        self.command = PanelCommand(False, False, 1, "Лист1", False)


class InstantController(RemoteController):
    def __init__(self, settings, panel):
        super().__init__(settings, panel)
        self.remaining_values = []
        self.remaining_inspected_values = []

    def _run_worker(self, state, remaining, remaining_inspected):
        self.remaining_values.append(remaining)
        self.remaining_inspected_values.append(remaining_inspected)
        summary = RunSummary(
            run_id=state.command_id,
            requested=remaining,
            captured=remaining,
            created=remaining,
            stopped_reason="Достигнут заданный лимит",
        )
        self._progress_callback(state, summary, "")
        with self._worker_guard:
            self._worker_result = WorkerResult(kind="finished", summary=summary)


def test_panel_reads_remote_command_from_fixed_cells(settings):
    values = [[""] * 6 for _ in range(12)]
    values[3][1] = "TRUE"
    values[4][1] = "FALSE"
    values[5][1] = "3"
    values[6][1] = "Новые"
    values[7][1] = "TRUE"
    values[8][1] = "8"
    panel = GoogleControlPanel(settings, spreadsheet=object(), worksheet_not_found=KeyError)
    panel.control = MatrixSheet(values)

    command = panel.read_command()

    assert command == PanelCommand(True, False, 3, "Новые", True, 8)


def test_claim_consumes_start_and_retry_captcha_checkboxes(settings):
    values = [[""] * 6 for _ in range(12)]
    values[3][1] = True
    values[7][1] = True
    panel = GoogleControlPanel(settings, spreadsheet=object(), worksheet_not_found=KeyError)
    panel.control = MatrixSheet(values)
    state = CommandState(
        command_id="cmd-once",
        target=1,
        worksheet="Лист1",
        retry_manual=True,
        phase="claiming",
        started_at="2026-08-01T10:00:00+00:00",
        max_inspected=2,
    )

    panel.claim(state)

    assert panel.control.values[3][1] is False
    assert panel.control.values[7][1] is False


def test_existing_user_control_sheet_is_never_overwritten(settings):
    existing = MatrixSheet([["Мои важные данные"]])
    spreadsheet = FakeSpreadsheet({settings.google_control_worksheet: existing})
    panel = GoogleControlPanel(settings, spreadsheet, MissingWorksheet)

    with pytest.raises(Exception, match="уже занят"):
        panel.ensure_layout()

    assert existing.values == [["Мои важные данные"]]


def test_setup_creates_migrated_history_and_period_analytics(settings):
    control = MatrixSheet([["AVITO CRM — УДАЛЁННЫЙ ПУЛЬТ"]])
    history = MatrixSheet([list(LEGACY_HISTORY_HEADERS)])
    spreadsheet = FakeSpreadsheet(
        {
            settings.google_control_worksheet: control,
            settings.google_history_worksheet: history,
        }
    )
    panel = GoogleControlPanel(settings, spreadsheet, MissingWorksheet)

    panel.ensure_layout()

    assert control.values[7][0] == "Вернуть в очередь после решённой капчи"
    assert "B8 нужна только" in control.values[10][0]
    assert tuple(history.values[0]) == HISTORY_HEADERS
    analytics = spreadsheet.worksheets[ANALYTICS_WORKSHEET]
    assert analytics.values[0][0] == ANALYTICS_MARKER
    assert analytics.values[3][1] == 30
    assert analytics.values[6][0] == "Запусков"
    assert analytics.values[6][1] == 0
    assert re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", analytics.values[17][0])
    assert HISTORY_HEADERS[-2] == "Предел просмотра"
    assert HISTORY_HEADERS[-1] == "Решено капч"


def test_setup_migrates_exact_history_header_from_previous_release(settings):
    control = MatrixSheet([["AVITO CRM — УДАЛЁННЫЙ ПУЛЬТ"]])
    history = MatrixSheet([list(PREVIOUS_HISTORY_HEADERS), ["old-run"]])
    spreadsheet = FakeSpreadsheet(
        {
            settings.google_control_worksheet: control,
            settings.google_history_worksheet: history,
        }
    )
    panel = GoogleControlPanel(settings, spreadsheet, MissingWorksheet)

    panel.ensure_layout()

    assert tuple(history.values[0]) == HISTORY_HEADERS
    assert history.values[1][0] == "old-run"


def test_history_append_is_idempotent_after_a_crash(settings):
    command_id = "cmd-existing"
    history = MatrixSheet([list(range(12)), [command_id]])
    panel = GoogleControlPanel(settings, spreadsheet=object(), worksheet_not_found=KeyError)
    panel.history = history
    state = CommandState(
        command_id=command_id,
        target=2,
        worksheet="Лист1",
        retry_manual=False,
        phase="claimed",
        started_at="2026-07-19T10:00:00+00:00",
    )

    row = panel.append_history(state)

    assert row == 2
    assert len(history.values) == 2


def test_new_history_rows_include_separate_crm_sync_warning_column(settings):
    history = MatrixSheet([list(HISTORY_HEADERS)])
    panel = GoogleControlPanel(settings, spreadsheet=object(), worksheet_not_found=KeyError)
    panel.history = history
    state = CommandState(
        command_id="cmd-warning-column",
        target=0,
        worksheet="Лист1",
        retry_manual=False,
        phase="claimed",
        started_at="2026-07-19T10:00:00+00:00",
    )

    row = panel.append_history(state)

    assert row == 2
    assert len(history.values[1]) == len(HISTORY_HEADERS)
    assert history.values[1][-1] == 0


def test_remote_command_is_claimed_once_and_finished(settings):
    panel = FakePanel(PanelCommand(True, False, 2, "Лист1", False))
    controller = InstantController(settings, panel)

    controller.tick()
    assert controller._worker is not None
    controller._worker.join(timeout=2)
    controller.tick()

    assert len(panel.claims) == 1
    assert panel.history_appends == 1
    assert controller.remaining_values == [2]
    assert controller.remaining_inspected_values == [4]
    assert panel.finishes[0]["status"] == "ЗАВЕРШЕНО"
    assert panel.finishes[0]["created"] == 2
    assert not controller.state_path.exists()

    controller.tick()
    assert len(panel.claims) == 1


def test_remote_worker_reloads_env_before_each_command(settings, monkeypatch):
    panel = FakePanel(PanelCommand(False, False, 1, "Лист1", False))
    controller = RemoteController(settings, panel)
    calls = []

    def load(root_dir, *, refresh_env=False):
        calls.append((root_dir, refresh_env))
        return settings

    monkeypatch.setattr("avito_crm.remote_control.Settings.load", load)

    assert controller._load_worker_settings() is settings
    assert calls == [(settings.root_dir, True)]


def test_interrupted_command_resumes_only_remaining_target(settings):
    panel = FakePanel(
        PanelCommand(False, False, 3, "Лист1", False),
        recovered=ProgressSnapshot(created=1, captured=1, inspected=1),
    )
    state = CommandState(
        command_id="cmd-resume",
        target=3,
        worksheet="Лист1",
        retry_manual=False,
        phase="running",
        started_at="2026-07-19T10:00:00+00:00",
        history_row=2,
    )
    controller = InstantController(settings, panel)
    controller._state = state

    controller.tick()
    assert controller._worker is not None
    controller._worker.join(timeout=2)
    controller.tick()

    assert controller.remaining_values == [2]
    assert controller.remaining_inspected_values == [5]
    assert panel.history_appends == 0
    assert panel.finishes[0]["created"] == 3


def test_stop_is_durable_and_does_not_restart_after_controller_reboot(settings):
    panel = FakePanel(PanelCommand(False, True, 5, "Лист1", False))
    controller = InstantController(settings, panel)
    controller._state = CommandState(
        command_id="cmd-stop",
        target=5,
        worksheet="Лист1",
        retry_manual=False,
        phase="running",
        started_at="2026-07-19T10:00:00+00:00",
        history_row=2,
    )

    controller.tick()

    assert panel.stop_acks == 1
    assert (settings.data_dir / "STOP").exists()
    saved = json.loads(controller.state_path.read_text(encoding="utf-8"))
    assert saved["stop_requested"] is True
    assert saved["phase"] == "finalizing"
    assert controller.remaining_values == []

    controller.tick()
    assert panel.finishes[0]["status"] == "ОСТАНОВЛЕНО"


def test_negative_remote_limit_is_rejected_before_state_is_created(settings):
    panel = FakePanel(PanelCommand(True, False, -1, "Лист1", False))
    controller = InstantController(settings, panel)

    with pytest.raises(Exception, match="лимит"):
        controller.tick()

    assert panel.rejections
    assert not controller.state_path.exists()


def test_remote_control_accepts_unlimited_target(settings):
    panel = FakePanel(PanelCommand(True, False, 0, "Лист1", False))
    controller = InstantController(settings, panel)

    controller.tick()
    assert controller._worker is not None
    controller._worker.join(timeout=2)
    controller.tick()

    assert controller.remaining_values == [0]
    assert controller.remaining_inspected_values == [50]
    assert panel.finishes[0]["status"] == "ЗАВЕРШЕНО"


def test_remote_control_uses_explicit_inspection_limit(settings):
    panel = FakePanel(PanelCommand(True, False, 5, "Лист1", False, 7))
    controller = InstantController(settings, panel)

    controller.tick()
    assert controller._worker is not None
    controller._worker.join(timeout=2)
    controller.tick()

    assert controller.remaining_values == [5]
    assert controller.remaining_inspected_values == [7]
    assert panel.finishes[0]["max_inspected"] == 7


def test_resumed_remote_command_does_not_exceed_total_inspection_limit(settings):
    panel = FakePanel(
        PanelCommand(False, False, 5, "Лист1", False, 10),
        recovered=ProgressSnapshot(created=2, captured=2, inspected=7),
    )
    controller = InstantController(settings, panel)
    controller._state = CommandState(
        command_id="cmd-resume-inspection-limit",
        target=5,
        worksheet="Лист1",
        retry_manual=False,
        phase="running",
        started_at="2026-08-01T10:00:00+00:00",
        max_inspected=10,
        history_row=2,
    )

    controller.tick()
    assert controller._worker is not None
    controller._worker.join(timeout=2)
    controller.tick()

    assert controller.remaining_values == [3]
    assert controller.remaining_inspected_values == [3]


def test_expected_listing_outcomes_do_not_mark_remote_run_as_error(settings):
    panel = FakePanel(PanelCommand(False, False, 10, "Лист1", False))
    controller = InstantController(settings, panel)
    state = CommandState(
        command_id="cmd-normal-outcomes",
        target=10,
        worksheet="Лист1",
        retry_manual=False,
        phase="running",
        started_at="2026-07-19T10:00:00+00:00",
    )
    controller._progress = ProgressSnapshot(
        created=2,
        inactive=3,
        unavailable=1,
        phone_failed=2,
        errors=0,
    )

    result = controller._classify_summary(
        state,
        RunSummary(
            run_id=state.command_id,
            requested=10,
            inactive=3,
            unavailable=1,
            phone_failed=2,
            stopped_reason="Очередь обработана: все доступные попытки завершены",
        ),
    )

    assert result["status"] == "ЗАВЕРШЕНО"
    assert "Общая воронка запуска" in result["message"]
    assert "Итог: Очередь обработана" in result["message"]


def test_remote_run_reports_time_deferred_status(settings):
    panel = FakePanel(PanelCommand(False, False, 1, "Лист1", False))
    controller = InstantController(settings, panel)
    state = CommandState(
        command_id="cmd-time-deferred",
        target=1,
        worksheet="Лист1",
        retry_manual=False,
        phase="running",
        started_at="2026-08-01T18:19:40+00:00",
    )

    result = controller._classify_summary(
        state,
        RunSummary(
            run_id=state.command_id,
            requested=1,
            stopped_reason=(
                "Отложено по времени: для всех доступных строк сейчас нет "
                "безопасного местного окна 10:00–19:45"
            ),
        ),
    )

    assert result["status"] == "ОТЛОЖЕНО ПО ВРЕМЕНИ"
    assert "10:00–19:45" in result["message"]


def test_crm_sync_warning_is_not_a_remote_technical_failure(settings):
    panel = FakePanel(PanelCommand(False, False, 10, "Лист1", False))
    controller = InstantController(settings, panel)
    state = CommandState(
        command_id="cmd-sync-warning",
        target=10,
        worksheet="Лист1",
        retry_manual=False,
        phase="running",
        started_at="2026-07-19T10:00:00+00:00",
    )
    controller._progress = ProgressSnapshot(created=10, errors=0)

    result = controller._classify_summary(
        state,
        RunSummary(
            run_id=state.command_id,
            requested=10,
            created=10,
            crm_sync_errors=4,
            stopped_reason="Достигнут заданный лимит",
        ),
    )

    assert result["status"] == "ЗАВЕРШЕНО С ПРЕДУПРЕЖДЕНИЯМИ"
    assert result["errors"] == 0
    assert result["crm_sync_errors"] == 4


def test_remote_controller_detects_an_updated_source_version(settings):
    project_file = settings.root_dir / "pyproject.toml"
    project_file.write_text(
        '[project]\nname = "avito-crm-pipeline"\nversion = "99.0.0"\n',
        encoding="utf-8",
    )
    controller = RemoteController(
        settings,
        FakePanel(PanelCommand(False, False, 0, "Лист1", False)),
    )

    assert controller._source_version_changed() is True
    with pytest.raises(RuntimeError):
        controller.tick()
    assert controller.panel.heartbeats == 0


def test_remote_panel_shows_long_crm_preflight_instead_of_generic_zero_progress(settings):
    panel = FakePanel(PanelCommand(False, False, 100, "Лист1", False))
    controller = RemoteController(settings, panel)
    state = CommandState(
        command_id="cmd-preflight",
        target=100,
        worksheet="Лист1",
        retry_manual=False,
        phase="running",
        started_at="2026-07-30T07:03:00+00:00",
        history_row=2,
    )

    class AliveWorker:
        @staticmethod
        def is_alive():
            return True

    controller._state = state
    controller._worker = AliveWorker()
    controller._phase_callback(
        state,
        "Синхронизация CRM: 17/223; обновлено 16, предупреждений 0.",
    )

    controller.tick()

    _command_id, _progress, details = panel.active_updates[-1]
    assert details["status"] == "СИНХРОНИЗАЦИЯ CRM"
    assert details["message"] == ("Синхронизация CRM: 17/223; обновлено 16, предупреждений 0.")
