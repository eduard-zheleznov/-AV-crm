from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import logging
import os
import secrets
import subprocess
import threading
import time
import uuid
import webbrowser
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from PIL import Image

from avito_crm.config import Settings
from avito_crm.errors import (
    BrowserOperationError,
    InactiveListingError,
    InvalidListingError,
    ManualActionRequired,
    NotificationError,
    OperatorStopRequested,
    PageNotReadyError,
    PhoneButtonUnavailableError,
    PhoneNotFoundError,
)
from avito_crm.models import PhoneResult
from avito_crm.notifications import NotificationRouter
from avito_crm.ocr import PhoneOcr
from avito_crm.phone import canonical_avito_url, normalize_phone

LOGGER = logging.getLogger(__name__)
MAX_EVENT_BYTES = 12 * 1024 * 1024
MAX_TAB_CAPTURE_PNG_BYTES = 8 * 1024 * 1024
MAX_TAB_CAPTURE_PIXELS = 64_000_000
EXPECTED_EXTENSION_VERSION = "1.0.7.8"
NAVIGATION_ATTEMPTS = 2
NAVIGATION_RETRY_DELAY_SECONDS = 1.5
EXTENSION_HEARTBEAT_STALE_SECONDS = 15.0
DEFAULT_MANUAL_WAIT_SECONDS = 12 * 60 * 60


def _extension_operation_timeout(settings: Settings, max_clicks: int) -> float:
    """Return a complete bound for sequential navigation and reveal work."""
    navigation = (
        settings.avito_page_timeout * NAVIGATION_ATTEMPTS
        + NAVIGATION_RETRY_DELAY_SECONDS * (NAVIGATION_ATTEMPTS - 1)
    )
    reveal = settings.avito_page_timeout + settings.avito_temp_number_wait_max
    retries = max(0, max_clicks - 1)
    return max(
        30.0,
        navigation
        + (reveal * max_clicks)
        + retries * (settings.avito_phone_retry_max + navigation)
        + 30.0,
    )


def _manual_wait_timeout(settings: Settings) -> float:
    """Bound manual pauses even when legacy configuration requested infinity."""
    return settings.avito_manual_timeout or DEFAULT_MANUAL_WAIT_SECONDS


@dataclass(slots=True)
class ExtensionEvent:
    event_type: str
    status: str
    payload: dict[str, Any]


class _BridgeState:
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.command: dict[str, Any] | None = None
        self.dispatched = False
        self.events: deque[ExtensionEvent] = deque()
        self.result: ExtensionEvent | None = None
        self.last_seen = 0.0
        self.extension_version = ""
        self.manual_waiting = False
        self.stopped = False


class ExtensionBridge:
    """Authenticated localhost long-poll bridge for the ordinary Chrome extension."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state = _BridgeState()
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.server is not None:
            return
        handler = self._handler_type()
        try:
            self.server = ThreadingHTTPServer(
                ("127.0.0.1", self.settings.avito_extension_port), handler
            )
        except OSError as exc:
            raise BrowserOperationError(
                "Не удалось запустить локальный мост Chrome на "
                f"127.0.0.1:{self.settings.avito_extension_port}: {exc}"
            ) from exc
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="avito-chrome-extension-bridge",
            daemon=True,
        )
        self.thread.start()
        LOGGER.info(
            "Локальный мост обычного Chrome слушает 127.0.0.1:%s",
            self.settings.avito_extension_port,
        )

    def close(self) -> None:
        with self.state.condition:
            self.state.stopped = True
            self.state.condition.notify_all()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=3)
        self.server = None
        self.thread = None

    def wait_for_connection(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0, timeout)
        with self.state.condition:
            while time.monotonic() - self.state.last_seen > 5:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.state.condition.wait(timeout=min(remaining, 0.5))
            return True

    def execute(
        self,
        *,
        url: str,
        row_id: str,
        max_clicks: int,
        status_callback: Callable[[ExtensionEvent], None] | None = None,
    ) -> ExtensionEvent:
        if self.server is None:
            raise RuntimeError("ExtensionBridge должен быть запущен")
        command_id = uuid.uuid4().hex
        command = {
            "id": command_id,
            "type": "reveal_phone",
            "url": url,
            "rowId": row_id,
            "maxClicks": max_clicks,
            "pageTimeoutMs": round(self.settings.avito_page_timeout * 1000),
            "manualTimeoutMs": round(_manual_wait_timeout(self.settings) * 1000),
            "phoneWaitMs": round(self.settings.avito_temp_number_wait_max * 1000),
            "retryDelayMs": round(self.settings.avito_phone_retry_max * 1000),
            "navigationAttempts": NAVIGATION_ATTEMPTS,
            "navigationRetryDelayMs": round(NAVIGATION_RETRY_DELAY_SECONDS * 1000),
        }
        connection_deadline = time.monotonic() + self.settings.avito_extension_connect_timeout
        with self.state.condition:
            while time.monotonic() - self.state.last_seen > 5:
                remaining = connection_deadline - time.monotonic()
                if remaining <= 0:
                    raise BrowserOperationError(
                        "Расширение Avito CRM не подключилось к локальному мосту. "
                        "Откройте обычный Chrome и проверьте, что расширение включено."
                    )
                self.state.condition.wait(timeout=min(remaining, 0.5))
            if self.state.extension_version != EXPECTED_EXTENSION_VERSION:
                installed = self.state.extension_version or "не определена"
                raise BrowserOperationError(
                    "Расширение Avito CRM в Chrome устарело "
                    f"(установлено: {installed}; требуется: {EXPECTED_EXTENSION_VERSION}). "
                    "Откройте chrome://extensions и нажмите «Обновить» в режиме разработчика."
                )
            if self.state.command is not None:
                raise BrowserOperationError("Локальный мост Chrome уже выполняет другую команду")
            self.state.command = command
            self.state.dispatched = False
            self.state.events.clear()
            self.state.result = None
            self.state.condition.notify_all()

        operation_timeout = _extension_operation_timeout(self.settings, max_clicks)
        deadline: float | None = time.monotonic() + operation_timeout
        stop_seen_at: float | None = None
        manual_started_at: float | None = None
        manual_deadline: float | None = None
        manual_reason = "ручная проверка Avito"
        reminder_seconds = tuple(
            minutes * 60 for minutes in self.settings.telegram_reminder_minutes
        )
        reminder_index = 0
        try:
            while True:
                event: ExtensionEvent | None = None
                with self.state.condition:
                    if self.state.events:
                        event = self.state.events.popleft()
                    elif self.state.result is not None:
                        return self.state.result
                    elif self.state.stopped:
                        raise BrowserOperationError("Локальный мост Chrome остановлен")
                    else:
                        now = time.monotonic()
                        if (self.settings.data_dir / "STOP").exists():
                            stop_seen_at = stop_seen_at or now
                            # Let the extension observe /v1/command-status and
                            # tear down its waiting content-script promise.
                            if now - stop_seen_at >= 10:
                                raise OperatorStopRequested(
                                    "Ожидание Chrome остановлено оператором"
                                )
                        if (
                            manual_started_at is not None
                            and now - self.state.last_seen
                            > EXTENSION_HEARTBEAT_STALE_SECONDS
                        ):
                            raise PageNotReadyError(
                                "Связь с расширением Chrome потеряна во время ручной "
                                "проверки; строка безопасно возвращена в очередь"
                            )
                        if manual_deadline is not None and now >= manual_deadline:
                            raise ManualActionRequired(
                                "Ручная проверка Avito не завершена за 12 часов; "
                                "строка сохранена для проверки оператором"
                            )
                        if (
                            manual_started_at is not None
                            and reminder_index < len(reminder_seconds)
                            and now - manual_started_at >= reminder_seconds[reminder_index]
                        ):
                            event = ExtensionEvent(
                                "status",
                                "manual_reminder",
                                {
                                    "reason": manual_reason,
                                    "elapsed_seconds": now - manual_started_at,
                                    "escalate": reminder_index == len(reminder_seconds) - 1,
                                },
                            )
                            reminder_index += 1
                        elif deadline is not None and now >= deadline:
                            raise BrowserOperationError(
                                "Обычный Chrome не завершил открытие номера: нет ответа расширения"
                            )
                        else:
                            self.state.condition.wait(timeout=1.0)
                            continue
                if event is not None:
                    if event.status == "manual_required":
                        manual_started_at = time.monotonic()
                        manual_reason = str(event.payload.get("reason", manual_reason))
                        reminder_index = 0
                        manual_deadline = time.monotonic() + _manual_wait_timeout(self.settings)
                        deadline = None
                    elif event.status == "manual_cleared":
                        manual_started_at = None
                        manual_deadline = None
                        deadline = time.monotonic() + operation_timeout
                    if status_callback is not None:
                        status_callback(event)
        finally:
            with self.state.condition:
                if self.state.command and self.state.command.get("id") == command_id:
                    self.state.command = None
                    self.state.dispatched = False
                    self.state.events.clear()
                    self.state.result = None
                    self.state.manual_waiting = False
                    self.state.condition.notify_all()

    def _poll(
        self,
        timeout: float = 20.0,
        *,
        extension_version: str = "",
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        with self.state.condition:
            self.state.last_seen = time.monotonic()
            self.state.extension_version = extension_version.strip()
            self.state.condition.notify_all()
            if (
                self.state.command is not None
                and self.state.dispatched
                and self.state.manual_waiting
            ):
                # The polling loop cannot request another command while its
                # content-script promise is still running. Seeing a new poll here
                # therefore means Chrome/service-worker restarted and lost that
                # promise. Fail the row safely instead of waiting forever.
                self.state.manual_waiting = False
                self.state.result = ExtensionEvent(
                    "result",
                    "page_not_ready",
                    {
                        "reason": (
                            "Chrome перезапустился во время ручной проверки; "
                            "строка безопасно возвращена в очередь"
                        )
                    },
                )
                self.state.condition.notify_all()
            while not self.state.stopped:
                if self.state.command is not None and not self.state.dispatched:
                    self.state.dispatched = True
                    return dict(self.state.command)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.state.condition.wait(timeout=min(remaining, 1.0))
        return None

    def _receive_event(self, payload: dict[str, Any]) -> None:
        command_id = str(payload.get("id", ""))
        event_type = str(payload.get("type", ""))
        status = str(payload.get("status", ""))
        if event_type not in {"status", "result"} or not status:
            raise ValueError("Некорректный тип события расширения")
        event = ExtensionEvent(event_type, status, payload)
        with self.state.condition:
            self.state.last_seen = time.monotonic()
            if not self.state.command or command_id != self.state.command.get("id"):
                raise ValueError("Событие не относится к активной команде")
            if event_type == "result":
                self.state.manual_waiting = False
                self.state.result = event
            else:
                if status == "manual_required":
                    self.state.manual_waiting = True
                elif status == "manual_cleared":
                    self.state.manual_waiting = False
                self.state.events.append(event)
            self.state.condition.notify_all()

    def _authorized(self, header: str | None) -> bool:
        expected = f"Bearer {self.settings.avito_extension_token}"
        return bool(header) and secrets.compare_digest(header, expected)

    def _command_status(self, command_id: str) -> str:
        with self.state.condition:
            active_id = str((self.state.command or {}).get("id", ""))
            # Only the exact active command is a liveness heartbeat. A delayed
            # request from an older tab must not conceal a disconnected worker.
            if active_id and command_id == active_id:
                self.state.last_seen = time.monotonic()
                self.state.condition.notify_all()
            if self.state.stopped or (self.settings.data_dir / "STOP").exists():
                return "cancelled"
            if not command_id or command_id != active_id:
                return "inactive"
            return "active"

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "AvitoChromeBridge/1"

            def do_OPTIONS(self) -> None:  # noqa: N802
                self.send_response(HTTPStatus.NO_CONTENT)
                self._cors_headers()
                self.send_header(
                    "Access-Control-Allow-Headers",
                    "Authorization, Content-Type, X-Avito-CRM-Extension-Version",
                )
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                if not bridge._authorized(self.headers.get("Authorization")):
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                parsed = urlsplit(self.path)
                path = parsed.path
                if path == "/v1/health":
                    self._send_json(HTTPStatus.OK, {"ok": True})
                    return
                if path == "/v1/command-status":
                    command_id = str(parse_qs(parsed.query).get("id", [""])[0])
                    self._send_json(
                        HTTPStatus.OK,
                        {"status": bridge._command_status(command_id)},
                    )
                    return
                if path != "/v1/poll":
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                command = bridge._poll(
                    extension_version=self.headers.get(
                        "X-Avito-CRM-Extension-Version",
                        "",
                    )
                )
                if command is None:
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self._cors_headers()
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    return
                self._send_json(HTTPStatus.OK, command)

            def do_POST(self) -> None:  # noqa: N802
                if not bridge._authorized(self.headers.get("Authorization")):
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if urlsplit(self.path).path != "/v1/event":
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                try:
                    content_length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    content_length = 0
                if content_length <= 0 or content_length > MAX_EVENT_BYTES:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_length"})
                    return
                try:
                    payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("JSON должен быть объектом")
                    bridge._receive_event(payload)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_event", "detail": str(exc)[:160]},
                    )
                    return
                self._send_json(HTTPStatus.OK, {"ok": True})

            def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self._cors_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _cors_headers(self) -> None:
                self.send_header("Access-Control-Allow-Origin", "*")

            def log_message(self, _format: str, *_args: object) -> None:
                return

        return Handler


class ChromeExtensionBrowser:
    """Phone browser backed by a user-installed extension in ordinary Chrome."""

    def __init__(
        self,
        settings: Settings,
        ocr: PhoneOcr,
        notifier: NotificationRouter | None = None,
        *,
        save_failed_captures: bool = False,
        operation_status_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.ocr = ocr
        self.notifier = notifier or NotificationRouter(settings)
        self._owns_notifier = notifier is None
        self.save_failed_captures = save_failed_captures
        self.operation_status_callback = operation_status_callback
        self.bridge = ExtensionBridge(settings)
        self._manual_notified = False
        self._manual_pending = False
        self._manual_backup_alerted = False
        self.captchas_solved = 0

    def __enter__(self) -> ChromeExtensionBrowser:
        self.bridge.start()
        if os.name == "nt" and not self.bridge.wait_for_connection(2):
            _start_windows_chrome()
        return self

    def __exit__(self, *_args: object) -> None:
        self.bridge.close()
        if self._owns_notifier:
            self.notifier.close()

    def reveal_phone(self, url: str, row_id: str = "", *, max_clicks: int = 2) -> PhoneResult:
        if max_clicks not in {1, 2}:
            raise ValueError("max_clicks должен быть равен 1 или 2")
        canonical_url = canonical_avito_url(url)
        self._manual_notified = False
        self._manual_pending = False
        self._manual_backup_alerted = False
        try:
            event = self.bridge.execute(
                url=canonical_url,
                row_id=row_id,
                max_clicks=max_clicks,
                status_callback=lambda status: self._handle_status(status, canonical_url),
            )
        except OperatorStopRequested:
            self._notify_captcha_stopped(canonical_url)
            raise
        except PageNotReadyError:
            self._report_operation_status(
                "Технический шаг Chrome завершён; строка безопасно возвращена в очередь."
            )
            raise
        status = event.status
        payload = event.payload
        if status == "phone":
            phone = normalize_phone(str(payload.get("phone", "")))
            if not phone:
                raise PhoneNotFoundError("Расширение вернуло некорректный номер")
            return PhoneResult(phone, str(payload.get("source", "chrome-extension-dom")))
        if status == "tab_capture":
            png = _decode_screenshot(str(payload.get("screenshot", "")))
            capture = payload.get("capture")
            if not isinstance(capture, dict):
                raise BrowserOperationError(
                    "Расширение не передало проверяемые координаты номера внутри вкладки"
                )
            return self._read_tab_capture(png, capture)
        if status in {"screenshot", "screen_capture"}:
            raise BrowserOperationError(
                "Расширение вернуло устаревший снимок Windows. Обновите расширение Chrome "
                f"до версии {EXPECTED_EXTENSION_VERSION}."
            )
        if status == "page_not_ready":
            self._log_readiness_diagnostics(payload)
            self._report_operation_status(
                "Технический шаг Chrome завершён; строка безопасно возвращена в очередь."
            )
            raise PageNotReadyError(
                str(
                    payload.get(
                        "reason",
                        "Страница объявления не успела полностью отобразиться",
                    )
                )
            )
        if status == "invalid_listing":
            self._log_readiness_diagnostics(payload)
            raise InvalidListingError(
                str(payload.get("reason", "Ссылка не ведёт на доступное объявление Avito"))
            )
        if status == "inactive":
            raise InactiveListingError(str(payload.get("reason", "объявление недоступно")))
        if status == "button_missing":
            raise PhoneButtonUnavailableError(
                str(payload.get("reason", "Кнопка показа телефона не найдена"))
            )
        if status == "challenge":
            raise ManualActionRequired(
                str(payload.get("reason", "Ручная проверка Avito не завершена"))
            )
        if status == "manual_timeout":
            raise ManualActionRequired(
                str(
                    payload.get(
                        "reason",
                        "Ручная проверка Avito не завершена за безопасный срок",
                    )
                )
            )
        if status == "cancelled":
            self._notify_captcha_stopped(canonical_url)
            raise OperatorStopRequested(
                str(payload.get("reason", "Ожидание Chrome остановлено оператором"))
            )
        if status in {"phone_error", "phone_missing"}:
            raise PhoneNotFoundError(str(payload.get("reason", "Avito не показал временный номер")))
        raise BrowserOperationError(
            str(payload.get("reason", f"Неизвестный ответ расширения: {status}"))
        )

    @staticmethod
    def _log_readiness_diagnostics(payload: dict[str, Any]) -> None:
        diagnostics = payload.get("diagnostics")
        if not isinstance(diagnostics, dict):
            return
        allowed = {
            key: diagnostics[key]
            for key in (
                "navigationAttempt",
                "state",
                "expectedId",
                "actualId",
                "tabStatus",
                "readyState",
                "probeConnected",
                "elapsedMs",
            )
            if key in diagnostics
        }
        if allowed:
            LOGGER.warning("Chrome readiness diagnostics: %s", allowed)

    def _handle_status(self, event: ExtensionEvent, url: str) -> None:
        if event.status == "manual_required" and not self._manual_notified:
            self._manual_notified = True
            self._manual_pending = True
            LOGGER.warning("Обычный Chrome ждёт ручного решения капчи; страница не перезагружается")
            self._report_operation_status(
                "Ожидается ручная проверка Avito в обычном Chrome."
            )
            self._notify_safely(
                "send_captcha_detected",
                reason=str(event.payload.get("reason", "ручную проверку")),
                url=url,
                wait_seconds=0,
            )
        elif event.status == "manual_cleared":
            if self._manual_pending:
                self.captchas_solved += 1
                self._manual_pending = False
            LOGGER.info("Ручная проверка в обычном Chrome завершена; продолжаем текущую строку")
            self._report_operation_status(
                "Ручная проверка Avito завершена; продолжаем текущую строку."
            )
        elif event.status == "manual_reminder" and self._manual_pending:
            escalate = bool(event.payload.get("escalate", False))
            self._manual_backup_alerted = self._manual_backup_alerted or escalate
            self._notify_safely(
                "send_captcha_reminder",
                reason=str(event.payload.get("reason", "ручную проверку")),
                url=url,
                elapsed_seconds=float(event.payload.get("elapsed_seconds", 0.0)),
                escalate=escalate,
            )
        elif event.status == "clicking":
            LOGGER.info("Обычный Chrome: кнопка показа телефона найдена")
        elif event.status == "clicked":
            LOGGER.info("Обычный Chrome: команда клика отправлена")
        elif event.status == "retrying_after_click_no_effect":
            LOGGER.warning(
                "Обычный Chrome: кнопка не раскрылась; вкладка перезагружается для "
                "последней попытки"
            )

    def _report_operation_status(self, message: str) -> None:
        if self.operation_status_callback is not None:
            self.operation_status_callback(message)

    def _notify_safely(self, method: str, **kwargs: object) -> None:
        if not self.notifier.enabled:
            return
        try:
            getattr(self.notifier, method)(**kwargs)
        except NotificationError as exc:
            LOGGER.warning("Уведомление не доставлено: %s", exc)
        except Exception as exc:
            LOGGER.warning("Уведомление не доставлено (%s)", exc.__class__.__name__)

    def _notify_captcha_stopped(self, url: str) -> None:
        if not self._manual_pending:
            return
        self._notify_safely(
            "send_captcha_stopped",
            url=url,
            include_backup=self._manual_backup_alerted,
        )
        self._manual_pending = False

    def _read_tab_capture(self, png: bytes, capture: dict[str, Any]) -> PhoneResult:
        if _is_nearly_blank_capture(png, viewport=True):
            raise PageNotReadyError(
                "Вкладка Avito ещё не отрисовала содержимое; строка будет повторена "
                "без расходования попытки открытия номера"
            )

        regions, diagnostics = _tab_capture_regions(png, capture)
        diagnostics["png_sha256"] = hashlib.sha256(png).hexdigest()[:12]
        LOGGER.info("Chrome tab-capture diagnostics: %s", diagnostics)

        errors: list[str] = []
        for label, region_png, psm in regions:
            try:
                result = self.ocr.read_png(region_png, psm=psm)
            except PhoneNotFoundError as exc:
                errors.append(f"{label}: {exc}")
                continue
            result.source = f"ocr-tab-{label}"
            return result

        # Preserve the proven inline/modal viewport crops as an in-memory-only
        # fallback. The full tab image and the phone crop are never persisted or
        # included in logs.
        try:
            result = self.ocr.read_viewport_png(png)
        except PhoneNotFoundError as exc:
            errors.append(f"viewport: {exc}")
        else:
            result.source = "ocr-tab-viewport"
            return result

        if self.save_failed_captures:
            self._save_failed_tab_capture(png, diagnostics)
        detail = errors[-1] if errors else "номер не попал в проверяемую область вкладки"
        raise PhoneNotFoundError(f"OCR не распознал номер в снимке вкладки Chrome: {detail}")

    def _save_failed_tab_capture(self, png: bytes, diagnostics: dict[str, Any]) -> None:
        digest = hashlib.sha256(png).hexdigest()
        captured_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        stem = f"ocr-failed-{stamp}-{digest[:12]}-{uuid.uuid4().hex[:8]}"
        directory = self.settings.output_dir / "ocr-failed-captures"
        png_path = directory / f"{stem}.png"
        metadata_path = directory / f"{stem}.json"
        png_temp = directory / f".{stem}.png.tmp"
        metadata_temp = directory / f".{stem}.json.tmp"
        metadata = {
            "schema": 1,
            "reason": "ocr_failed",
            "captured_at_utc": captured_at,
            "contains_visible_page_data": True,
            "png_file": png_path.name,
            "png_sha256": digest,
            "capture": diagnostics,
        }
        try:
            directory.mkdir(parents=True, exist_ok=True)
            png_temp.write_bytes(png)
            metadata_temp.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            png_temp.replace(png_path)
            metadata_temp.replace(metadata_path)
        except Exception as exc:
            for temporary in (png_temp, metadata_temp):
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)
            LOGGER.warning(
                "Не удалось сохранить диагностический OCR-снимок (%s)",
                exc.__class__.__name__,
            )
            return
        LOGGER.warning(
            "Диагностический снимок OCR-ошибки сохранён локально: PNG=%s; metadata=%s",
            png_path,
            metadata_path,
        )


def open_ordinary_chrome() -> None:
    """Open Avito in installed Chrome without automation or a separate profile."""
    if os.name == "nt":
        _start_windows_chrome("https://www.avito.ru/")
        return
    if not webbrowser.open("https://www.avito.ru/", new=1, autoraise=True):
        raise BrowserOperationError("Не удалось открыть обычный Google Chrome")


def _start_windows_chrome(url: str | None = None) -> None:
    candidates = tuple(
        Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
        for base in (
            os.environ.get("PROGRAMFILES", ""),
            os.environ.get("PROGRAMFILES(X86)", ""),
            os.environ.get("LOCALAPPDATA", ""),
        )
        if base
    )
    chrome = next((candidate for candidate in candidates if candidate.is_file()), None)
    if chrome is None:
        raise BrowserOperationError("Обычный Google Chrome не найден")
    command = [str(chrome)]
    if url:
        command.append(url)
    subprocess.Popen(command, close_fds=True)


def _decode_screenshot(data_url: str) -> bytes:
    prefix = "data:image/png;base64,"
    if not data_url.startswith(prefix):
        raise BrowserOperationError("Расширение вернуло скриншот в неизвестном формате")
    try:
        png = base64.b64decode(data_url[len(prefix) :], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise BrowserOperationError("Расширение вернуло повреждённый скриншот") from exc
    if len(png) > MAX_TAB_CAPTURE_PNG_BYTES:
        raise BrowserOperationError("Расширение вернуло слишком большой снимок вкладки")
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise BrowserOperationError("Расширение вернуло не PNG-изображение")
    return png


def _tab_capture_regions(
    png: bytes,
    capture: dict[str, Any],
) -> tuple[list[tuple[str, bytes, int]], dict[str, Any]]:
    try:
        image = Image.open(io.BytesIO(png)).convert("RGB")
    except Exception as exc:
        raise BrowserOperationError(f"Не удалось открыть снимок вкладки Chrome: {exc}") from exc

    if (
        image.width < 200
        or image.height < 200
        or image.width * image.height > MAX_TAB_CAPTURE_PIXELS
    ):
        raise BrowserOperationError("Расширение вернуло снимок вкладки недопустимого размера")

    try:
        if int(capture["schemaVersion"]) != 1:
            raise ValueError("unsupported schema")
        viewport = capture["viewport"]
        region = capture["region"]
        if not isinstance(viewport, dict) or not isinstance(region, dict):
            raise TypeError("metadata objects required")
        css_width = _finite_float(viewport["cssWidth"])
        css_height = _finite_float(viewport["cssHeight"])
        declared_dpr = _finite_float(viewport["devicePixelRatio"])
        left = _finite_float(region["left"])
        top = _finite_float(region["top"])
        width = _finite_float(region["width"])
        height = _finite_float(region["height"])
        kind = str(region.get("kind", "control"))
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise BrowserOperationError("Расширение вернуло некорректные координаты вкладки") from exc

    if kind not in {"control", "dialog", "panel"}:
        kind = "control"
    if not (
        200 <= css_width <= 10000
        and 200 <= css_height <= 10000
        and 0.5 <= declared_dpr <= 8
        and width > 10
        and height > 10
        and width <= css_width * 1.1
        and height <= css_height * 1.1
        and left < css_width
        and top < css_height
        and left + width > 0
        and top + height > 0
    ):
        raise BrowserOperationError("Расширение вернуло область вне видимой вкладки")

    scale_x = image.width / css_width
    scale_y = image.height / css_height
    if not (0.5 <= scale_x <= 8 and 0.5 <= scale_y <= 8):
        raise BrowserOperationError("Масштаб снимка вкладки вышел за безопасные пределы")
    if abs(scale_x - scale_y) / max(scale_x, scale_y) > 0.20:
        raise BrowserOperationError("Снимок вкладки имеет несовместимый масштаб по осям")

    def crop_variant(
        label: str,
        pad_x_ratio: float,
        pad_y_ratio: float,
        psm: int,
    ) -> tuple[str, bytes, int]:
        pad_x = max(8.0, width * pad_x_ratio)
        pad_y = max(6.0, height * pad_y_ratio)
        box = (
            max(0, round((left - pad_x) * scale_x)),
            max(0, round((top - pad_y) * scale_y)),
            min(image.width, round((left + width + pad_x) * scale_x)),
            min(image.height, round((top + height + pad_y) * scale_y)),
        )
        if box[2] - box[0] <= 20 or box[3] - box[1] <= 20:
            raise BrowserOperationError("Область номера слишком мала после масштабирования")
        buffer = io.BytesIO()
        image.crop(box).save(buffer, format="PNG")
        return label, buffer.getvalue(), psm

    if kind == "control":
        regions = [
            crop_variant("control", 0.12, 0.35, 7),
            crop_variant("control-context", 0.45, 1.25, 6),
        ]
    else:
        regions = [crop_variant(kind, 0.04, 0.06, 6)]

    diagnostics = {
        "schema": 1,
        "image_px": f"{image.width}x{image.height}",
        "viewport_css": f"{round(css_width)}x{round(css_height)}",
        "declared_dpr": round(declared_dpr, 3),
        "effective_scale": f"{scale_x:.3f}x{scale_y:.3f}",
        "region_kind": kind,
        "region_origin_css": f"{round(left)},{round(top)}",
        "region_css": f"{round(width)}x{round(height)}",
        "capture_fallback": _safe_capture_fallback(capture.get("fallback")),
    }
    return regions, diagnostics


def _safe_capture_fallback(value: object) -> str:
    fallback = str(value or "").strip()
    if not fallback:
        return "none"
    if fallback in {"invalid_region", "offscreen_region"}:
        return fallback
    return "unknown"


def _finite_float(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    parsed = float(value)
    if not (-1e9 < parsed < 1e9):
        raise ValueError("number is not finite")
    return parsed


def _is_nearly_blank_capture(png: bytes, *, viewport: bool = False) -> bool:
    """Return True only for a virtually empty, light browser surface."""
    try:
        from PIL import Image, ImageStat

        image = Image.open(io.BytesIO(png)).convert("L")
        if viewport and image.width > 100 and image.height > 100:
            # Ignore Chrome chrome and the Windows taskbar. The failed captures
            # contain those controls but an entirely blank page viewport.
            image = image.crop(
                (
                    round(image.width * 0.03),
                    round(image.height * 0.12),
                    round(image.width * 0.97),
                    round(image.height * 0.92),
                )
            )
        image.thumbnail((320, 180))
        stats = ImageStat.Stat(image)
        return stats.mean[0] >= 246 and stats.stddev[0] <= 5
    except Exception:
        # Screenshot validation must never mask the normal OCR error path.
        return False
