from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from urllib.parse import urlsplit

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


def _time(name: str, default: str) -> time:
    value = os.getenv(name, default).strip()
    try:
        hour, minute = (int(part) for part in value.split(":"))
        return time(hour=hour, minute=minute)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name}: ожидалось время ЧЧ:ММ") from exc


CHAT_ID_RE = re.compile(r"(?:-?\d+|@[A-Za-z0-9_]{5,})")
EMAIL_ADDRESS_RE = re.compile(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+")
MAX_RECIPIENT_RE = re.compile(r"(?:(?:user|chat):)?-?\d+", re.IGNORECASE)


def parse_chat_ids(value: str) -> tuple[str, ...]:
    candidates = [part.strip() for part in re.split(r"[,;\s]+", value) if part.strip()]
    invalid = [candidate for candidate in candidates if not CHAT_ID_RE.fullmatch(candidate)]
    if invalid:
        raise ConfigurationError("Некорректный Telegram Chat ID: " + ", ".join(invalid[:3]))
    return tuple(dict.fromkeys(candidates))


def parse_reminder_minutes(value: str) -> tuple[float, ...]:
    raw_values = [part.strip() for part in re.split(r"[,;\s]+", value) if part.strip()]
    if not raw_values:
        return ()
    try:
        result = tuple(float(part) for part in raw_values)
    except ValueError as exc:
        raise ConfigurationError(
            "TELEGRAM_CAPTCHA_REMINDER_MINUTES: укажите минуты через запятую"
        ) from exc
    if any(minutes <= 0 for minutes in result):
        raise ConfigurationError("Интервалы Telegram должны быть больше нуля")
    if tuple(sorted(set(result))) != result:
        raise ConfigurationError("Интервалы Telegram должны возрастать и не повторяться")
    return result


def parse_email_addresses(value: str) -> tuple[str, ...]:
    candidates = [part.strip() for part in re.split(r"[,;\s]+", value) if part.strip()]
    invalid = [candidate for candidate in candidates if not EMAIL_ADDRESS_RE.fullmatch(candidate)]
    if invalid:
        raise ConfigurationError("Некорректный email: " + ", ".join(invalid[:3]))
    return tuple(dict.fromkeys(candidate.casefold() for candidate in candidates))


def parse_max_recipients(value: str) -> tuple[str, ...]:
    candidates = [part.strip() for part in re.split(r"[,;\s]+", value) if part.strip()]
    invalid = [candidate for candidate in candidates if not MAX_RECIPIENT_RE.fullmatch(candidate)]
    if invalid:
        raise ConfigurationError(
            "Некорректный MAX ID: " + ", ".join(invalid[:3]) + ". Формат: user:123 или chat:456"
        )
    normalized = []
    for candidate in candidates:
        lowered = candidate.lower()
        normalized.append(lowered if ":" in lowered else f"user:{lowered}")
    return tuple(dict.fromkeys(normalized))


@dataclass(frozen=True, slots=True)
class Settings:
    root_dir: Path
    data_dir: Path
    output_dir: Path
    logs_dir: Path
    state_db: Path
    browser_profile_dir: Path
    avito_browser_channel: str
    avito_browser_driver: str
    avito_extension_port: int
    avito_extension_token: str
    avito_extension_connect_timeout: float
    screenshot_dir: Path

    lptracker_base_url: str
    lptracker_login: str
    lptracker_password: str
    lptracker_project_id: int | None
    lptracker_project_name: str
    lptracker_field_name: str
    lptracker_field_value: str
    lptracker_service_name: str
    lptracker_autoresponder_funnel_name: str
    lptracker_no_answer_funnel_name: str
    lptracker_new_lead_funnel_name: str
    lptracker_repeat_funnel_name: str
    lptracker_pending_funnel_names: tuple[str, ...]
    lptracker_timezone: str
    crm_monitor_max_hours: float
    crm_monitor_batch_size: int
    duplicate_policy: str
    crm_duplicate_window_days: float

    robot_handoff_enabled: bool
    robot_handoff_source_funnel_name: str
    robot_handoff_source_field_values: tuple[str, ...]
    robot_handoff_target_funnel_name: str
    robot_handoff_field_name: str
    robot_handoff_field_value: str
    robot_handoff_stage_date_field_name: str
    robot_handoff_stage_delay_days: int
    robot_handoff_poll_seconds: float
    robot_handoff_lookback_hours: float
    robot_handoff_batch_size: int
    robot_handoff_min_confidence: float
    gemini_api_key: str
    gemini_model: str
    gemini_api_base_url: str
    gemini_max_audio_bytes: int

    google_credentials_file: Path | None
    google_spreadsheet_id: str
    google_worksheet: str
    google_control_worksheet: str
    google_history_worksheet: str
    google_plan_worksheet: str
    remote_control_poll_seconds: float

    url_column: str
    status_column: str
    phone_column: str
    crm_lead_column: str
    funnel_stage_column: str
    crm_create_count_column: str
    repeat_crm_lead_column: str
    repeat_phone_attempts_column: str
    error_column: str
    attempts_column: str
    processed_at_column: str
    run_id_column: str
    next_retry_at_column: str

    timezone_guard_enabled: bool
    local_call_start: time
    local_lead_cutoff: time
    phone_retry_delay_minutes: float

    avito_headless: bool
    avito_min_delay: float
    avito_max_delay: float
    avito_page_timeout: float
    avito_manual_timeout: float
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
    telegram_bot_token: str
    telegram_primary_chat_ids: tuple[str, ...]
    telegram_backup_chat_ids: tuple[str, ...]
    telegram_reminder_minutes: tuple[float, ...]
    telegram_request_timeout: float
    telegram_send_attempts: int
    telegram_completion_primary: bool
    telegram_completion_backup: bool
    max_api_base_url: str
    max_bot_token: str
    max_primary_recipients: tuple[str, ...]
    max_backup_recipients: tuple[str, ...]
    max_request_timeout: float
    max_send_attempts: int
    max_completion_primary: bool
    max_completion_backup: bool
    smtp_host: str
    smtp_port: int
    smtp_security: str
    smtp_username: str
    smtp_password: str
    smtp_from_address: str
    email_primary_recipients: tuple[str, ...]
    email_backup_recipients: tuple[str, ...]
    email_request_timeout: float
    email_send_attempts: int
    email_completion_primary: bool
    email_completion_backup: bool
    notification_computer_name: str
    tesseract_cmd: str
    ocr_min_agreement: int
    max_attempts: int
    repeat_phone_max_attempts: int
    max_consecutive_failures: int

    @classmethod
    def load(
        cls,
        root_dir: Path | None = None,
        *,
        refresh_env: bool = False,
    ) -> Settings:
        root = (root_dir or Path.cwd()).resolve()
        # The desktop application starts short-lived workers, but the Google
        # remote controller can stay alive for days.  ``refresh_env=True`` lets
        # that controller deliberately reload GUI-saved settings before every
        # command instead of retaining recipients from its startup.  Normal CLI
        # loads keep the conventional external-environment precedence.
        load_dotenv(root / ".env", override=refresh_env, encoding="utf-8-sig")
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
            avito_browser_channel=os.getenv("AVITO_BROWSER_CHANNEL", "").strip().casefold(),
            avito_browser_driver=os.getenv("AVITO_BROWSER_DRIVER", "playwright").strip().casefold(),
            avito_extension_port=_int("AVITO_EXTENSION_PORT", 8765) or 8765,
            avito_extension_token=os.getenv("AVITO_EXTENSION_TOKEN", "").strip(),
            avito_extension_connect_timeout=_float("AVITO_EXTENSION_CONNECT_TIMEOUT_SECONDS", 30.0),
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
            lptracker_autoresponder_funnel_name=os.getenv(
                "LPTRACKER_AUTORESPONDER_FUNNEL_NAME", "Автоответчики"
            ).strip(),
            lptracker_no_answer_funnel_name=os.getenv(
                "LPTRACKER_NO_ANSWER_FUNNEL_NAME", "Недозвон"
            ).strip(),
            lptracker_new_lead_funnel_name=os.getenv(
                "LPTRACKER_NEW_LEAD_FUNNEL_NAME", "Новый Лид"
            ).strip(),
            lptracker_repeat_funnel_name=os.getenv(
                "LPTRACKER_REPEAT_FUNNEL_NAME", "Повторный лид"
            ).strip(),
            lptracker_pending_funnel_names=tuple(
                name.strip()
                for name in os.getenv(
                    "LPTRACKER_PENDING_FUNNEL_NAMES", "Новый лид,⚙️ Лид с робота"
                ).split(",")
                if name.strip()
            ),
            lptracker_timezone=os.getenv("LPTRACKER_TIMEZONE", "Europe/Moscow").strip(),
            crm_monitor_max_hours=_float("CRM_MONITOR_MAX_HOURS", 24.0),
            crm_monitor_batch_size=_int("CRM_MONITOR_BATCH_SIZE", 20) or 20,
            duplicate_policy=os.getenv("CRM_DUPLICATE_POLICY", "skip").strip().lower(),
            crm_duplicate_window_days=_float("CRM_DUPLICATE_WINDOW_DAYS", 7.0),
            robot_handoff_enabled=_bool("ROBOT_HANDOFF_ENABLED", False),
            robot_handoff_source_funnel_name=os.getenv(
                "ROBOT_HANDOFF_SOURCE_FUNNEL_NAME", "⚙️ Лид с робота"
            ).strip(),
            robot_handoff_source_field_values=tuple(
                value.strip()
                for value in os.getenv(
                    "ROBOT_HANDOFF_SOURCE_FIELD_VALUES",
                    "Сбор № лпр (Ав, ремонт кв. под ключ)|"
                    "Сбор № лпр (Ян, ремонт кв. под ключ)",
                ).split("|")
                if value.strip()
            ),
            robot_handoff_target_funnel_name=os.getenv(
                "ROBOT_HANDOFF_TARGET_FUNNEL_NAME", "Новый лид"
            ).strip(),
            robot_handoff_field_name=os.getenv(
                "ROBOT_HANDOFF_FIELD_NAME", "Тег+ для новых с Ав и Ян"
            ).strip(),
            robot_handoff_field_value=os.getenv(
                "ROBOT_HANDOFF_FIELD_VALUE", "Предлагаем бесплатный аудит авито"
            ).strip(),
            robot_handoff_stage_date_field_name=os.getenv(
                "ROBOT_HANDOFF_STAGE_DATE_FIELD_NAME", "Дата шага"
            ).strip(),
            robot_handoff_stage_delay_days=int(
                _int("ROBOT_HANDOFF_STAGE_DELAY_DAYS", 2) or 0
            ),
            robot_handoff_poll_seconds=_float("ROBOT_HANDOFF_POLL_SECONDS", 120.0),
            robot_handoff_lookback_hours=_float("ROBOT_HANDOFF_LOOKBACK_HOURS", 72.0),
            robot_handoff_batch_size=int(_int("ROBOT_HANDOFF_BATCH_SIZE", 3) or 0),
            robot_handoff_min_confidence=_float("ROBOT_HANDOFF_MIN_CONFIDENCE", 0.93),
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip(),
            gemini_api_base_url=os.getenv(
                "GEMINI_API_BASE_URL", "https://generativelanguage.googleapis.com"
            )
            .strip()
            .rstrip("/"),
            gemini_max_audio_bytes=int(
                _int("GEMINI_MAX_AUDIO_BYTES", 14 * 1024 * 1024) or 0
            ),
            google_credentials_file=Path(credentials).expanduser().resolve()
            if credentials
            else None,
            google_spreadsheet_id=os.getenv("GOOGLE_SPREADSHEET_ID", "").strip(),
            google_worksheet=os.getenv("GOOGLE_WORKSHEET", "Лист1").strip(),
            google_control_worksheet=os.getenv("GOOGLE_CONTROL_WORKSHEET", "Управление").strip(),
            google_history_worksheet=os.getenv(
                "GOOGLE_HISTORY_WORKSHEET", "История запусков"
            ).strip(),
            google_plan_worksheet=os.getenv("GOOGLE_PLAN_WORKSHEET", "План загрузки").strip(),
            remote_control_poll_seconds=_float("REMOTE_CONTROL_POLL_SECONDS", 20.0),
            url_column=os.getenv("QUEUE_URL_COLUMN", "Ссылка").strip(),
            status_column=os.getenv("QUEUE_STATUS_COLUMN", "Статус").strip(),
            phone_column=os.getenv("QUEUE_PHONE_COLUMN", "Телефон").strip(),
            crm_lead_column=os.getenv("QUEUE_CRM_LEAD_COLUMN", "CRM lead ID").strip(),
            funnel_stage_column=os.getenv("QUEUE_FUNNEL_STAGE_COLUMN", "Шаг воронки").strip(),
            crm_create_count_column=os.getenv(
                "QUEUE_CRM_CREATE_COUNT_COLUMN", "Попытка завода в CRM"
            ).strip(),
            repeat_crm_lead_column=os.getenv(
                "QUEUE_REPEAT_CRM_LEAD_COLUMN", "Повторный CRM ID"
            ).strip(),
            repeat_phone_attempts_column=os.getenv(
                "QUEUE_REPEAT_PHONE_ATTEMPTS_COLUMN", "Попытки повторного открытия"
            ).strip(),
            error_column=os.getenv("QUEUE_ERROR_COLUMN", "Ошибка").strip(),
            attempts_column=os.getenv("QUEUE_ATTEMPTS_COLUMN", "Попытки").strip(),
            processed_at_column=os.getenv("QUEUE_PROCESSED_AT_COLUMN", "Обработано").strip(),
            run_id_column=os.getenv("QUEUE_RUN_ID_COLUMN", "Run ID").strip(),
            next_retry_at_column=os.getenv(
                "QUEUE_NEXT_RETRY_AT_COLUMN", "Следующая попытка"
            ).strip(),
            timezone_guard_enabled=_bool("LOCAL_TIME_GUARD_ENABLED", True),
            local_call_start=_time("LOCAL_CALL_START", "10:00"),
            local_lead_cutoff=_time("LOCAL_LEAD_CUTOFF", "19:45"),
            phone_retry_delay_minutes=_float("PHONE_RETRY_DELAY_MINUTES", 30.0),
            avito_headless=_bool("AVITO_HEADLESS", False),
            avito_min_delay=_float("AVITO_MIN_DELAY_SECONDS", 7.0),
            avito_max_delay=_float("AVITO_MAX_DELAY_SECONDS", 15.0),
            avito_page_timeout=_float("AVITO_PAGE_TIMEOUT_SECONDS", 45.0),
            avito_manual_timeout=_float("AVITO_MANUAL_TIMEOUT_SECONDS", 0.0),
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
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_primary_chat_ids=parse_chat_ids(os.getenv("TELEGRAM_PRIMARY_CHAT_IDS", "")),
            telegram_backup_chat_ids=parse_chat_ids(os.getenv("TELEGRAM_BACKUP_CHAT_IDS", "")),
            telegram_reminder_minutes=parse_reminder_minutes(
                os.getenv("TELEGRAM_CAPTCHA_REMINDER_MINUTES", "30,60")
            ),
            telegram_request_timeout=_float("TELEGRAM_REQUEST_TIMEOUT_SECONDS", 15.0),
            telegram_send_attempts=_int("TELEGRAM_SEND_ATTEMPTS", 3) or 3,
            telegram_completion_primary=_bool("TELEGRAM_COMPLETION_PRIMARY", True),
            telegram_completion_backup=_bool("TELEGRAM_COMPLETION_BACKUP", True),
            max_api_base_url=os.getenv("MAX_API_BASE_URL", "https://platform-api2.max.ru")
            .strip()
            .rstrip("/"),
            max_bot_token=os.getenv("MAX_BOT_TOKEN", "").strip(),
            max_primary_recipients=parse_max_recipients(os.getenv("MAX_PRIMARY_RECIPIENTS", "")),
            max_backup_recipients=parse_max_recipients(os.getenv("MAX_BACKUP_RECIPIENTS", "")),
            max_request_timeout=_float("MAX_REQUEST_TIMEOUT_SECONDS", 15.0),
            max_send_attempts=_int("MAX_SEND_ATTEMPTS", 3) or 3,
            max_completion_primary=_bool("MAX_COMPLETION_PRIMARY", True),
            max_completion_backup=_bool("MAX_COMPLETION_BACKUP", True),
            smtp_host=os.getenv("SMTP_HOST", "smtp.yandex.ru").strip(),
            smtp_port=int(_int("SMTP_PORT", 465) or 465),
            smtp_security=os.getenv("SMTP_SECURITY", "ssl").strip().lower(),
            smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            smtp_from_address=os.getenv("SMTP_FROM_ADDRESS", "").strip(),
            email_primary_recipients=parse_email_addresses(
                os.getenv("EMAIL_PRIMARY_RECIPIENTS", "")
            ),
            email_backup_recipients=parse_email_addresses(os.getenv("EMAIL_BACKUP_RECIPIENTS", "")),
            email_request_timeout=_float("EMAIL_REQUEST_TIMEOUT_SECONDS", 20.0),
            email_send_attempts=_int("EMAIL_SEND_ATTEMPTS", 2) or 2,
            email_completion_primary=_bool("EMAIL_COMPLETION_PRIMARY", True),
            email_completion_backup=_bool("EMAIL_COMPLETION_BACKUP", True),
            notification_computer_name=os.getenv(
                "NOTIFICATION_COMPUTER_NAME", os.getenv("COMPUTERNAME", socket.gethostname())
            ).strip(),
            tesseract_cmd=os.getenv("TESSERACT_CMD", "").strip(),
            ocr_min_agreement=_int("OCR_MIN_AGREEMENT", 2) or 2,
            # One session contains two actual reveal clicks (with one reload).
            # A second session contains the single final click: three clicks total.
            max_attempts=min(_int("PIPELINE_MAX_ATTEMPTS", 2) or 2, 2),
            repeat_phone_max_attempts=_int("CRM_REPEAT_PHONE_MAX_ATTEMPTS", 3) or 3,
            max_consecutive_failures=_int("PIPELINE_MAX_CONSECUTIVE_FAILURES", 5) or 5,
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.avito_min_delay < 0 or self.avito_max_delay < self.avito_min_delay:
            raise ConfigurationError("Некорректный диапазон задержек Avito")
        if self.robot_handoff_poll_seconds < 30:
            raise ConfigurationError("ROBOT_HANDOFF_POLL_SECONDS должен быть не меньше 30")
        if not 1 <= self.robot_handoff_lookback_hours <= 24 * 14:
            raise ConfigurationError("ROBOT_HANDOFF_LOOKBACK_HOURS: допустимо от 1 до 336")
        if not 1 <= self.robot_handoff_batch_size <= 10:
            raise ConfigurationError("ROBOT_HANDOFF_BATCH_SIZE: допустимо от 1 до 10")
        if not 0.5 <= self.robot_handoff_min_confidence <= 1.0:
            raise ConfigurationError("ROBOT_HANDOFF_MIN_CONFIDENCE: допустимо от 0.5 до 1")
        if not 1 <= self.robot_handoff_stage_delay_days <= 30:
            raise ConfigurationError(
                "ROBOT_HANDOFF_STAGE_DELAY_DAYS: допустимо от 1 до 30 дней"
            )
        if not 1024 * 1024 <= self.gemini_max_audio_bytes <= 14 * 1024 * 1024:
            raise ConfigurationError(
                "GEMINI_MAX_AUDIO_BYTES: допустимо от 1 до 14 МБ для inline-запроса"
            )
        gemini_base = urlsplit(self.gemini_api_base_url)
        if (
            gemini_base.scheme != "https"
            or gemini_base.hostname != "generativelanguage.googleapis.com"
            or gemini_base.username
            or gemini_base.password
        ):
            raise ConfigurationError(
                "GEMINI_API_BASE_URL должен быть официальным HTTPS-адресом "
                "https://generativelanguage.googleapis.com"
            )
        if self.robot_handoff_enabled:
            required = {
                "ROBOT_HANDOFF_SOURCE_FUNNEL_NAME": self.robot_handoff_source_funnel_name,
                "ROBOT_HANDOFF_SOURCE_FIELD_VALUES": (
                    "|".join(self.robot_handoff_source_field_values)
                ),
                "ROBOT_HANDOFF_TARGET_FUNNEL_NAME": self.robot_handoff_target_funnel_name,
                "ROBOT_HANDOFF_FIELD_NAME": self.robot_handoff_field_name,
                "ROBOT_HANDOFF_FIELD_VALUE": self.robot_handoff_field_value,
                "ROBOT_HANDOFF_STAGE_DATE_FIELD_NAME": (
                    self.robot_handoff_stage_date_field_name
                ),
                "GEMINI_API_KEY": self.gemini_api_key,
                "GEMINI_MODEL": self.gemini_model,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ConfigurationError(
                    "Для ROBOT_HANDOFF_ENABLED=true не заполнено: " + ", ".join(missing)
                )
            normalized_source_values = {
                value.casefold().strip() for value in self.robot_handoff_source_field_values
            }
            if len(normalized_source_values) != len(self.robot_handoff_source_field_values):
                raise ConfigurationError(
                    "ROBOT_HANDOFF_SOURCE_FIELD_VALUES содержит повторяющиеся значения"
                )
        if self.avito_manual_timeout < 0 or 0 < self.avito_manual_timeout < 60:
            raise ConfigurationError(
                "AVITO_MANUAL_TIMEOUT_SECONDS: 0 означает ждать до решения; "
                "иное значение должно быть не меньше 60 секунд"
            )
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
        if self.avito_browser_channel not in {"", "chrome"}:
            raise ConfigurationError("AVITO_BROWSER_CHANNEL должен быть пустым или равен chrome")
        if self.avito_browser_driver not in {"playwright", "chrome_extension"}:
            raise ConfigurationError(
                "AVITO_BROWSER_DRIVER должен быть playwright или chrome_extension"
            )
        if not 1024 <= self.avito_extension_port <= 65535:
            raise ConfigurationError("AVITO_EXTENSION_PORT должен быть от 1024 до 65535")
        if self.avito_extension_connect_timeout <= 0:
            raise ConfigurationError(
                "AVITO_EXTENSION_CONNECT_TIMEOUT_SECONDS должен быть больше нуля"
            )
        if self.avito_browser_driver == "chrome_extension" and len(self.avito_extension_token) < 32:
            raise ConfigurationError(
                "Для chrome_extension задайте AVITO_EXTENSION_TOKEN длиной не менее 32 символов"
            )
        if self.avito_phone_second_round_attempts < 0:
            raise ConfigurationError(
                "AVITO_PHONE_SECOND_ROUND_ATTEMPTS не может быть отрицательным"
            )
        if self.max_attempts < 1:
            raise ConfigurationError("PIPELINE_MAX_ATTEMPTS должен быть больше нуля")
        if self.repeat_phone_max_attempts < 1:
            raise ConfigurationError("CRM_REPEAT_PHONE_MAX_ATTEMPTS должен быть больше нуля")
        if self.crm_monitor_max_hours <= 0:
            raise ConfigurationError("CRM_MONITOR_MAX_HOURS должен быть больше нуля")
        if not 1 <= self.crm_monitor_batch_size <= 100:
            raise ConfigurationError("CRM_MONITOR_BATCH_SIZE должен быть от 1 до 100")
        if not self.lptracker_pending_funnel_names:
            raise ConfigurationError("LPTRACKER_PENDING_FUNNEL_NAMES не может быть пустым")
        if self.local_lead_cutoff <= self.local_call_start:
            raise ConfigurationError("LOCAL_LEAD_CUTOFF должен быть позже LOCAL_CALL_START")
        if self.phone_retry_delay_minutes < 1:
            raise ConfigurationError("PHONE_RETRY_DELAY_MINUTES должен быть не меньше 1")
        if self.telegram_request_timeout <= 0:
            raise ConfigurationError("TELEGRAM_REQUEST_TIMEOUT_SECONDS должен быть больше нуля")
        if self.telegram_send_attempts < 1 or self.telegram_send_attempts > 10:
            raise ConfigurationError("TELEGRAM_SEND_ATTEMPTS должен быть от 1 до 10")
        max_api_url = urlsplit(self.max_api_base_url)
        if (
            max_api_url.scheme != "https"
            or max_api_url.hostname != "platform-api2.max.ru"
            or max_api_url.path not in {"", "/"}
        ):
            raise ConfigurationError("MAX_API_BASE_URL должен быть https://platform-api2.max.ru")
        if self.max_bot_token and (
            len(self.max_bot_token) < 20 or any(char.isspace() for char in self.max_bot_token)
        ):
            raise ConfigurationError("MAX_BOT_TOKEN имеет некорректный формат")
        if self.max_primary_recipients and not self.max_bot_token:
            raise ConfigurationError("Для MAX_PRIMARY_RECIPIENTS нужен MAX_BOT_TOKEN")
        if self.max_backup_recipients and not self.max_primary_recipients:
            raise ConfigurationError(
                "MAX_BACKUP_RECIPIENTS требует хотя бы одного основного получателя"
            )
        if self.max_request_timeout <= 0:
            raise ConfigurationError("MAX_REQUEST_TIMEOUT_SECONDS должен быть больше нуля")
        if self.max_send_attempts < 1 or self.max_send_attempts > 10:
            raise ConfigurationError("MAX_SEND_ATTEMPTS должен быть от 1 до 10")
        if not 1 <= self.smtp_port <= 65_535:
            raise ConfigurationError("SMTP_PORT должен быть от 1 до 65535")
        if self.smtp_security not in {"ssl", "starttls"}:
            raise ConfigurationError("SMTP_SECURITY: допустимо ssl или starttls")
        if self.smtp_from_address and not EMAIL_ADDRESS_RE.fullmatch(self.smtp_from_address):
            raise ConfigurationError("SMTP_FROM_ADDRESS содержит некорректный email")
        if self.email_request_timeout <= 0:
            raise ConfigurationError("EMAIL_REQUEST_TIMEOUT_SECONDS должен быть больше нуля")
        if self.email_send_attempts < 1 or self.email_send_attempts > 10:
            raise ConfigurationError("EMAIL_SEND_ATTEMPTS должен быть от 1 до 10")
        email_enabled = bool(
            self.smtp_host
            and self.smtp_username
            and self.smtp_password
            and self.email_primary_recipients
        )
        if (
            (
                (self.telegram_bot_token and self.telegram_primary_chat_ids)
                or (self.max_bot_token and self.max_primary_recipients)
                or email_enabled
            )
            and self.telegram_reminder_minutes
            and self.avito_manual_timeout > 0
        ):
            last_reminder_seconds = self.telegram_reminder_minutes[-1] * 60
            if last_reminder_seconds >= self.avito_manual_timeout:
                raise ConfigurationError("Последнее напоминание должно быть раньше таймаута капчи")
        if self.duplicate_policy not in {"skip", "create_lead"}:
            raise ConfigurationError("CRM_DUPLICATE_POLICY: допустимо skip или create_lead")
        if self.crm_duplicate_window_days < 0:
            raise ConfigurationError("CRM_DUPLICATE_WINDOW_DAYS не может быть меньше 0")
        funnel_names = {
            "LPTRACKER_AUTORESPONDER_FUNNEL_NAME": self.lptracker_autoresponder_funnel_name,
            "LPTRACKER_NO_ANSWER_FUNNEL_NAME": self.lptracker_no_answer_funnel_name,
            "LPTRACKER_NEW_LEAD_FUNNEL_NAME": self.lptracker_new_lead_funnel_name,
            "LPTRACKER_REPEAT_FUNNEL_NAME": self.lptracker_repeat_funnel_name,
        }
        empty_funnels = [name for name, value in funnel_names.items() if not value]
        if empty_funnels:
            raise ConfigurationError(f"{', '.join(empty_funnels)} не может быть пустым")
        # LPTRACKER_TIMEZONE is required only by the CRM rule that compares a
        # lead creation time with its first call. Validate it lazily in that
        # CRM operation so unrelated commands (notably ``avito-profile``) can
        # still open the persistent browser profile.
        if not self.google_control_worksheet:
            raise ConfigurationError("GOOGLE_CONTROL_WORKSHEET не может быть пустым")
        if not self.google_history_worksheet:
            raise ConfigurationError("GOOGLE_HISTORY_WORKSHEET не может быть пустым")
        if self.timezone_guard_enabled and not self.google_plan_worksheet:
            raise ConfigurationError("GOOGLE_PLAN_WORKSHEET не может быть пустым")
        remote_tabs = {
            self.google_control_worksheet.casefold(),
            self.google_history_worksheet.casefold(),
        }
        if len(remote_tabs) != 2 or self.google_worksheet.casefold() in remote_tabs:
            raise ConfigurationError("Лист очереди, пульт и история должны иметь разные имена")
        if not 5 <= self.remote_control_poll_seconds <= 300:
            raise ConfigurationError("REMOTE_CONTROL_POLL_SECONDS должен быть от 5 до 300")

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
