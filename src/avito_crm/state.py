from __future__ import annotations

import json
import os
import sqlite3
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from avito_crm.errors import InstanceAlreadyRunning
from avito_crm.models import QueueItem, QueuePatch, RunSummary


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
                inspected INTEGER NOT NULL DEFAULT 0,
                processed INTEGER NOT NULL DEFAULT 0,
                rounds INTEGER NOT NULL DEFAULT 0,
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
            "processed": "INTEGER NOT NULL DEFAULT 0",
            "rounds": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in run_columns.items():
            if name not in columns:
                self.connection.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
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
        self.connection.execute(
            """
            INSERT INTO items(
                canonical_url, source_name, row_id, status, phone, crm_lead_id,
                error, attempts, run_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(canonical_url) DO UPDATE SET
                source_name=excluded.source_name,
                row_id=excluded.row_id,
                status=excluded.status,
                phone=excluded.phone,
                crm_lead_id=excluded.crm_lead_id,
                error=excluded.error,
                attempts=excluded.attempts,
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
                manual_required=manual_required + ?, inspected=inspected + ?,
                processed=processed + ?, rounds=rounds + ?, stopped_reason=?
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
                summary.inspected,
                summary.processed,
                summary.rounds,
                summary.stopped_reason,
                summary.run_id,
            ),
        )
        self.connection.commit()

    def latest_run(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT 1"
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
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            else:
                raise InstanceAlreadyRunning(
                    "Другой процесс avito-crm уже работает. Используйте `avito-crm status`."
                ) from None
        payload = json.dumps({"pid": os.getpid(), "started_at": utc_now()}).encode()
        os.write(descriptor, payload)
        os.close(descriptor)
        self.acquired = True
        return self

    def _clear_stale(self) -> bool:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            pid = int(payload["pid"])
            os.kill(pid, 0)
            return False
        except PermissionError:
            return False
        except (ProcessLookupError, ValueError, KeyError, json.JSONDecodeError, OSError):
            self.path.unlink(missing_ok=True)
            return True

    def __exit__(self, *_args: object) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)
            self.acquired = False
