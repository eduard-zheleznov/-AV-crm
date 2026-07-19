from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values

from avito_crm.config import parse_chat_ids, parse_email_addresses, parse_reminder_minutes
from avito_crm.errors import ConfigurationError

SPREADSHEET_URL_RE = re.compile(
    r"https?://docs\.google\.com/spreadsheets/(?:u/\d+/)?d/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
SPREADSHEET_ID_RE = re.compile(r"[A-Za-z0-9_-]{20,}")


def extract_spreadsheet_id(value: str) -> str:
    """Accept a Google Sheets URL or a raw spreadsheet ID."""
    candidate = value.strip().strip('"').strip("'")
    match = SPREADSHEET_URL_RE.search(candidate)
    if match:
        return match.group(1)
    if SPREADSHEET_ID_RE.fullmatch(candidate):
        return candidate
    raise ValueError("Вставьте полную ссылку Google Sheets или ID таблицы")


def google_sheet_url(spreadsheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"


def read_env_values(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    return {
        str(key): str(value or "")
        for key, value in dotenv_values(path, encoding="utf-8-sig").items()
        if key
    }


def update_env_values(path: Path, updates: Mapping[str, str]) -> None:
    """Atomically update selected .env keys without exposing or replacing secrets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    newline = "\r\n" if "\r\n" in original else "\n"
    lines = original.splitlines()
    pending = {key: _encode_env_value(_single_line(value)) for key, value in updates.items()}

    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key in pending:
            lines[index] = f"{key}={pending.pop(key)}"

    if pending:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Settings saved by the desktop launcher.")
        lines.extend(f"{key}={value}" for key, value in pending.items())

    content = newline.join(lines)
    if content and not content.endswith(newline):
        content += newline

    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8-sig", newline="") as stream:
            stream.write(content)
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def service_account_email(path: Path) -> str:
    if not path.is_file():
        raise ValueError("Файл JSON сервисного аккаунта не найден")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Не удалось прочитать JSON сервисного аккаунта") from exc
    email = str(payload.get("client_email", "")).strip()
    private_key = str(payload.get("private_key", "")).strip()
    if payload.get("type") != "service_account" or not email or not private_key:
        raise ValueError("Выбранный JSON не является ключом Google service account")
    return email


def parse_limit(value: str) -> int:
    try:
        limit = int(value.strip())
    except ValueError as exc:
        raise ValueError("Лимит должен быть целым числом") from exc
    if not 1 <= limit <= 10_000:
        raise ValueError("Лимит должен быть от 1 до 10000")
    return limit


def parse_telegram_chat_ids(value: str) -> tuple[str, ...]:
    try:
        return parse_chat_ids(value)
    except ConfigurationError as exc:
        raise ValueError(str(exc)) from exc


def parse_telegram_reminders(value: str) -> tuple[float, ...]:
    try:
        reminders = parse_reminder_minutes(value)
    except ConfigurationError as exc:
        raise ValueError(str(exc)) from exc
    if not reminders:
        raise ValueError("Укажите хотя бы одно Telegram-напоминание")
    return reminders


def parse_notification_emails(value: str) -> tuple[str, ...]:
    try:
        return parse_email_addresses(value)
    except ConfigurationError as exc:
        raise ValueError(str(exc)) from exc


def parse_smtp_port(value: str) -> int:
    try:
        port = int(value.strip())
    except ValueError as exc:
        raise ValueError("SMTP-порт должен быть целым числом") from exc
    if not 1 <= port <= 65_535:
        raise ValueError("SMTP-порт должен быть от 1 до 65535")
    return port


def parse_captcha_wait_hours(value: str) -> float:
    try:
        hours = float(value.strip().replace(",", "."))
    except ValueError as exc:
        raise ValueError("Время ожидания капчи должно быть числом часов") from exc
    if not 1 <= hours <= 168:
        raise ValueError("Время ожидания капчи должно быть от 1 до 168 часов")
    return hours


def _single_line(value: str) -> str:
    result = str(value)
    if "\r" in result or "\n" in result:
        raise ValueError("Значение .env должно занимать одну строку")
    return result.strip()


def _encode_env_value(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
