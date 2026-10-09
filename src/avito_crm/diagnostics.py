"""Read-only, allowlisted support reports; never copy raw logs or configuration."""

from __future__ import annotations

import hashlib
import json
import platform
import re
import sys
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from avito_crm import __version__
from avito_crm.chrome_extension import EXPECTED_EXTENSION_VERSION
from avito_crm.gui_config import read_env_values
from avito_crm.state import _process_is_running

EVENTS = frozenset(
    {
        "command_created",
        "command_dispatched",
        "command_timeout",
        "extension_connected",
        "extension_status",
        "extension_result",
        "extension_restarted_during_command",
    }
)
STATUSES = frozenset(
    {
        "command_progress",
        "healthy",
        "browser_infra",
        "manual_required",
        "manual_cleared",
        "manual_reminder",
        "cancelled",
        "phone",
        "screen_capture",
        "phone_error",
        "phone_missing",
        "listing_mismatch",
        "clicking",
        "click_dispatched",
        "click_recovery",
        "reveal_confirmed",
    }
)
STAGES = frozenset(
    {
        "preparing_tab",
        "navigation_lookup",
        "navigation_reload",
        "navigation_update",
        "navigation_probe",
        "waiting_content_reply",
        "navigation",
        *[f"tabs.{v}" for v in ("get", "query", "create", "update", "reload", "remove")],
        *[f"windows.{v}" for v in ("getAll", "create", "update", "remove")],
        "extension.isAllowedIncognitoAccess",
        "scripting.executeScript",
        "runtime.getPlatformInfo",
    }
)
ISSUES = {
    "extension_no_reply": "нет ответа расширения",
    "browser_recovery_failed": "Автоматическое восстановление окна Avito не удалось",
    "page_not_rendered": "Chrome не отобразил Avito",
    "bridge_not_connected": "не подключилось к локальному мосту",
    "crm_stage_missing": "Шаг автоответчика не найден однозначно",
    "controller_cycle_error": "Ошибка цикла удалённого пульта",
    "worker_exit": "Рабочий процесс пульта завершился",
}


def _read_json(path: Path) -> dict:
    try:
        if path.stat().st_size > 65536:
            return {}
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _tail(path: Path, maximum: int = 131072) -> list[str]:
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - maximum))
            data = stream.read(maximum).decode("utf-8", errors="replace")
        return data.splitlines()[-300:]
    except OSError:
        return []


def _version(value: object) -> str:
    return str(value) if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){1,3}", str(value)) else "unknown"


def _time(value: object) -> str:
    try:
        return datetime.fromisoformat(str(value)).isoformat(timespec="seconds")
    except (ValueError, OverflowError):
        return "unknown"


def _hash(value: object) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()[:12] if value else ""


def _lock(path: Path) -> dict:
    value = _read_json(path)
    pid = value.get("pid")
    running = _process_is_running(pid) if type(pid) is int and pid > 0 else False
    return {
        "file_present": path.is_file(),
        "process_running": running,
        "started_at": _time(value.get("started_at")),
    }


def _bridge(values: dict[str, str]) -> dict:
    try:
        port = int(values.get("AVITO_EXTENSION_PORT", "8765"))
        if not 1024 <= port <= 65535 or not values.get("AVITO_EXTENSION_TOKEN"):
            return {"state": "not_configured"}
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/health",
            headers={"Authorization": "Bearer " + values["AVITO_EXTENSION_TOKEN"]},
        )
        # Never send the local bearer token through a user-configured proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=2) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            return {"state": "invalid_response"}
        health = json.loads(raw)
        if not isinstance(health, dict):
            return {"state": "invalid_response"}
        return {
            "state": "reachable",
            "connected": health.get("connected") is True,
            "extension_version": _version(health.get("extension_version")),
            "command_active": health.get("command_active") is True,
            "command_dispatched": health.get("command_dispatched") is True,
        }
    except urllib.error.HTTPError as exc:
        return {"state": "auth_failed" if exc.code == 401 else "http_error"}
    except (OSError, ValueError, urllib.error.URLError):
        return {"state": "unavailable"}


def _health_records(root: Path) -> list[dict]:
    records = []
    for line in _tail(root / "logs" / "chrome-extension-health.jsonl"):
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict) or raw.get("event") not in EVENTS:
                continue
            record = {
                "timestamp": _time(raw.get("timestamp")),
                "event": raw["event"],
                "command": _hash(raw.get("command_id")),
                "instance": _hash(raw.get("extension_instance")),
            }
            if raw.get("command_type") in {"health_probe", "reveal_phone"}:
                record["command_type"] = raw["command_type"]
            if raw.get("status") in STATUSES:
                record["status"] = raw["status"]
            row = raw.get("row_id")
            if str(row).isdigit() and len(str(row)) <= 7:
                record["row"] = int(row)
            diagnostic = raw.get("diagnostics")
            if isinstance(diagnostic, dict) and diagnostic.get("stage") in STAGES:
                record["stage"] = diagnostic["stage"]
            records.append(record)
        except (ValueError, TypeError):
            continue
    return records[-80:]


def collect_report(root: Path) -> dict:
    root = root.resolve()
    try:
        values = read_env_values(root / ".env")
    except (OSError, ValueError):
        values = {}
    driver = values.get("AVITO_BROWSER_DRIVER", "playwright")
    if driver not in {"chrome_extension", "playwright"}:
        driver = "unknown"
    state = _read_json(root / "data" / "remote-control.json")
    issues = []
    for line in _tail(root / "logs" / "avito-crm.log"):
        recognized = False
        for code, marker in ISSUES.items():
            if marker in line:
                recognized = True
                issues.append({"timestamp": _time(line[:23].replace(",", ".")), "code": code})
        if not recognized and re.search(r"\b(?:ERROR|CRITICAL)\b", line):
            issues.append({"timestamp": _time(line[:23].replace(",", ".")), "code": "other_error"})
    return {
        "schema": 1,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "app_version": __version__,
        "expected_extension_version": EXPECTED_EXTENSION_VERSION,
        "installed_extension_files": _version(
            _read_json(root / "chrome-extension" / "manifest.json").get("version")
        ),
        "system": {"os": platform.system(), "python": platform.python_version()},
        "configuration": {
            "env_present": (root / ".env").is_file(),
            "driver": driver,
            "extension_token_present": bool(values.get("AVITO_EXTENSION_TOKEN")),
            "google_configured": bool(values.get("GOOGLE_SPREADSHEET_ID")),
            "notifications": {
                channel.lower(): {
                    "enabled": values.get(f"{channel}_NOTIFICATIONS_ENABLED", "true")
                    .strip()
                    .lower()
                    in {"1", "true", "yes", "on"},
                }
                for channel in ("MAX", "EMAIL", "TELEGRAM")
            },
        },
        "controller": _lock(root / "data" / "remote-control.lock"),
        "worker": _lock(root / "data" / "worker.lock"),
        "stop_requested": (root / "data" / "STOP").exists(),
        "pending_command": bool(state.get("command_id")),
        "bridge": _bridge(values) if driver == "chrome_extension" else {"state": "not_used"},
        "recent_issues": issues[-40:],
        "chrome_events": _health_records(root),
    }


def save_report(root: Path) -> Path:
    report = collect_report(root)
    destination = root / "output" / "diagnostics"
    destination.mkdir(parents=True, exist_ok=True)
    name = datetime.now(UTC).strftime("avito-diagnostics-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    archive = destination / f"{name}.zip"
    # Exclusive creation: never overwrite another report.
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("report.json", json.dumps(report, ensure_ascii=False, indent=2))
        bundle.writestr(
            "README.txt",
            "Диагностика Avito → CRM\n\n"
            "Передайте этот архив в чат поддержки. Проверка не запускала очередь, не открывала "
            "номера и не изменяла CRM или настройки.\n"
            "Исходные логи, .env, телефоны, получатели уведомлений и ключи не включены.\n"
            "Локальный мост может быть недоступен, когда рабочий процесс не запущен — "
            "это само по себе не ошибка. Версия файлов расширения не доказывает версию, "
            "загруженную в Chrome: для этого смотрите bridge.extension_version.\n",
        )
    return archive


def show_report_in_folder(path: Path) -> None:
    import subprocess

    if sys.platform == "win32":
        subprocess.Popen(["explorer.exe", "/select,", str(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])
