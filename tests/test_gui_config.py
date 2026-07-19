import json

import pytest

from avito_crm.gui_config import (
    browser_profile_dir,
    browser_profile_is_initialized,
    extract_spreadsheet_id,
    google_sheet_url,
    parse_captcha_wait_hours,
    parse_limit,
    parse_max_recipient_ids,
    parse_notification_emails,
    parse_smtp_port,
    parse_telegram_chat_ids,
    parse_telegram_reminders,
    read_env_values,
    service_account_email,
    update_env_values,
)

SPREADSHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


def test_extract_spreadsheet_id_accepts_url_and_raw_id():
    url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit#gid=123"
    account_url = f"https://docs.google.com/spreadsheets/u/0/d/{SPREADSHEET_ID}/edit"

    assert extract_spreadsheet_id(url) == SPREADSHEET_ID
    assert extract_spreadsheet_id(account_url) == SPREADSHEET_ID
    assert extract_spreadsheet_id(SPREADSHEET_ID) == SPREADSHEET_ID
    assert google_sheet_url(SPREADSHEET_ID).endswith(f"/{SPREADSHEET_ID}/edit")


def test_extract_spreadsheet_id_rejects_unrelated_text():
    with pytest.raises(ValueError):
        extract_spreadsheet_id("not a google sheet")


def test_update_env_values_preserves_secrets_and_uses_bom(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LPTRACKER_PASSWORD=keep-this-secret\nGOOGLE_SPREADSHEET_ID=old\n",
        encoding="utf-8-sig",
    )

    update_env_values(
        env_file,
        {
            "GOOGLE_SPREADSHEET_ID": SPREADSHEET_ID,
            "GUI_DEFAULT_LIMIT": "25",
            "GOOGLE_WORKSHEET": "Лиды #1",
            "GOOGLE_CREDENTIALS_FILE": r"C:\Program Data\service #1.json",
        },
    )

    assert env_file.read_bytes().startswith(b"\xef\xbb\xbf")
    values = read_env_values(env_file)
    assert values["LPTRACKER_PASSWORD"] == "keep-this-secret"
    assert values["GOOGLE_SPREADSHEET_ID"] == SPREADSHEET_ID
    assert values["GUI_DEFAULT_LIMIT"] == "25"
    assert values["GOOGLE_WORKSHEET"] == "Лиды #1"
    assert values["GOOGLE_CREDENTIALS_FILE"] == r"C:\Program Data\service #1.json"


def test_service_account_email_validates_json_without_returning_key(tmp_path):
    path = tmp_path / "google.json"
    path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "client_email": "worker@example.iam.gserviceaccount.com",
                "private_key": "test-only-private-key",
            }
        ),
        encoding="utf-8",
    )

    assert service_account_email(path) == "worker@example.iam.gserviceaccount.com"


@pytest.mark.parametrize(("raw", "expected"), [("1", 1), ("25", 25), ("10000", 10000)])
def test_parse_limit(raw, expected):
    assert parse_limit(raw) == expected


@pytest.mark.parametrize("raw", ["", "0", "10001", "1.5"])
def test_parse_limit_rejects_invalid_values(raw):
    with pytest.raises(ValueError):
        parse_limit(raw)


def test_parse_telegram_chat_ids_accepts_multiple_recipients():
    assert parse_telegram_chat_ids("12345, -100123; @duty_team 12345") == (
        "12345",
        "-100123",
        "@duty_team",
    )


@pytest.mark.parametrize("raw", ["abc", "@x", "123,wrong"])
def test_parse_telegram_chat_ids_rejects_invalid_values(raw):
    with pytest.raises(ValueError):
        parse_telegram_chat_ids(raw)


def test_parse_telegram_reminders_requires_increasing_values():
    assert parse_telegram_reminders("30, 60") == (30.0, 60.0)
    with pytest.raises(ValueError):
        parse_telegram_reminders("60,30")


@pytest.mark.parametrize(("raw", "expected"), [("1", 1.0), ("12", 12.0), ("24,5", 24.5)])
def test_parse_captcha_wait_hours(raw, expected):
    assert parse_captcha_wait_hours(raw) == expected


def test_parse_notification_emails_accepts_unique_addresses():
    assert parse_notification_emails("MAIN@example.com; backup@example.com main@example.com") == (
        "main@example.com",
        "backup@example.com",
    )


@pytest.mark.parametrize("raw", ["wrong", "a@", "a@example.com,broken"])
def test_parse_notification_emails_rejects_invalid_values(raw):
    with pytest.raises(ValueError):
        parse_notification_emails(raw)


@pytest.mark.parametrize(("raw", "expected"), [("465", 465), ("587", 587)])
def test_parse_smtp_port(raw, expected):
    assert parse_smtp_port(raw) == expected


@pytest.mark.parametrize("raw", ["", "0", "65536", "4.65"])
def test_parse_smtp_port_rejects_invalid_values(raw):
    with pytest.raises(ValueError):
        parse_smtp_port(raw)


def test_parse_max_recipient_ids_normalizes_people_and_chats():
    assert parse_max_recipient_ids("123; user:123, chat:456") == (
        "user:123",
        "chat:456",
    )


def test_browser_profile_dir_uses_data_dir_and_supports_guest_profile(tmp_path):
    project_root = tmp_path / "project"
    path = browser_profile_dir(project_root, {"APP_DATA_DIR": "runtime"})

    assert path == (project_root / "runtime" / "browser-profile").resolve()
    assert browser_profile_is_initialized(path) is False


def test_browser_profile_status_only_reports_saved_browser_metadata(tmp_path):
    profile = tmp_path / "profile"
    preferences = profile / "Default" / "Preferences"
    preferences.parent.mkdir(parents=True)
    preferences.write_text("{}", encoding="utf-8")

    assert browser_profile_is_initialized(profile) is True


@pytest.mark.parametrize("raw", ["user:", "group:123", "chat:abc", "@name"])
def test_parse_max_recipient_ids_rejects_invalid_values(raw):
    with pytest.raises(ValueError):
        parse_max_recipient_ids(raw)
