from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from avito_crm.errors import ConfigurationError


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigurationError(f"{name}: ожидалось true/false, получено {value!r}")


def _int(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name}: ожидалось целое число") from exc


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name}: ожидалось число") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    root_dir: Path
    data_dir: Path
    output_dir: Path
    logs_dir: Path
    state_db: Path
    browser_profile_dir: Path
    screenshot_dir: Path

    lptracker_base_url: str
    lptracker_login: str
    lptracker_password: str
    lptracker_project_id: int | None
    lptracker_project_name: str
    lptracker_field_name: str
    lptracker_field_value: str
    lptracker_service_name: str
    duplicate_policy: str

    google_credentials_file: Path | None
    google_spreadsheet_id: str
    google_worksheet: str

    url_column: str
    status_column: str
    phone_column: str
    crm_lead_column: str
    error_column: str
    attempts_column: str
    processed_at_column: str
    run_id_column: str

    avito_headless: bool
    avito_min_delay: float
    avito_max_delay: float
    avito_page_timeout: float
    avito_manual_timeout: float
    avito_max_per_session: int
    avito_phone_first_round_attempts: int
    avito_phone_second_round_attempts: int
    avito_phone_retry_min: float
    avito_phone_retry_max: float
    avito_temp_number_wait_min: float
    avito_temp_number_wait_max: float
    avito_after_label_min: float
    avito_after_label_max: float
    avito_long_break_interval_min: float
    avito_long_break_interval_max: float
    avito_long_break_duration_min: float
    avito_long_break_duration_max: float
    tesseract_cmd: str
    ocr_min_agreement: int
    max_attempts: int
    max_consecutive_failures: int

    @classmethod
    def load(cls, root_dir: Path | None = None) -> Settings:
        root = (root_dir or Path.cwd()).resolve()
        load_dotenv(root / ".env", override=False)
        data = Path(os.getenv("APP_DATA_DIR", root / "data")).expanduser().resolve()
        output = Path(os.getenv("APP_OUTPUT_DIR", root / "output")).expanduser().resolve()
        logs = Path(os.getenv("APP_LOGS_DIR", root / "logs")).expanduser().resolve()
        credentials = os.getenv("GOOGLE_CREDENTIALS_FILE", "").strip()

        settings = cls(
            root_dir=root,
            data_dir=data,
            output_dir=output,
            logs_dir=logs,
            state_db=data / "state.sqlite3",
            browser_profile_dir=Path(os.getenv("AVITO_PROFILE_DIR", data / "browser-profile"))
            .expanduser()
            .resolve(),
            screenshot_dir=Path(os.getenv("AVITO_SCREENSHOT_DIR", output / "diagnostics"))
            .expanduser()
            .resolve(),
            lptracker_base_url=os.getenv(
                "LPTRACKER_BASE_URL", "https://direct.lptracker.ru"
            ).rstrip("/"),
            lptracker_login=os.getenv("LPTRACKER_LOGIN", "").strip(),
            lptracker_password=os.getenv("LPTRACKER_PASSWORD", ""),
            lptracker_project_id=_int("LPTRACKER_PROJECT_ID"),
            lptracker_project_name=os.getenv("LPTRACKER_PROJECT_NAME", "").strip(),
            lptracker_field_name=os.getenv(
                "LPTRACKER_FIELD_NAME", "Тег+ для новых с Ав и Ян"
            ).strip(),
            lptracker_field_value=os.getenv(
                "LPTRACKER_FIELD_VALUE", "Сбор № лпр (Ав, ремонт кв. под ключ)"
            ).strip(),
            lptracker_service_name=os.getenv(
                "LPTRACKER_SERVICE_NAME", "Avito CRM Pipeline"
            ).strip(),
            duplicate_policy=os.getenv("CRM_DUPLICATE_POLICY", "skip").strip().lower(),
            google_credentials_file=Path(credentials).expanduser().resolve()
            if credentials
            else None,
            google_spreadsheet_id=os.getenv("GOOGLE_SPREADSHEET_ID", "").strip(),
            google_worksheet=os.getenv("GOOGLE_WORKSHEET", "Лист1").strip(),
            url_column=os.getenv("QUEUE_URL_COLUMN", "Ссылка").strip(),
            status_column=os.getenv("QUEUE_STATUS_COLUMN", "Статус").strip(),
            phone_column=os.getenv("QUEUE_PHONE_COLUMN", "Телефон").strip(),
            crm_lead_column=os.getenv("QUEUE_CRM_LEAD_COLUMN", "CRM lead ID").strip(),
            error_column=os.getenv("QUEUE_ERROR_COLUMN", "Ошибка").strip(),
            attempts_column=os.getenv("QUEUE_ATTEMPTS_COLUMN", "Попытки").strip(),
            processed_at_column=os.getenv("QUEUE_PROCESSED_AT_COLUMN", "Обработано").strip(),
            run_id_column=os.getenv("QUEUE_RUN_ID_COLUMN", "Run ID").strip(),
            avito_headless=_bool("AVITO_HEADLESS", False),
            avito_min_delay=_float("AVITO_MIN_DELAY_SECONDS", 7.0),
            avito_max_delay=_float("AVITO_MAX_DELAY_SECONDS", 15.0),
            avito_page_timeout=_float("AVITO_PAGE_TIMEOUT_SECONDS", 45.0),
            avito_manual_timeout=_float("AVITO_MANUAL_TIMEOUT_SECONDS", 300.0),
            avito_max_per_session=_int("AVITO_MAX_PER_SESSION", 25) or 25,
            avito_phone_first_round_attempts=int(_int("AVITO_PHONE_FIRST_ROUND_ATTEMPTS", 6) or 0),
            avito_phone_second_round_attempts=int(
                _int("AVITO_PHONE_SECOND_ROUND_ATTEMPTS", 3) or 0
            ),
            avito_phone_retry_min=_float("AVITO_PHONE_RETRY_MIN_SECONDS", 5.0),
            avito_phone_retry_max=_float("AVITO_PHONE_RETRY_MAX_SECONDS", 15.0),
            avito_temp_number_wait_min=_float("AVITO_TEMP_NUMBER_WAIT_MIN_SECONDS", 7.0),
            avito_temp_number_wait_max=_float("AVITO_TEMP_NUMBER_WAIT_MAX_SECONDS", 10.0),
            avito_after_label_min=_float("AVITO_AFTER_LABEL_MIN_SECONDS", 1.7),
            avito_after_label_max=_float("AVITO_AFTER_LABEL_MAX_SECONDS", 3.0),
            avito_long_break_interval_min=_float("AVITO_LONG_BREAK_INTERVAL_MIN_SECONDS", 1500.0),
            avito_long_break_interval_max=_float("AVITO_LONG_BREAK_INTERVAL_MAX_SECONDS", 1800.0),
            avito_long_break_duration_min=_float("AVITO_LONG_BREAK_DURATION_MIN_SECONDS", 180.0),
            avito_long_break_duration_max=_float("AVITO_LONG_BREAK_DURATION_MAX_SECONDS", 420.0),
            tesseract_cmd=os.getenv("TESSERACT_CMD", "").strip(),
            ocr_min_agreement=_int("OCR_MIN_AGREEMENT", 2) or 2,
            max_attempts=_int("PIPELINE_MAX_ATTEMPTS", 3) or 3,
            max_consecutive_failures=_int("PIPELINE_MAX_CONSECUTIVE_FAILURES", 5) or 5,
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.avito_min_delay < 0 or self.avito_max_delay < self.avito_min_delay:
            raise ConfigurationError("Некорректный диапазон задержек Avito")
        if self.avito_max_per_session < 1:
            raise ConfigurationError("AVITO_MAX_PER_SESSION должен быть больше нуля")
        ranges = (
            (
                "AVITO_PHONE_RETRY",
                self.avito_phone_retry_min,
                self.avito_phone_retry_max,
            ),
            (
                "AVITO_TEMP_NUMBER_WAIT",
                self.avito_temp_number_wait_min,
                self.avito_temp_number_wait_max,
            ),
            (
                "AVITO_AFTER_LABEL",
                self.avito_after_label_min,
                self.avito_after_label_max,
            ),
            (
                "AVITO_LONG_BREAK_INTERVAL",
                self.avito_long_break_interval_min,
                self.avito_long_break_interval_max,
            ),
            (
                "AVITO_LONG_BREAK_DURATION",
                self.avito_long_break_duration_min,
                self.avito_long_break_duration_max,
            ),
        )
        for label, minimum, maximum in ranges:
            if minimum < 0 or maximum < minimum:
                raise ConfigurationError(f"Некорректный диапазон {label}")
        if self.avito_phone_first_round_attempts < 1:
            raise ConfigurationError("AVITO_PHONE_FIRST_ROUND_ATTEMPTS должен быть больше нуля")
        if self.avito_phone_second_round_attempts < 0:
            raise ConfigurationError(
                "AVITO_PHONE_SECOND_ROUND_ATTEMPTS не может быть отрицательным"
            )
        if self.max_attempts < 1:
            raise ConfigurationError("PIPELINE_MAX_ATTEMPTS должен быть больше нуля")
        if self.duplicate_policy not in {"skip", "create_lead"}:
            raise ConfigurationError("CRM_DUPLICATE_POLICY: допустимо skip или create_lead")

    def ensure_runtime_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.output_dir,
            self.logs_dir,
            self.browser_profile_dir,
            self.screenshot_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def require_crm_credentials(self) -> None:
        missing = []
        if not self.lptracker_login:
            missing.append("LPTRACKER_LOGIN")
        if not self.lptracker_password:
            missing.append("LPTRACKER_PASSWORD")
        if missing:
            raise ConfigurationError("Не заданы настройки CRM: " + ", ".join(missing))

    def require_crm_destination(self) -> None:
        missing = []
        if not (self.lptracker_project_id or self.lptracker_project_name):
            missing.append("LPTRACKER_PROJECT_ID или LPTRACKER_PROJECT_NAME")
        if missing:
            raise ConfigurationError("Не заданы настройки CRM: " + ", ".join(missing))

    def require_crm(self) -> None:
        self.require_crm_credentials()
        self.require_crm_destination()
