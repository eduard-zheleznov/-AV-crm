from __future__ import annotations

from dataclasses import replace

import pytest

from avito_crm.config import Settings
from avito_crm.errors import ConfigurationError


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


def test_browser_channel_defaults_to_bundled_chromium(tmp_path, monkeypatch):
    monkeypatch.delenv("AVITO_BROWSER_CHANNEL", raising=False)

    settings = Settings.load(tmp_path)

    assert settings.avito_browser_channel == ""


def test_browser_channel_normalizes_installed_chrome(tmp_path, monkeypatch):
    monkeypatch.setenv("AVITO_BROWSER_CHANNEL", " Chrome ")

    settings = Settings.load(tmp_path)

    assert settings.avito_browser_channel == "chrome"


def test_browser_channel_rejects_unknown_playwright_channel(settings):
    configured = replace(settings, avito_browser_channel="firefox")

    with pytest.raises(ConfigurationError, match="AVITO_BROWSER_CHANNEL"):
        configured.validate()


def test_extension_driver_requires_a_long_local_token(settings):
    configured = replace(
        settings,
        avito_browser_driver="chrome_extension",
        avito_extension_token="too-short",
    )

    with pytest.raises(ConfigurationError, match="AVITO_EXTENSION_TOKEN"):
        configured.validate()


def test_extension_driver_accepts_loopback_configuration(settings):
    configured = replace(
        settings,
        avito_browser_driver="chrome_extension",
        avito_extension_token="a" * 64,
        avito_extension_port=8765,
    )

    configured.validate()


def test_robot_handoff_requires_local_gemini_key_when_enabled(settings):
    configured = replace(settings, robot_handoff_enabled=True, gemini_api_key="")

    with pytest.raises(ConfigurationError, match="GEMINI_API_KEY"):
        configured.validate()


def test_gemini_inline_audio_limit_accounts_for_base64_overhead(settings):
    configured = replace(settings, gemini_max_audio_bytes=15 * 1024 * 1024)

    with pytest.raises(ConfigurationError, match="GEMINI_MAX_AUDIO_BYTES"):
        configured.validate()
