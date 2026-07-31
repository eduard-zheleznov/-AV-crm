from __future__ import annotations

import pytest

from avito_crm.config import Settings


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    values = {
        "APP_DATA_DIR": str(tmp_path / "data"),
        "APP_OUTPUT_DIR": str(tmp_path / "output"),
        "APP_LOGS_DIR": str(tmp_path / "logs"),
        "AVITO_PROFILE_DIR": str(tmp_path / "profile"),
        "AVITO_SCREENSHOT_DIR": str(tmp_path / "diagnostics"),
        "LPTRACKER_LOGIN": "test@example.com",
        "LPTRACKER_PASSWORD": "not-a-real-secret",
        "LPTRACKER_PROJECT_ID": "1",
        "LPTRACKER_PROJECT_NAME": "",
        "LPTRACKER_FIELD_NAME": "Тег+ для новых с Ав и Ян",
        "LPTRACKER_FIELD_VALUE": "Сбор № лпр (Ав, ремонт кв. под ключ)",
        "CRM_DUPLICATE_POLICY": "skip",
        "PIPELINE_MAX_ATTEMPTS": "2",
        "AVITO_MIN_DELAY_SECONDS": "0",
        "AVITO_MAX_DELAY_SECONDS": "0",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    result = Settings.load(tmp_path)
    result.ensure_runtime_dirs()
    return result
