from __future__ import annotations

import json
import logging
import smtplib
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from importlib.resources import files

import httpx

from avito_crm.config import Settings
from avito_crm.errors import ConfigurationError, NotificationError
from avito_crm.models import RunSummary

LOGGER = logging.getLogger(__name__)
TELEGRAM_API_ROOT = "https://api.telegram.org"
TELEGRAM_MESSAGE_LIMIT = 4096
MAX_MESSAGE_LIMIT = 4000
MAX_ROOT_CA_RESOURCE = "certs/russian_trusted_root_ca.pem"


@dataclass(frozen=True, slots=True)
class TelegramChat:
    chat_id: str
    label: str


@dataclass(frozen=True, slots=True)
class MaxRecipient:
    target: str
    label: str


def _max_ssl_context() -> ssl.SSLContext:
    """Build a MAX-only trust context without weakening TLS for other services."""
    context = ssl.create_default_context()
    root_ca = (
        files("avito_crm").joinpath(*MAX_ROOT_CA_RESOURCE.split("/")).read_text(encoding="ascii")
    )
    context.load_verify_locations(cadata=root_ca)
    return context


def _max_http_error_description(exc: httpx.HTTPError) -> str:
    """Return an actionable, token-safe reason for a MAX transport failure."""
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, ssl.SSLCertVerificationError):
            return (
                "не удалось проверить TLS-сертификат MAX; "
                "обновите программу до версии 1.4.1 или новее"
            )
        current = current.__cause__ or current.__context__

    if isinstance(exc, httpx.ConnectTimeout):
        return "истёк тайм-аут подключения к platform-api2.max.ru"
    if isinstance(exc, httpx.ReadTimeout):
        return "MAX не ответил за отведённое время"
    if isinstance(exc, httpx.ConnectError):
        return (
            "не удалось подключиться к platform-api2.max.ru:443; "
            "проверьте доступ сервера к этому адресу"
        )
    return exc.__class__.__name__


def _telegram_http_error_description(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.ConnectTimeout):
        return "нет доступа к api.telegram.org:443 (тайм-аут подключения)"
    if isinstance(exc, httpx.ReadTimeout):
        return "Telegram API не ответил за отведённое время"
    if isinstance(exc, httpx.ConnectError):
        return "не удалось подключиться к api.telegram.org:443"
    return exc.__class__.__name__


class TelegramNotifier:
    """Small Telegram Bot API client that never exposes the bot token in errors."""

    channel_name = "Telegram"

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.token = settings.telegram_bot_token
        self.primary_chat_ids = settings.telegram_primary_chat_ids
        self.backup_chat_ids = settings.telegram_backup_chat_ids
        self.completion_primary = settings.telegram_completion_primary
        self.completion_backup = settings.telegram_completion_backup
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
            detail = (
                f"сообщение доставлено в {sent} из {len(recipients)} чатов; "
                + "; ".join(failures)
            )
            if sent == 0:
                raise NotificationError(detail)
            LOGGER.warning("Частичная доставка Telegram: %s", detail)
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

    def send_run_completed(
        self, *, summary: RunSummary, source_name: str, mode: str, live: bool
    ) -> int:
        _subject, body = _run_completion_message(
            summary, self.computer_name, source_name, mode, live
        )
        recipients = _completion_recipients(
            self.primary_chat_ids,
            self.backup_chat_ids,
            primary=self.completion_primary,
            backup=self.completion_backup,
        )
        return self.send(body, recipients) if recipients else 0

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
            except httpx.HTTPError as exc:
                last_error = _telegram_http_error_description(exc)
            except ValueError:
                last_error = "Telegram вернул ответ не в формате JSON"

            if attempt < self.send_attempts:
                time.sleep(min(2**attempt, 5))

        raise NotificationError(f"Telegram API недоступен ({last_error})")


class MaxNotifier:
    """Official MAX Bot API client with token-safe errors and recipient routing."""

    channel_name = "MAX"

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.token = settings.max_bot_token
        self.primary_recipients = settings.max_primary_recipients
        self.backup_recipients = settings.max_backup_recipients
        self.completion_primary = settings.max_completion_primary
        self.completion_backup = settings.max_completion_backup
        self.computer_name = settings.notification_computer_name or socket.gethostname()
        self.send_attempts = settings.max_send_attempts
        self._owns_client = client is None
        self.client = client or httpx.Client(
            base_url=settings.max_api_base_url,
            timeout=httpx.Timeout(settings.max_request_timeout, connect=10.0),
            follow_redirects=False,
            verify=_max_ssl_context(),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.primary_recipients)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> MaxNotifier:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def send(self, text: str, recipients: tuple[str, ...]) -> int:
        if not self.token:
            raise ConfigurationError("Не задан MAX_BOT_TOKEN")
        targets = _deduplicate(recipients)
        if not targets:
            raise ConfigurationError("Не указаны получатели MAX")

        sent = 0
        failures: list[str] = []
        for target in targets:
            kind, raw_id = target.split(":", 1)
            params = {f"{kind}_id": int(raw_id), "disable_link_preview": True}
            try:
                for chunk in split_max_text(text):
                    self._request(
                        "POST",
                        "/messages",
                        params=params,
                        json_body={"text": chunk, "notify": True},
                    )
                sent += 1
            except NotificationError as exc:
                failures.append(f"{_mask_max_recipient(target)}: {exc}")

        if failures:
            detail = (
                f"MAX-сообщение доставлено {sent} из {len(targets)} получателей; "
                + "; ".join(failures)
            )
            if sent == 0:
                raise NotificationError(detail)
            LOGGER.warning("Частичная доставка MAX: %s", detail)
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
            self.primary_recipients,
        )

    def send_captcha_reminder(
        self,
        *,
        reason: str,
        url: str,
        elapsed_seconds: float,
        escalate: bool,
    ) -> int:
        recipients = self.primary_recipients
        if escalate and self.backup_recipients:
            recipients = _deduplicate((*self.primary_recipients, *self.backup_recipients))
        return self.send(
            "\n".join(
                (
                    "⚠️ Капча Avito всё ещё ожидает решения.",
                    f"Компьютер: {self.computer_name}",
                    f"Прошло: {_format_duration(elapsed_seconds)}",
                    f"Причина: {reason}",
                    f"Ссылка: {url}",
                )
            ),
            recipients,
        )

    def send_captcha_resolved(
        self, *, url: str, elapsed_seconds: float, include_backup: bool
    ) -> int:
        recipients = self.primary_recipients
        if include_backup:
            recipients = _deduplicate((*self.primary_recipients, *self.backup_recipients))
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
        recipients = _deduplicate((*self.primary_recipients, *self.backup_recipients))
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
        recipients = self.primary_recipients
        if include_backup:
            recipients = _deduplicate((*self.primary_recipients, *self.backup_recipients))
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

    def send_run_completed(
        self, *, summary: RunSummary, source_name: str, mode: str, live: bool
    ) -> int:
        _subject, body = _run_completion_message(
            summary, self.computer_name, source_name, mode, live
        )
        recipients = _completion_recipients(
            self.primary_recipients,
            self.backup_recipients,
            primary=self.completion_primary,
            backup=self.completion_backup,
        )
        return self.send(body, recipients) if recipients else 0

    def send_test(self) -> int:
        recipients = _deduplicate((*self.primary_recipients, *self.backup_recipients))
        return self.send(
            "\n".join(
                (
                    "✅ MAX-уведомления Avito → CRM работают.",
                    f"Компьютер: {self.computer_name}",
                    f"Проверено: {datetime.now().astimezone():%d.%m.%Y %H:%M:%S %Z}",
                )
            ),
            recipients,
        )

    def recent_recipients(self) -> list[MaxRecipient]:
        if not self.token:
            raise ConfigurationError("Сначала укажите MAX_BOT_TOKEN")
        bot = self._request("GET", "/me")
        bot_id = str(bot.get("user_id", ""))
        payload = self._request(
            "GET",
            "/updates",
            params=[
                ("limit", 100),
                ("timeout", 0),
                ("types", "bot_started,message_created,bot_added"),
            ],
        )
        updates = payload.get("updates")
        if not isinstance(updates, list) or not updates:
            subscriptions_payload = self._request("GET", "/subscriptions")
            subscriptions = subscriptions_payload.get("subscriptions")
            if isinstance(subscriptions, list) and subscriptions:
                raise ConfigurationError(
                    "У MAX-бота включён Webhook, поэтому MAX не отдаёт события "
                    "через кнопку «Найти ID». Возьмите user_id из текущего "
                    "Webhook-сервиса или используйте отдельного MAX-бота."
                )
            return []
        recipients: dict[str, MaxRecipient] = {}
        for update in updates:
            if not isinstance(update, dict):
                continue
            self._add_max_user(recipients, update.get("user"), bot_id)
            message = update.get("message")
            if isinstance(message, dict):
                self._add_max_user(recipients, message.get("sender"), bot_id)
            chat_id = update.get("chat_id")
            if update.get("update_type") == "bot_added" and isinstance(chat_id, int):
                target = f"chat:{chat_id}"
                recipients[target] = MaxRecipient(target, f"групповой чат {chat_id}")
        return list(recipients.values())

    @staticmethod
    def _add_max_user(recipients: dict[str, MaxRecipient], value: object, bot_id: str) -> None:
        if not isinstance(value, dict) or "user_id" not in value:
            return
        user_id = str(value["user_id"])
        if not user_id or user_id == bot_id or value.get("is_bot") is True:
            return
        full_name = " ".join(
            part
            for part in (
                str(value.get("first_name", "")).strip(),
                str(value.get("last_name", "")).strip(),
            )
            if part
        )
        name = full_name or str(value.get("name", "")).strip()
        username = str(value.get("username", "")).strip()
        label = name or (f"@{username}" if username else f"пользователь {user_id}")
        target = f"user:{user_id}"
        recipients[target] = MaxRecipient(target, label)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: object | None = None,
        json_body: dict[str, object] | None = None,
    ) -> dict[str, object]:
        last_error = "неизвестная ошибка"
        for attempt in range(1, self.send_attempts + 1):
            try:
                response = self.client.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                    headers={"Authorization": self.token},
                )
                payload = response.json()
                if 200 <= response.status_code < 300 and isinstance(payload, dict):
                    return payload
                description = _max_error_description(payload)
                if self.token:
                    description = description.replace(self.token, "***REDACTED***")
                last_error = f"HTTP {response.status_code}: {description or 'ошибка API'}"
            except httpx.HTTPError as exc:
                last_error = _max_http_error_description(exc)
            except ValueError:
                last_error = "MAX вернул ответ не в формате JSON"
            if attempt < self.send_attempts:
                time.sleep(min(2**attempt, 5))
        raise NotificationError(f"MAX API недоступен ({last_error})")


class EmailNotifier:
    """SMTP notification backend using an app password stored only in local .env."""

    channel_name = "Email"

    def __init__(self, settings: Settings) -> None:
        self.host = settings.smtp_host
        self.port = settings.smtp_port
        self.security = settings.smtp_security
        self.username = settings.smtp_username
        self.password = settings.smtp_password
        self.from_address = settings.smtp_from_address or settings.smtp_username
        self.primary_recipients = settings.email_primary_recipients
        self.backup_recipients = settings.email_backup_recipients
        self.completion_primary = settings.email_completion_primary
        self.completion_backup = settings.email_completion_backup
        self.computer_name = settings.notification_computer_name or socket.gethostname()
        self.timeout = settings.email_request_timeout
        self.send_attempts = settings.email_send_attempts

    @property
    def enabled(self) -> bool:
        return bool(
            self.host
            and self.username
            and self.password
            and self.from_address
            and self.primary_recipients
        )

    def close(self) -> None:
        return None

    def __enter__(self) -> EmailNotifier:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def send(self, subject: str, text: str, recipients: tuple[str, ...]) -> int:
        if not self.enabled:
            raise ConfigurationError(
                "Для email нужны SMTP-сервер, логин, пароль приложения и получатель"
            )
        pending = list(_deduplicate(recipients))
        if not pending:
            raise ConfigurationError("Не указаны получатели email")

        sent = 0
        last_error = "неизвестная ошибка"
        for attempt in range(1, self.send_attempts + 1):
            try:
                with self._connect() as smtp:
                    smtp.login(self.username, self.password)
                    for recipient in tuple(pending):
                        message = EmailMessage()
                        message["Subject"] = subject
                        message["From"] = formataddr(("Avito CRM", self.from_address))
                        message["To"] = recipient
                        message.set_content(text)
                        refused = smtp.send_message(message)
                        if refused:
                            last_error = "SMTP отклонил получателя"
                            continue
                        pending.remove(recipient)
                        sent += 1
            except (OSError, TimeoutError, smtplib.SMTPException) as exc:
                last_error = exc.__class__.__name__

            if not pending:
                return sent
            if attempt < self.send_attempts:
                time.sleep(min(2**attempt, 5))

        masked = ", ".join(_mask_email(value) for value in pending)
        detail = (
            f"email доставлен {sent} из {sent + len(pending)} получателей; "
            f"не доставлено: {masked} ({last_error})"
        )
        if sent == 0:
            raise NotificationError(detail)
        LOGGER.warning("Частичная доставка Email: %s", detail)
        return sent

    def send_captcha_detected(self, *, reason: str, url: str, wait_seconds: float) -> int:
        return self.send(
            "[Avito CRM] Требуется решить капчу",
            "\n".join(
                (
                    "Avito остановил очередь для ручной проверки.",
                    f"Компьютер: {self.computer_name}",
                    f"Причина: {reason}",
                    f"Ссылка: {url}",
                    f"Ожидание: до {_format_duration(wait_seconds)}.",
                    "Откройте удалённый компьютер и завершите проверку в браузере. "
                    "После этого программа продолжит работу автоматически.",
                )
            ),
            self.primary_recipients,
        )

    def send_captcha_reminder(
        self,
        *,
        reason: str,
        url: str,
        elapsed_seconds: float,
        escalate: bool,
    ) -> int:
        recipients = self.primary_recipients
        if escalate and self.backup_recipients:
            recipients = _deduplicate((*self.primary_recipients, *self.backup_recipients))
        return self.send(
            "[Avito CRM] Капча всё ещё ожидает решения",
            "\n".join(
                (
                    "Капча Avito всё ещё ожидает решения.",
                    f"Компьютер: {self.computer_name}",
                    f"Прошло: {_format_duration(elapsed_seconds)}",
                    f"Причина: {reason}",
                    f"Ссылка: {url}",
                )
            ),
            recipients,
        )

    def send_captcha_resolved(
        self, *, url: str, elapsed_seconds: float, include_backup: bool
    ) -> int:
        recipients = self.primary_recipients
        if include_backup:
            recipients = _deduplicate((*self.primary_recipients, *self.backup_recipients))
        return self.send(
            "[Avito CRM] Капча решена, работа продолжена",
            "\n".join(
                (
                    "Проверка Avito завершена, очередь продолжает работу.",
                    f"Компьютер: {self.computer_name}",
                    f"Пауза заняла: {_format_duration(elapsed_seconds)}",
                    f"Ссылка: {url}",
                )
            ),
            recipients,
        )

    def send_captcha_timeout(self, *, reason: str, url: str, wait_seconds: float) -> int:
        recipients = _deduplicate((*self.primary_recipients, *self.backup_recipients))
        return self.send(
            "[Avito CRM] Ожидание капчи завершилось",
            "\n".join(
                (
                    "Ожидание ручной проверки Avito завершилось по таймауту.",
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
        recipients = self.primary_recipients
        if include_backup:
            recipients = _deduplicate((*self.primary_recipients, *self.backup_recipients))
        return self.send(
            "[Avito CRM] Ожидание остановлено оператором",
            "\n".join(
                (
                    "Ожидание проверки Avito остановлено оператором.",
                    f"Компьютер: {self.computer_name}",
                    f"Ссылка: {url}",
                    "Текущая строка сохранена для повторного запуска.",
                )
            ),
            recipients,
        )

    def send_run_completed(
        self, *, summary: RunSummary, source_name: str, mode: str, live: bool
    ) -> int:
        subject, body = _run_completion_message(
            summary, self.computer_name, source_name, mode, live
        )
        recipients = _completion_recipients(
            self.primary_recipients,
            self.backup_recipients,
            primary=self.completion_primary,
            backup=self.completion_backup,
        )
        return self.send(subject, body, recipients) if recipients else 0

    def send_test(self) -> int:
        recipients = _deduplicate((*self.primary_recipients, *self.backup_recipients))
        return self.send(
            "[Avito CRM] Проверка email-уведомлений",
            "\n".join(
                (
                    "Email-уведомления Avito → CRM работают.",
                    f"Компьютер: {self.computer_name}",
                    f"Проверено: {datetime.now().astimezone():%d.%m.%Y %H:%M:%S %Z}",
                )
            ),
            recipients,
        )

    def _connect(self):
        context = ssl.create_default_context()
        if self.security == "ssl":
            return smtplib.SMTP_SSL(
                self.host,
                self.port,
                timeout=self.timeout,
                context=context,
            )
        smtp = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        try:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        except Exception:
            smtp.close()
            raise
        return smtp


class NotificationRouter:
    """Send through every configured channel without losing later reminders."""

    def __init__(self, settings: Settings, backends: list[object] | None = None) -> None:
        # Email goes first so a blocked Telegram endpoint cannot delay the useful alert.
        self.backends = (
            backends
            if backends is not None
            else [MaxNotifier(settings), EmailNotifier(settings), TelegramNotifier(settings)]
        )
        self._unavailable: set[int] = set()

    @property
    def enabled(self) -> bool:
        return any(
            bool(getattr(backend, "enabled", False)) and id(backend) not in self._unavailable
            for backend in self.backends
        )

    def close(self) -> None:
        for backend in self.backends:
            close = getattr(backend, "close", None)
            if close:
                close()

    def _dispatch(self, method: str, **kwargs: object) -> int:
        delivered = 0
        failures: list[str] = []
        for backend in self.backends:
            if not getattr(backend, "enabled", False) or id(backend) in self._unavailable:
                continue
            channel = str(getattr(backend, "channel_name", backend.__class__.__name__))
            try:
                channel_delivered = int(getattr(backend, method)(**kwargs))
                delivered += channel_delivered
                LOGGER.info(
                    "Уведомление %s/%s: доставлено получателям: %s",
                    channel,
                    method,
                    channel_delivered,
                )
            except ConfigurationError as exc:
                self._unavailable.add(id(backend))
                failures.append(f"{channel}: {exc}")
                LOGGER.warning("Канал %s отключён из-за настройки: %s", channel, exc)
            except NotificationError as exc:
                # Delivery/API failures can be transient.  Keep the channel
                # eligible for the next reminder so a temporary outage does not
                # silence the entire CAPTCHA escalation chain.
                failures.append(f"{channel}: {exc}")
                LOGGER.warning(
                    "Канал %s не доставил %s; повторим на следующем напоминании: %s",
                    channel,
                    method,
                    exc,
                )
            except Exception as exc:
                self._unavailable.add(id(backend))
                failures.append(f"{channel}: {exc.__class__.__name__}")
                LOGGER.warning(
                    "Канал %s отключён до конца запуска (%s)",
                    channel,
                    exc.__class__.__name__,
                )
        if delivered == 0 and failures:
            raise NotificationError("; ".join(failures))
        return delivered

    def send_captcha_detected(self, **kwargs: object) -> int:
        return self._dispatch("send_captcha_detected", **kwargs)

    def send_captcha_reminder(self, **kwargs: object) -> int:
        return self._dispatch("send_captcha_reminder", **kwargs)

    def send_captcha_resolved(self, **kwargs: object) -> int:
        return self._dispatch("send_captcha_resolved", **kwargs)

    def send_captcha_timeout(self, **kwargs: object) -> int:
        return self._dispatch("send_captcha_timeout", **kwargs)

    def send_captcha_stopped(self, **kwargs: object) -> int:
        return self._dispatch("send_captcha_stopped", **kwargs)

    def send_run_completed(self, **kwargs: object) -> int:
        return self._dispatch("send_run_completed", **kwargs)


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


def split_max_text(text: str) -> list[str]:
    return split_telegram_text(text, limit=MAX_MESSAGE_LIMIT)


def _run_completion_message(
    summary: RunSummary,
    computer_name: str,
    source_name: str,
    mode: str,
    live: bool,
) -> tuple[str, str]:
    reason = (summary.stopped_reason or "Работа завершена").strip()
    reason_lower = reason.casefold()
    if summary.errors or reason_lower.startswith("ошибка запуска"):
        headline = "⚠️ Запуск завершён с техническими ошибками"
        subject = "[Avito CRM] Завершено с техническими ошибками"
    elif summary.manual_required:
        headline = "⏸ Запуск остановлен: требуется ручное действие"
        subject = "[Avito CRM] Требуется ручное действие"
    elif "останов" in reason_lower:
        headline = "⏹ Запуск остановлен оператором"
        subject = "[Avito CRM] Запуск остановлен"
    else:
        headline = "✅ Запуск Avito → CRM завершён"
        subject = "[Avito CRM] Запуск завершён"

    source_kind = source_name.split(":", 1)[0] or "очередь"
    lines = (
        headline,
        f"Компьютер: {computer_name}",
        f"Run ID: {summary.run_id}",
        f"Источник: {source_kind}; режим: {mode}; CRM: {'да' if live else 'нет'}",
        "",
        f"Обработано ссылок: {summary.processed}",
        f"Всего попыток: {summary.inspected}; кругов: {summary.rounds}",
        f"Номеров открыто: {summary.captured}",
        f"Лидов создано: {summary.created}",
        f"Повторных лидов: {summary.repeat_created}",
        f"Шагов воронки обновлено: {summary.stage_synced}",
        f'Строк со статусом «Недозвон»: {summary.no_answer_synced}',
        f"Повторное открытие исчерпано: {summary.repeat_exhausted}",
        f"Дубликатов: {summary.duplicates}",
        f"Неактивных объявлений: {summary.inactive}",
        f"Без кнопки телефона: {summary.unavailable}",
        f"Номер не открыт после всех попыток: {summary.phone_failed}",
        f"Повторных попыток: {summary.retries}",
        f"Некорректных ссылок: {summary.invalid}",
        f"Технических ошибок: {summary.errors}",
        f"Требуют ручного действия: {summary.manual_required}",
        f"Итог: {reason}",
    )
    return subject, "\n".join(lines)


def _deduplicate(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _completion_recipients(
    primary_values: tuple[str, ...],
    backup_values: tuple[str, ...],
    *,
    primary: bool,
    backup: bool,
) -> tuple[str, ...]:
    selected: tuple[str, ...] = ()
    if primary:
        selected += primary_values
    if backup:
        selected += backup_values
    return _deduplicate(selected)


def _mask_chat_id(chat_id: str) -> str:
    if len(chat_id) <= 4:
        return "***"
    return f"***{chat_id[-4:]}"


def _mask_email(address: str) -> str:
    local, separator, domain = address.partition("@")
    if not separator:
        return "***"
    visible = local[:1] if local else ""
    return f"{visible}***@{domain}"


def _mask_max_recipient(target: str) -> str:
    kind, _, raw_id = target.partition(":")
    return f"{kind}:***{raw_id[-4:]}"


def _max_error_description(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("message", "error", "description", "code"):
        value = payload.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()[:240]
        if isinstance(value, dict):
            nested = _max_error_description(value)
            if nested:
                return nested
    return ""


def _format_duration(seconds: float) -> str:
    minutes = max(0, round(seconds / 60))
    hours, remaining = divmod(minutes, 60)
    if hours and remaining:
        return f"{hours} ч {remaining} мин"
    if hours:
        return f"{hours} ч"
    return f"{remaining} мин"
