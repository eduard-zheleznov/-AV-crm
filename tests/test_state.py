import json
import os
import sqlite3

import pytest

import avito_crm.state as state_module
from avito_crm.errors import InstanceAlreadyRunning
from avito_crm.models import ItemStatus, QueueItem, QueuePatch, RunSummary
from avito_crm.state import SingleInstanceLock, StateStore


def test_state_store_records_resumable_item(tmp_path):
    with StateStore(tmp_path / "state.sqlite3") as state:
        summary = RunSummary(run_id="run-1", requested=2, captured=1)
        state.begin_run(summary)
        item = QueueItem("2", "https://www.avito.ru/moskva/item_123456789")
        patch = QueuePatch(
            status=ItemStatus.CAPTURED,
            attempts=1,
            phone="+79991234567",
            run_id="run-1",
        )
        state.record_item(item.url, "test", item, patch)
        state.finish_run(summary)

        assert state.get_item(item.url)["status"] == ItemStatus.CAPTURED
        assert state.latest_run()["captured"] == 1


def test_state_store_persists_outcome_and_round_counters(tmp_path):
    with StateStore(tmp_path / "state.sqlite3") as state:
        summary = RunSummary(
            run_id="run-outcomes",
            requested=5,
            inactive=1,
            unavailable=1,
            phone_failed=1,
            retries=2,
            processed=4,
            inspected=6,
            rounds=3,
            captchas_solved=2,
        )
        state.begin_run(summary)
        state.finish_run(summary)
        run = state.latest_run()

    assert run["inactive"] == 1
    assert run["unavailable"] == 1
    assert run["phone_failed"] == 1
    assert run["retries"] == 2
    assert run["processed"] == 4
    assert run["rounds"] == 3
    assert run["captchas_solved"] == 2


def test_state_store_migrates_robot_handoff_due_date_without_losing_cache(tmp_path):
    database = tmp_path / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE robot_handoffs (
                lead_id TEXT PRIMARY KEY,
                record_key TEXT NOT NULL,
                phone TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO robot_handoffs(
                lead_id, record_key, phone, status, error, updated_at
            ) VALUES ('700', 'record-1', '+79991234567', 'recognized', '', 'now')
            """
        )

    with StateStore(database) as state:
        cached = state.get_robot_handoff(700)
        columns = {
            row[1]
            for row in state.connection.execute("PRAGMA table_info(robot_handoffs)").fetchall()
        }

    assert "stage_due_date" in columns
    assert cached["phone"] == "+79991234567"
    assert cached["stage_due_date"] == ""


def test_state_store_migrates_existing_manual_notification_without_resending(tmp_path):
    database = tmp_path / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE robot_handoffs (
                lead_id TEXT PRIMARY KEY,
                record_key TEXT NOT NULL,
                phone TEXT NOT NULL DEFAULT '',
                stage_due_date TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO robot_handoffs(
                lead_id, record_key, status, error, updated_at
            ) VALUES ('700', 'legacy-record', 'manual_required', 'ambiguous', 'now')
            """
        )

    with StateStore(database) as state:
        first_claim = state.claim_robot_handoff_notification(
            700, "stable-record", "manual_required"
        )
        repeated_claim = state.claim_robot_handoff_notification(
            700, "stable-record", "manual_required"
        )
        new_record_claim = state.claim_robot_handoff_notification(
            700, "new-record", "manual_required"
        )
        state.release_robot_handoff_notification(700, "new-record", "manual_required")
        retry_after_failed_delivery = state.claim_robot_handoff_notification(
            700, "new-record", "manual_required"
        )

    assert first_claim is False
    assert repeated_claim is False
    assert new_record_claim is True
    assert retry_after_failed_delivery is True


def test_state_store_accumulates_a_resumed_run(tmp_path):
    with StateStore(tmp_path / "state.sqlite3") as state:
        first = RunSummary(run_id="remote-1", requested=3, created=1, captured=1)
        state.begin_run(first)
        state.finish_run(first)

        resumed = RunSummary(run_id="remote-1", requested=2, created=2, captured=2)
        state.begin_run(resumed)
        state.finish_run(resumed)

        run = state.latest_run()

    assert run["requested"] == 3
    assert run["created"] == 3
    assert run["captured"] == 3


def test_single_instance_lock_cleans_up(tmp_path):
    lock_path = tmp_path / "worker.lock"
    with SingleInstanceLock(lock_path):
        assert lock_path.exists()
    assert not lock_path.exists()


def test_single_instance_lock_clears_stale_windows_lock(tmp_path, monkeypatch):
    lock_path = tmp_path / "worker.lock"
    lock_path.write_text(
        json.dumps({"pid": 999_999_999, "started_at": "2026-07-29T00:00:00+00:00"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(state_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(state_module, "_windows_process_is_running", lambda _pid: False)

    with SingleInstanceLock(lock_path):
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()

    assert not lock_path.exists()


def test_single_instance_lock_keeps_live_windows_lock(tmp_path, monkeypatch):
    lock_path = tmp_path / "worker.lock"
    lock_path.write_text(
        json.dumps({"pid": 1234, "started_at": "2026-07-29T00:00:00+00:00"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(state_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(state_module, "_windows_process_is_running", lambda _pid: True)

    with pytest.raises(InstanceAlreadyRunning):
        SingleInstanceLock(lock_path).__enter__()

    assert lock_path.exists()


def test_single_instance_lock_recovers_from_system_error_during_pid_probe(tmp_path, monkeypatch):
    lock_path = tmp_path / "worker.lock"
    lock_path.write_text(
        json.dumps({"pid": 1234, "started_at": "2026-07-29T00:00:00+00:00"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(state_module, "_IS_WINDOWS", False)

    def broken_kill(_pid, _signal):
        raise SystemError("<built-in function kill> returned a result with an exception set")

    monkeypatch.setattr(state_module.os, "kill", broken_kill)

    with SingleInstanceLock(lock_path):
        assert json.loads(lock_path.read_text(encoding="utf-8"))["pid"] == os.getpid()
