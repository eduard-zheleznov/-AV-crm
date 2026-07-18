from __future__ import annotations

import json
import logging
import socket
import time
from dataclasses import dataclass
from datetime import datetime

import httpx

from avito_crm.config import Settings
from avito_crm.errors import ConfigurationError, NotificationError

LOGGER = logging.getLogger(__name__)
TELEGRAM_API_ROOT = "https://api.telegram.org"
TELEGRAM_MESSAGE_LIMIT = 4096


@dataclass(frozen=True, slots=True)
class TelegramChat:
    chat_id: str
    label: str


class TelegramNotifier:
    """Small Telegram Bot API client that never exposes the bot token in errors."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.token = settings.telegram_bot_token
        self.primary_chat_ids = settings.telegram_primary_chat_ids
        self.backup_chat_ids = settings.telegram_backup_chat_ids
        self.computer_name = settings.notification_computer_name or socket.gethostname()
        self.send_attempts = settings.telegram_send_attempts
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(settings.telegram_request_timeout, connect=10.0),
            follow_redirects=False,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.primary_chat_ids)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> TelegramNotifier:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def send(self, text: str, chat_ids: tuple[str, ...]) -> int:
        if not self.token:
            raise ConfigurationError("Не задан TELEGRAM_BOT_TOKEN")
        recipients = _deduplicate(chat_ids)
        if not recipients:
            raise ConfigurationError("Не указаны Chat ID получателей Telegram")

        sent = 0
        failures: list[str] = []
        for chat_id in recipients:
            try:
                for chunk in split_telegram_text(text):
                    self._call(
                        "sendMessage",
                        {
                            "chat_id": chat_id,
                            "text": chunk,
                            "disable_web_page_preview": True,
                        },
                    )
                sent += 1
            except NotificationError as exc:
                failures.append(f"{_mask_chat_id(chat_id)}: {exc}")

        if failures:
            raise NotificationError(
                f"сообщение доставлено в {sent} из {len(recipients)} чатов; " + "; ".join(failures)
            )
        return sent

    def send_captcha_detected(self, *, reason: str, url: str, wait_seconds: float) -> int:
        return self.send(
            "\n".join(
                (
                    "🚫 Avito остановил очередь для ручной проверки.",
                    f"Компьютер: {self.computer_name}",
                    f"Причина: {reason}",
                    f"Ссылка: {url}",
                    f"Ожидание: до {_format_duration(wait_seconds)}.",
                    "Откройте удалённый компьютер и завершите проверку в браузере. "
                    "После этого программа продолжит работу автоматически.",
                )
            ),
            self.primary_chat_ids,
        )

    def send_captcha_reminder(
        self,
        *,
        reason: str,
        url: str,
        elapsed_seconds: float,
        escalate: bool,
    ) -> int:
        recipients = self.primary_chat_ids
        audience = "основному ответственному"
        if escalate and self.backup_chat_ids:
            recipients = _deduplicate((*self.primary_chat_ids, *self.backup_chat_ids))
            audience = "основному и резервному ответственным"
        return self.send(
            "\n".join(
                (
                    "⚠️ Капча Avito всё ещё ожидает решения.",
                    f"Компьютер: {self.computer_name}",
                    f"Прошло: {_format_duration(elapsed_seconds)}",
                    f"Причина: {reason}",
                    f"Ссылка: {url}",
                    f"Напоминание отправлено {audience}.",
                )
            ),
            recipients,
        )

    def send_captcha_resolved(
        self, *, url: str, elapsed_seconds: float, include_backup: bool
    ) -> int:
        recipients = self.primary_chat_ids
        if include_backup:
            recipients = _deduplicate((*self.primary_chat_ids, *self.backup_chat_ids))
        return self.send(
            "\n".join(
                (
                    "✅ Проверка Avito завершена, очередь продолжает работу.",
                    f"Компьютер: {self.computer_name}",
                    f"Пауза заняла: {_format_duration(elapsed_seconds)}",
                    f"Ссылка: {url}",
                )
            ),
            recipients,
        )

    def send_captcha_timeout(self, *, reason: str, url: str, wait_seconds: float) -> int:
        recipients = _deduplicate((*self.primary_chat_ids, *self.backup_chat_ids))
        return self.send(
            "\n".join(
                (
                    "🛑 Ожидание ручной проверки Avito завершилось по таймауту.",
                    f"Компьютер: {self.computer_name}",
                    f"Ожидали: {_format_duration(wait_seconds)}",
                    f"Причина: {reason}",
                    f"Ссылка: {url}",
                    "Строка сохранена для повторного запуска.",
                )
            ),
            recipients,
        )

    def send_captcha_stopped(self, *, url: str, include_backup: bool) -> int:
        recipients = self.primary_chat_ids
        if include_backup:
            recipients = _deduplicate((*self.primary_chat_ids, *self.backup_chat_ids))
        return self.send(
            "\n".join(
                (
                    "⏹ Ожидание проверки Avito остановлено оператором.",
                    f"Компьютер: {self.computer_name}",
                    f"Ссылка: {url}",
                    "Текущая строка сохранена для повторного запуска.",
                )
            ),
            recipients,
        )

    def send_test(self) -> int:
        recipients = _deduplicate((*self.primary_chat_ids, *self.backup_chat_ids))
        return self.send(
            "\n".join(
                (
                    "✅ Telegram-уведомления Avito → CRM работают.",
                    f"Компьютер: {self.computer_name}",
                    f"Проверено: {datetime.now().astimezone():%d.%m.%Y %H:%M:%S %Z}",
                )
            ),
            recipients,
        )

    def recent_chats(self) -> list[TelegramChat]:
        if not self.token:
            raise ConfigurationError("Сначала укажите TELEGRAM_BOT_TOKEN")
        payload = self._call(
            "getUpdates",
            {
                "limit": 100,
                "timeout": 0,
                "allowed_updates": json.dumps(["message", "my_chat_member"]),
            },
        )
        result = payload.get("result")
        if not isinstance(result, list):
            return []

        chats: dict[str, TelegramChat] = {}
        for update in result:
            if not isinstance(update, dict):
                continue
            message = update.get("message")
            membership = update.get("my_chat_member")
            chat = None
            if isinstance(message, dict):
                chat = message.get("chat")
            elif isinstance(membership, dict):
                chat = membership.get("chat")
            if not isinstance(chat, dict) or "id" not in chat:
                continue
            chat_id = str(chat["id"])
            label_parts = [
                str(chat.get("title", "")).strip(),
                " ".join(
                    part
                    for part in (
                        str(chat.get("first_name", "")).strip(),
                        str(chat.get("last_name", "")).strip(),
                    )
                    if part
                ),
                f"@{chat['username']}" if chat.get("username") else "",
            ]
            label = " — ".join(part for part in label_parts if part) or "без названия"
            chats[chat_id] = TelegramChat(chat_id=chat_id, label=label)
        return list(chats.values())

    def _call(self, method: str, data: dict[str, object]) -> dict[str, object]:
        url = f"{TELEGRAM_API_ROOT}/bot{self.token}/{method}"
        last_error = "неизвестная ошибка"
        for attempt in range(1, self.send_attempts + 1):
            try:
                response = self.client.post(url, data=data)
                payload = response.json()
                if response.status_code == 200 and isinstance(payload, dict) and payload.get("ok"):
                    return payload
                description = (
                    str(payload.get("description", "")) if isinstance(payload, dict) else ""
                )
                if self.token:
                    description = description.replace(self.token, "***REDACTED***")
                last_error = f"HTTP {response.status_code}: {description[:240] or 'ошибка API'}"
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc.__class__.__name__

            if attempt < self.send_attempts:
                time.sleep(min(2**attempt, 5))

        raise NotificationError(f"Telegram API недоступен ({last_error})")


def split_telegram_text(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    value = str(text or "").strip()
    if not value:
        return [" "]
    if len(value) <= limit:
        return [value]
    chunks: list[str] = []
    rest = value
    while rest:
        split_at = rest.rfind("\n", 0, limit + 1)
        if split_at < max(1, limit // 2):
            split_at = limit
        chunks.append(rest[:split_at].strip())
        rest = rest[split_at:].lstrip()
    return chunks


def _deduplicate(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _mask_chat_id(chat_id: str) -> str:
    if len(chat_id) <= 4:
        return "***"
    return f"***{chat_id[-4:]}"


def _format_duration(seconds: float) -> str:
    minutes = max(0, round(seconds / 60))
    hours, remaining = divmod(minutes, 60)
    if hours and remaining:
        return f"{hours} ч {remaining} мин"
    if hours:
        return f"{hours} ч"
    return f"{remaining} мин"
