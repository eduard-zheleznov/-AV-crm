import hashlib
import smtplib
import ssl
from dataclasses import replace

import httpx
import pytest

from avito_crm.errors import NotificationError
from avito_crm.notifications import (
    EmailNotifier,
    MaxNotifier,
    NotificationRouter,
    TelegramNotifier,
    _max_http_error_description,
    _max_ssl_context,
    split_max_text,
    split_telegram_text,
)


def _notification_settings(settings, **overrides):
    values = {
        "telegram_bot_token": "123456:test-only-token",
        "telegram_primary_chat_ids": ("10001",),
        "telegram_backup_chat_ids": ("10002",),
        "telegram_send_attempts": 1,
    }
    values.update(overrides)
    return replace(settings, **values)


def test_telegram_test_is_sent_to_unique_primary_and_backup_chats(settings):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    configured = _notification_settings(
        settings,
        telegram_primary_chat_ids=("10001", "10002"),
        telegram_backup_chat_ids=("10002", "10003"),
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    notifier = TelegramNotifier(configured, client=client)

    assert notifier.send_test() == 3
    assert len(requests) == 3
    assert all(request.url.path.endswith("/sendMessage") for request in requests)


def test_telegram_error_never_exposes_bot_token(settings):
    token = "123456:must-never-appear"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    configured = _notification_settings(settings, telegram_bot_token=token)
    notifier = TelegramNotifier(
        configured,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(NotificationError) as captured:
        notifier.send_test()

    assert token not in str(captured.value)
    assert "Unauthorized" in str(captured.value)


def test_recent_chats_are_deduplicated(settings):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {
                            "chat": {
                                "id": 10001,
                                "first_name": "Иван",
                                "username": "ivan",
                            }
                        },
                    },
                    {
                        "update_id": 2,
                        "message": {"chat": {"id": 10001, "first_name": "Иван"}},
                    },
                    {
                        "update_id": 3,
                        "my_chat_member": {"chat": {"id": -10002, "title": "Дежурные"}},
                    },
                ],
            },
        )

    notifier = TelegramNotifier(
        _notification_settings(settings),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    chats = notifier.recent_chats()

    assert [(chat.chat_id, chat.label) for chat in chats] == [
        ("10001", "Иван"),
        ("-10002", "Дежурные"),
    ]


def test_split_telegram_text_respects_api_limit():
    chunks = split_telegram_text("line\n" * 2000, limit=120)

    assert len(chunks) > 1
    assert all(0 < len(chunk) <= 120 for chunk in chunks)


def _max_settings(settings, **overrides):
    values = {
        "max_api_base_url": "https://platform-api2.max.ru",
        "max_bot_token": "max-test-token",
        "max_primary_recipients": ("user:10001",),
        "max_backup_recipients": ("chat:20002",),
        "max_send_attempts": 1,
    }
    values.update(overrides)
    return replace(settings, **values)


def test_max_test_is_sent_to_unique_users_and_chats(settings):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"message": {"body": {"text": "ok"}}})

    configured = _max_settings(
        settings,
        max_primary_recipients=("user:10001", "chat:20002"),
        max_backup_recipients=("chat:20002",),
    )
    client = httpx.Client(
        base_url=configured.max_api_base_url,
        transport=httpx.MockTransport(handler),
    )
    notifier = MaxNotifier(configured, client=client)

    assert notifier.send_test() == 2
    assert len(requests) == 2
    assert requests[0].url.params["user_id"] == "10001"
    assert requests[1].url.params["chat_id"] == "20002"
    assert all(request.headers["Authorization"] == "max-test-token" for request in requests)


def test_max_error_never_exposes_bot_token(settings):
    token = "max-token-must-never-appear"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": f"invalid {token}"})

    configured = _max_settings(settings, max_bot_token=token)
    notifier = MaxNotifier(
        configured,
        client=httpx.Client(
            base_url=configured.max_api_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )

    with pytest.raises(NotificationError) as captured:
        notifier.send_test()

    assert token not in str(captured.value)
    assert "REDACTED" in str(captured.value)


def test_max_ssl_context_contains_official_ministry_root():
    expected = "d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31"

    fingerprints = {
        hashlib.sha256(certificate).hexdigest()
        for certificate in _max_ssl_context().get_ca_certs(binary_form=True)
    }

    assert expected in fingerprints


def test_max_tls_error_has_actionable_message():
    request = httpx.Request("GET", "https://platform-api2.max.ru/me")
    try:
        try:
            raise ssl.SSLCertVerificationError("certificate verify failed")
        except ssl.SSLCertVerificationError as cause:
            raise httpx.ConnectError("TLS failed", request=request) from cause
    except httpx.ConnectError as exc:
        description = _max_http_error_description(exc)

    assert "TLS-сертификат" in description
    assert "1.4.1" in description


def test_max_connection_error_has_host_and_port():
    request = httpx.Request("GET", "https://platform-api2.max.ru/me")
    error = httpx.ConnectError("connection refused", request=request)

    assert "platform-api2.max.ru:443" in _max_http_error_description(error)


def test_max_recent_recipients_reads_started_users_and_group_chats(settings):
    update_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/me":
            return httpx.Response(200, json={"user_id": 999, "is_bot": True})
        update_requests.append(request)
        return httpx.Response(
            200,
            json={
                "updates": [
                    {
                        "update_type": "bot_started",
                        "chat_id": 10001,
                        "user": {"user_id": 10001, "first_name": "Иван"},
                    },
                    {
                        "update_type": "message_created",
                        "message": {
                            "sender": {
                                "user_id": 10002,
                                "first_name": "Анна",
                                "username": "anna",
                            }
                        },
                    },
                    {"update_type": "bot_added", "chat_id": 20002},
                ],
                "marker": 10,
            },
        )

    configured = _max_settings(settings)
    notifier = MaxNotifier(
        configured,
        client=httpx.Client(
            base_url=configured.max_api_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )

    recipients = notifier.recent_recipients()

    assert update_requests[0].url.params["marker"] == "0"
    assert [(item.target, item.label) for item in recipients] == [
        ("user:10001", "Иван"),
        ("user:10002", "Анна"),
        ("chat:20002", "групповой чат 20002"),
    ]


def test_split_max_text_respects_api_limit():
    chunks = split_max_text("строка\n" * 1000)

    assert len(chunks) > 1
    assert all(0 < len(chunk) <= 4000 for chunk in chunks)


class FakeSmtp:
    instances = []

    def __init__(self, host, port, **kwargs):
        self.host = host
        self.port = port
        self.kwargs = kwargs
        self.login_values = None
        self.messages = []
        self.starttls_called = False
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def ehlo(self):
        return None

    def starttls(self, **_kwargs):
        self.starttls_called = True

    def login(self, username, password):
        self.login_values = (username, password)

    def send_message(self, message):
        self.messages.append(message)
        return {}

    def close(self):
        return None


def _email_settings(settings, **overrides):
    values = {
        "smtp_host": "smtp.yandex.ru",
        "smtp_port": 465,
        "smtp_security": "ssl",
        "smtp_username": "sender@example.com",
        "smtp_password": "app-password-test-only",
        "smtp_from_address": "sender@example.com",
        "email_primary_recipients": ("main@example.com",),
        "email_backup_recipients": ("backup@example.com",),
        "email_send_attempts": 1,
    }
    values.update(overrides)
    return replace(settings, **values)


def test_email_test_is_sent_to_unique_primary_and_backup_recipients(settings, monkeypatch):
    FakeSmtp.instances = []
    monkeypatch.setattr("avito_crm.notifications.smtplib.SMTP_SSL", FakeSmtp)
    notifier = EmailNotifier(
        _email_settings(
            settings,
            email_primary_recipients=("main@example.com", "backup@example.com"),
            email_backup_recipients=("backup@example.com",),
        )
    )

    assert notifier.send_test() == 2
    smtp = FakeSmtp.instances[0]
    assert smtp.host == "smtp.yandex.ru"
    assert smtp.port == 465
    assert smtp.login_values == ("sender@example.com", "app-password-test-only")
    assert [message["To"] for message in smtp.messages] == [
        "main@example.com",
        "backup@example.com",
    ]
    assert all("Проверка email" in message["Subject"] for message in smtp.messages)


def test_email_starttls_is_negotiated(settings, monkeypatch):
    FakeSmtp.instances = []
    monkeypatch.setattr("avito_crm.notifications.smtplib.SMTP", FakeSmtp)
    notifier = EmailNotifier(_email_settings(settings, smtp_port=587, smtp_security="starttls"))

    assert notifier.send_test() == 2
    assert FakeSmtp.instances[0].starttls_called is True


def test_email_error_never_exposes_password(settings, monkeypatch):
    password = "must-never-appear"

    class RejectingSmtp(FakeSmtp):
        def login(self, _username, _password):
            raise smtplib.SMTPAuthenticationError(535, b"authentication failed")

    monkeypatch.setattr("avito_crm.notifications.smtplib.SMTP_SSL", RejectingSmtp)
    notifier = EmailNotifier(_email_settings(settings, smtp_password=password))

    with pytest.raises(NotificationError) as captured:
        notifier.send_test()

    assert password not in str(captured.value)
    assert "m***@example.com" in str(captured.value)


def test_router_keeps_working_channel_and_disables_failed_one(settings):
    calls = []

    class Backend:
        enabled = True

        def __init__(self, name, *, fails=False):
            self.channel_name = name
            self.fails = fails

        def send_captcha_detected(self, **_kwargs):
            calls.append(self.channel_name)
            if self.fails:
                raise NotificationError("unavailable")
            return 1

        def close(self):
            return None

    email = Backend("Email")
    telegram = Backend("Telegram", fails=True)
    router = NotificationRouter(settings, backends=[email, telegram])

    assert (
        router.send_captcha_detected(reason="captcha", url="https://example.com", wait_seconds=1)
        == 1
    )
    assert (
        router.send_captcha_detected(reason="captcha", url="https://example.com", wait_seconds=1)
        == 1
    )
    assert calls == ["Email", "Telegram", "Email"]


def test_router_default_delivery_order_is_max_email_telegram(settings):
    router = NotificationRouter(settings)
    try:
        assert [backend.channel_name for backend in router.backends] == [
            "MAX",
            "Email",
            "Telegram",
        ]
    finally:
        router.close()
