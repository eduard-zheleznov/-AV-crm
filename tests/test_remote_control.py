from __future__ import annotations

import json

import pytest

from avito_crm.models import RunSummary
from avito_crm.remote_control import (
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

    def get(self, _range_name):
        return [row[:] for row in self.values]

    def batch_update(self, data, **_kwargs):
        self.batch.extend(data)

    def update(self, values, _range_name, **_kwargs):
        self.values = [row[:] for row in values]

    def get_all_values(self):
        return [row[:] for row in self.values]


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
            False, self.command.stop, self.command.limit, self.command.worksheet, False
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

    def _run_worker(self, state, remaining):
        self.remaining_values.append(remaining)
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
    panel = GoogleControlPanel(settings, spreadsheet=object(), worksheet_not_found=KeyError)
    panel.control = MatrixSheet(values)

    command = panel.read_command()

    assert command == PanelCommand(True, False, 3, "Новые", True)


def test_existing_user_control_sheet_is_never_overwritten(settings):
    existing = MatrixSheet([["Мои важные данные"]])
    spreadsheet = FakeSpreadsheet({settings.google_control_worksheet: existing})
    panel = GoogleControlPanel(settings, spreadsheet, MissingWorksheet)

    with pytest.raises(Exception, match="уже занят"):
        panel.ensure_layout()

    assert existing.values == [["Мои важные данные"]]


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
    assert panel.finishes[0]["status"] == "ЗАВЕРШЕНО"
    assert panel.finishes[0]["created"] == 2
    assert not controller.state_path.exists()

    controller.tick()
    assert len(panel.claims) == 1


def test_interrupted_command_resumes_only_remaining_target(settings):
    panel = FakePanel(
        PanelCommand(False, False, 3, "Лист1", False),
        recovered=ProgressSnapshot(created=1, captured=1),
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


def test_invalid_remote_limit_is_rejected_before_state_is_created(settings):
    panel = FakePanel(PanelCommand(True, False, 1001, "Лист1", False))
    controller = InstantController(settings, panel)

    with pytest.raises(Exception, match="лимит"):
        controller.tick()

    assert panel.rejections
    assert not controller.state_path.exists()
