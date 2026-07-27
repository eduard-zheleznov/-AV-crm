from __future__ import annotations

from avito_crm.config import Settings


def test_refresh_env_reloads_gui_saved_notification_recipients(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_PRIMARY_CHAT_IDS", "111111")
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_PRIMARY_CHAT_IDS=222222\n", encoding="utf-8")

    stale = Settings.load(tmp_path)
    refreshed = Settings.load(tmp_path, refresh_env=True)

    assert stale.telegram_primary_chat_ids == ("111111",)
    assert refreshed.telegram_primary_chat_ids == ("222222",)


def test_invalid_crm_timezone_does_not_block_unrelated_settings_load(tmp_path, monkeypatch):
    monkeypatch.setenv("LPTRACKER_TIMEZONE", "Missing/Timezone")

    settings = Settings.load(tmp_path)

    assert settings.lptracker_timezone == "Missing/Timezone"
