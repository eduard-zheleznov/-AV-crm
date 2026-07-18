from dataclasses import replace

import httpx
import pytest

from avito_crm.errors import NotificationError
from avito_crm.notifications import TelegramNotifier, split_telegram_text


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
