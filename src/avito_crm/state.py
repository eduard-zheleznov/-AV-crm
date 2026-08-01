from __future__ import annotations

import ctypes
import json
import os
import sqlite3
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from avito_crm.errors import InstanceAlreadyRunning
from avito_crm.models import QueueItem, QueuePatch, RunSummary

_IS_WINDOWS = os.name == "nt"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                requested INTEGER NOT NULL,
                captured INTEGER NOT NULL DEFAULT 0,
                created INTEGER NOT NULL DEFAULT 0,
                duplicates INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0,
                invalid INTEGER NOT NULL DEFAULT 0,
                inactive INTEGER NOT NULL DEFAULT 0,
                unavailable INTEGER NOT NULL DEFAULT 0,
                phone_failed INTEGER NOT NULL DEFAULT 0,
                retries INTEGER NOT NULL DEFAULT 0,
                manual_required INTEGER NOT NULL DEFAULT 0,
                captchas_solved INTEGER NOT NULL DEFAULT 0,
                inspected INTEGER NOT NULL DEFAULT 0,
                processed INTEGER NOT NULL DEFAULT 0,
                rounds INTEGER NOT NULL DEFAULT 0,
                stage_synced INTEGER NOT NULL DEFAULT 0,
                no_answer_synced INTEGER NOT NULL DEFAULT 0,
                repeat_created INTEGER NOT NULL DEFAULT 0,
                repeat_exhausted INTEGER NOT NULL DEFAULT 0,
                crm_sync_errors INTEGER NOT NULL DEFAULT 0,
                stopped_reason TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS items (
                canonical_url TEXT PRIMARY KEY,
                source_name TEXT NOT NULL,
                row_id TEXT NOT NULL,
                status TEXT NOT NULL,
                phone TEXT NOT NULL DEFAULT '',
                crm_lead_id TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                funnel_stage TEXT NOT NULL DEFAULT '',
                crm_create_count INTEGER NOT NULL DEFAULT 0,
                repeat_crm_lead_id TEXT NOT NULL DEFAULT '',
                repeat_phone_attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT NOT NULL DEFAULT '',
                run_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
            CREATE INDEX IF NOT EXISTS idx_items_phone ON items(phone);
            """
        )
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(runs)").fetchall()}
        run_columns = {
            "captured": "INTEGER NOT NULL DEFAULT 0",
            "inactive": "INTEGER NOT NULL DEFAULT 0",
            "unavailable": "INTEGER NOT NULL DEFAULT 0",
            "phone_failed": "INTEGER NOT NULL DEFAULT 0",
            "retries": "INTEGER NOT NULL DEFAULT 0",
            "captchas_solved": "INTEGER NOT NULL DEFAULT 0",
            "processed": "INTEGER NOT NULL DEFAULT 0",
            "rounds": "INTEGER NOT NULL DEFAULT 0",
            "stage_synced": "INTEGER NOT NULL DEFAULT 0",
            "no_answer_synced": "INTEGER NOT NULL DEFAULT 0",
            "repeat_created": "INTEGER NOT NULL DEFAULT 0",
            "repeat_exhausted": "INTEGER NOT NULL DEFAULT 0",
            "crm_sync_errors": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in run_columns.items():
            if name not in columns:
                self.connection.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
        item_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(items)").fetchall()
        }
        item_migrations = {
            "funnel_stage": "TEXT NOT NULL DEFAULT ''",
            "crm_create_count": "INTEGER NOT NULL DEFAULT 0",
            "repeat_crm_lead_id": "TEXT NOT NULL DEFAULT ''",
            "repeat_phone_attempts": "INTEGER NOT NULL DEFAULT 0",
            "next_retry_at": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in item_migrations.items():
            if name not in item_columns:
                self.connection.execute(f"ALTER TABLE items ADD COLUMN {name} {definition}")
        self.connection.commit()

    def begin_run(self, summary: RunSummary) -> None:
        self.connection.execute(
            """
            INSERT INTO runs(run_id, started_at, requested) VALUES (?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                finished_at=NULL,
                requested=MAX(runs.requested, excluded.requested),
                stopped_reason=''
            """,
            (summary.run_id, utc_now(), summary.requested),
        )
        self.connection.commit()

    def record_item(
        self,
        canonical_url: str,
        source_name: str,
        item: QueueItem,
        patch: QueuePatch,
    ) -> None:
        previous = self.get_item(canonical_url) or {}
        funnel_stage = (
            patch.funnel_stage
            if patch.funnel_stage is not None
            else str(previous.get("funnel_stage", ""))
        )
        crm_create_count = (
            patch.crm_create_count
            if patch.crm_create_count is not None
            else _safe_int(previous.get("crm_create_count"))
        )
        repeat_crm_lead_id = (
            patch.repeat_crm_lead_id
            if patch.repeat_crm_lead_id is not None
            else str(previous.get("repeat_crm_lead_id", ""))
        )
        repeat_phone_attempts = (
            patch.repeat_phone_attempts
            if patch.repeat_phone_attempts is not None
            else _safe_int(previous.get("repeat_phone_attempts"))
        )
        next_retry_at = (
            patch.next_retry_at
            if patch.next_retry_at is not None
            else str(previous.get("next_retry_at", ""))
        )
        self.connection.execute(
            """
            INSERT INTO items(
                canonical_url, source_name, row_id, status, phone, crm_lead_id,
                error, attempts, funnel_stage, crm_create_count,
                repeat_crm_lead_id, repeat_phone_attempts, next_retry_at,
                run_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(canonical_url) DO UPDATE SET
                source_name=excluded.source_name,
                row_id=excluded.row_id,
                status=excluded.status,
                phone=excluded.phone,
                crm_lead_id=excluded.crm_lead_id,
                error=excluded.error,
                attempts=excluded.attempts,
                funnel_stage=excluded.funnel_stage,
                crm_create_count=excluded.crm_create_count,
                repeat_crm_lead_id=excluded.repeat_crm_lead_id,
                repeat_phone_attempts=excluded.repeat_phone_attempts,
                next_retry_at=excluded.next_retry_at,
                run_id=excluded.run_id,
                updated_at=excluded.updated_at
            """,
            (
                canonical_url,
                source_name,
                item.row_id,
                patch.status,
                patch.phone,
                patch.crm_lead_id,
                patch.error,
                patch.attempts,
                str(funnel_stage),
                int(crm_create_count),
                str(repeat_crm_lead_id),
                int(repeat_phone_attempts),
                str(next_retry_at),
                patch.run_id,
                utc_now(),
            ),
        )
        self.connection.commit()

    def get_item(self, canonical_url: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM items WHERE canonical_url = ?", (canonical_url,)
        ).fetchone()
        return dict(row) if row else None

    def finish_run(self, summary: RunSummary) -> None:
        self.connection.execute(
            """
            UPDATE runs SET finished_at=?, captured=captured + ?, created=created + ?,
                duplicates=duplicates + ?, errors=errors + ?, invalid=invalid + ?,
                inactive=inactive + ?, unavailable=unavailable + ?,
                phone_failed=phone_failed + ?, retries=retries + ?,
                manual_required=manual_required + ?, captchas_solved=captchas_solved + ?,
                inspected=inspected + ?,
                processed=processed + ?, rounds=rounds + ?, stopped_reason=?
                , stage_synced=stage_synced + ?, no_answer_synced=no_answer_synced + ?,
                repeat_created=repeat_created + ?,
                repeat_exhausted=repeat_exhausted + ?, crm_sync_errors=crm_sync_errors + ?
            WHERE run_id=?
            """,
            (
                utc_now(),
                summary.captured,
                summary.created,
                summary.duplicates,
                summary.errors,
                summary.invalid,
                summary.inactive,
                summary.unavailable,
                summary.phone_failed,
                summary.retries,
                summary.manual_required,
                summary.captchas_solved,
                summary.inspected,
                summary.processed,
                summary.rounds,
                summary.stopped_reason,
                summary.stage_synced,
                summary.no_answer_synced,
                summary.repeat_created,
                summary.repeat_exhausted,
                summary.crm_sync_errors,
                summary.run_id,
            ),
        )
        self.connection.commit()

    def latest_run(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row else None

    def totals(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT status, COUNT(*) AS count FROM items GROUP BY status"
        ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _safe_int(value: Any) -> int:
    try:
        return int(float(str(value or "0").strip()))
    except (TypeError, ValueError):
        return 0


def _windows_process_is_running(pid: int) -> bool:
    """Check a PID without relying on os.kill(), which is unreliable on Windows."""
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    synchronize = 0x00100000
    still_active = 259
    error_access_denied = 5
    missing_process_errors = {87, 1168}

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(
        process_query_limited_information | synchronize,
        False,
        pid,
    )
    if not handle:
        error_code = ctypes.get_last_error()
        if error_code in missing_process_errors:
            return False
        if error_code == error_access_denied:
            return True
        # Unknown errors are treated conservatively as a live process.
        return True

    exit_code = wintypes.DWORD()
    try:
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            # Never remove a lock when Windows cannot prove that its owner exited.
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _process_is_running(pid: int) -> bool:
    if pid <= 0 or pid > 0xFFFFFFFF:
        return False
    if pid == os.getpid():
        return True
    if _IS_WINDOWS:
        return _windows_process_is_running(pid)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except (OSError, SystemError):
        return False
    return True


class SingleInstanceLock(AbstractContextManager["SingleInstanceLock"]):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False

    def __enter__(self) -> SingleInstanceLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if self._clear_stale():
                try:
                    descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    raise InstanceAlreadyRunning(
                        "Другой процесс avito-crm уже запустился. Повторите проверку статуса."
                    ) from None
            else:
                raise InstanceAlreadyRunning(
                    "Другой процесс avito-crm уже работает. Используйте `avito-crm status`."
                ) from None
        payload = json.dumps({"pid": os.getpid(), "started_at": utc_now()}).encode()
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        self.acquired = True
        return self

    def _clear_stale(self) -> bool:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            pid = int(payload["pid"])
        except (ValueError, KeyError, json.JSONDecodeError, OSError):
            return self._remove_stale_file()
        if _process_is_running(pid):
            return False
        return self._remove_stale_file()

    def _remove_stale_file(self) -> bool:
        try:
            self.path.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    def __exit__(self, *_args: object) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)
            self.acquired = False
