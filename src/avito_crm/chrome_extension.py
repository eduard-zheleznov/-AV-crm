from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
import uuid
import webbrowser
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from avito_crm.config import Settings
from avito_crm.errors import (
    BrowserInfrastructureError,
    BrowserOperationError,
    ClickNotEffectiveError,
    InactiveListingError,
    ListingNavigationError,
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
EXPECTED_EXTENSION_VERSION = "1.0.13"


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
        self.extension_instance = ""
        self.stopped = False


class ExtensionBridge:
    """Authenticated localhost long-poll bridge for the ordinary Chrome extension."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state = _BridgeState()
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self._diagnostic_lock = threading.Lock()
        self._diagnostic_path = settings.logs_dir / "chrome-extension-health.jsonl"

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

    def health_snapshot(self) -> dict[str, Any]:
        with self.state.condition:
            idle_seconds = max(0.0, time.monotonic() - self.state.last_seen)
            return {
                "connected": idle_seconds <= 5,
                "idle_seconds": round(idle_seconds, 3),
                "extension_version": self.state.extension_version,
                "extension_instance": self.state.extension_instance,
                "command_active": self.state.command is not None,
                "command_dispatched": self.state.dispatched,
            }

    def health_probe(self) -> ExtensionEvent:
        return self._execute(
            command_type="health_probe",
            url="https://www.avito.ru/",
            row_id="startup-canary",
            max_clicks=0,
        )

    def execute(
        self,
        *,
        url: str,
        row_id: str,
        max_clicks: int,
        status_callback: Callable[[ExtensionEvent], None] | None = None,
    ) -> ExtensionEvent:
        return self._execute(
            command_type="reveal_phone",
            url=url,
            row_id=row_id,
            max_clicks=max_clicks,
            status_callback=status_callback,
        )

    def _execute(
        self,
        *,
        command_type: str,
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
            "type": command_type,
            "url": url,
            "rowId": row_id,
            "maxClicks": max_clicks,
            "pageTimeoutMs": round(self.settings.avito_page_timeout * 1000),
            # Ordinary Chrome waits until the operator solves the challenge or
            # presses STOP. A clock timeout would strand the queue row.
            "manualTimeoutMs": 0,
            "phoneWaitMs": round(self.settings.avito_temp_number_wait_max * 1000),
            "retryDelayMs": round(self.settings.avito_phone_retry_max * 1000),
            "recoveryBackoffMs": round(self.settings.avito_extension_recovery_backoff * 1000),
        }
        connection_deadline = time.monotonic() + self.settings.avito_extension_connect_timeout
        with self.state.condition:
            while time.monotonic() - self.state.last_seen > 5:
                remaining = connection_deadline - time.monotonic()
                if remaining <= 0:
                    raise BrowserInfrastructureError(
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
        self._write_diagnostic(
            "command_created",
            command_id=command_id,
            command_type=command_type,
            row_id=row_id,
            url=url,
        )

        operation_timeout = max(
            30.0,
            (self.settings.avito_page_timeout * 2)
            + self.settings.avito_extension_recovery_backoff
            + (max_clicks * (self.settings.avito_page_timeout + 5.0))
            + (
                max(0, max_clicks - 1)
                * (self.settings.avito_phone_retry_max + self.settings.avito_page_timeout)
            )
            + 30.0,
        )
        deadline: float | None = time.monotonic() + operation_timeout
        stop_seen_at: float | None = None
        manual_started_at: float | None = None
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
                            self._write_diagnostic(
                                "command_timeout",
                                command_id=command_id,
                                command_type=command_type,
                                row_id=row_id,
                                url=url,
                            )
                            raise BrowserInfrastructureError(
                                "Обычный Chrome не завершил команду: нет ответа расширения"
                            )
                        else:
                            self.state.condition.wait(timeout=1.0)
                            continue
                if event is not None:
                    if event.status == "manual_required":
                        manual_started_at = time.monotonic()
                        manual_reason = str(event.payload.get("reason", manual_reason))
                        reminder_index = 0
                        deadline = None
                    elif event.status == "manual_cleared":
                        manual_started_at = None
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
                    self.state.condition.notify_all()

    def _write_diagnostic(self, event: str, **values: object) -> None:
        record: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
        }
        for key in (
            "command_id",
            "command_type",
            "row_id",
            "status",
            "extension_version",
            "extension_instance",
        ):
            value = values.get(key)
            if value not in {None, ""}:
                record[key] = value
        url = str(values.get("url", "") or "")
        listing_id = _listing_id_for_diagnostic(url) if url else ""
        if listing_id:
            record["listing_id"] = listing_id
        diagnostics = values.get("diagnostics")
        if isinstance(diagnostics, dict):
            record["diagnostics"] = _safe_extension_diagnostics(diagnostics)
        try:
            self._diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            with self._diagnostic_lock, self._diagnostic_path.open("a", encoding="utf-8") as stream:
                stream.write(line)
        except OSError as exc:
            LOGGER.warning("Не удалось записать журнал Chrome health: %s", exc)

    def _poll(
        self,
        timeout: float = 20.0,
        *,
        extension_version: str = "",
        extension_instance: str = "",
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        with self.state.condition:
            session_changed = bool(
                extension_instance and extension_instance.strip() != self.state.extension_instance
            )
            self.state.last_seen = time.monotonic()
            self.state.extension_version = extension_version.strip()
            self.state.extension_instance = extension_instance.strip()
            self.state.condition.notify_all()
            if session_changed:
                self._write_diagnostic(
                    "extension_connected",
                    extension_version=self.state.extension_version,
                    extension_instance=self.state.extension_instance,
                )
            while not self.state.stopped:
                # The long-poll request itself is an idle heartbeat. Refreshing
                # it here distinguishes an attached service worker from an old
                # timestamp even while there is no command to dispatch.
                self.state.last_seen = time.monotonic()
                if self.state.command is not None and not self.state.dispatched:
                    self.state.dispatched = True
                    self._write_diagnostic(
                        "command_dispatched",
                        command_id=str(self.state.command.get("id", "")),
                        command_type=str(self.state.command.get("type", "")),
                        row_id=str(self.state.command.get("rowId", "")),
                        url=str(self.state.command.get("url", "")),
                        extension_version=self.state.extension_version,
                        extension_instance=self.state.extension_instance,
                    )
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
        command_type = ""
        row_id = ""
        url = ""
        extension_version = ""
        extension_instance = ""
        with self.state.condition:
            self.state.last_seen = time.monotonic()
            if not self.state.command or command_id != self.state.command.get("id"):
                raise ValueError("Событие не относится к активной команде")
            command_type = str(self.state.command.get("type", ""))
            row_id = str(self.state.command.get("rowId", ""))
            url = str(self.state.command.get("url", ""))
            extension_version = self.state.extension_version
            extension_instance = self.state.extension_instance
            if event_type == "result":
                self.state.result = event
            else:
                self.state.events.append(event)
            self.state.condition.notify_all()
        self._write_diagnostic(
            f"extension_{event_type}",
            command_id=command_id,
            command_type=command_type,
            row_id=row_id,
            url=url,
            status=status,
            extension_version=extension_version,
            extension_instance=extension_instance,
            diagnostics=payload.get("diagnostics"),
        )

    def _authorized(self, header: str | None) -> bool:
        expected = f"Bearer {self.settings.avito_extension_token}"
        return bool(header) and secrets.compare_digest(header, expected)

    def _command_status(self, command_id: str) -> str:
        with self.state.condition:
            active_id = str((self.state.command or {}).get("id", ""))
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
                    "Authorization, Content-Type, X-Avito-CRM-Extension-Version, "
                    "X-Avito-CRM-Extension-Instance",
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
                    self._send_json(HTTPStatus.OK, bridge.health_snapshot())
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
                    ),
                    extension_instance=self.headers.get(
                        "X-Avito-CRM-Extension-Instance",
                        "",
                    ),
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
    ) -> None:
        self.settings = settings
        self.ocr = ocr
        self.notifier = notifier or NotificationRouter(settings)
        self._owns_notifier = notifier is None
        self.bridge = ExtensionBridge(settings)
        self._manual_notified = False
        self._manual_pending = False
        self._manual_backup_alerted = False
        self._needs_active_probe = True
        self._last_probe_at = 0.0
        self._last_extension_instance = ""
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

    def preflight(self, *, force: bool = False) -> dict[str, Any]:
        """Prove that Chrome can render Avito without clicking or consuming a row."""
        snapshot = self.bridge.health_snapshot()
        instance = str(snapshot.get("extension_instance", ""))
        probe_age = max(0.0, time.monotonic() - self._last_probe_at)
        active_probe_required = (
            force
            or self._needs_active_probe
            or not snapshot.get("connected")
            or snapshot.get("extension_version") != EXPECTED_EXTENSION_VERSION
            or not instance
            or instance != self._last_extension_instance
            or probe_age > self.settings.avito_extension_health_ttl
        )
        if not active_probe_required:
            return snapshot

        event = self.bridge.health_probe()
        if event.status == "manual_required":
            self._needs_active_probe = True
            raise ManualActionRequired(
                str(event.payload.get("reason", "Avito требует ручной проверки"))
            )
        if event.status != "healthy":
            self._needs_active_probe = True
            raise BrowserInfrastructureError(
                str(
                    event.payload.get(
                        "reason",
                        "Предстартовая проверка Chrome/расширения не пройдена",
                    )
                )
            )

        snapshot = self.bridge.health_snapshot()
        self._last_probe_at = time.monotonic()
        self._last_extension_instance = str(snapshot.get("extension_instance", ""))
        self._needs_active_probe = False
        return snapshot

    def reveal_phone(self, url: str, row_id: str = "", *, max_clicks: int = 2) -> PhoneResult:
        if max_clicks not in {1, 2}:
            raise ValueError("max_clicks должен быть равен 1 или 2")
        canonical_url = canonical_avito_url(url)
        self.preflight()
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
            self._needs_active_probe = True
            raise
        status = event.status
        payload = event.payload
        if status == "phone":
            phone = normalize_phone(str(payload.get("phone", "")))
            if not phone:
                raise PhoneNotFoundError("Расширение вернуло некорректный номер")
            return PhoneResult(phone, str(payload.get("source", "chrome-extension-dom")))
        if status in {"screenshot", "screen_capture"}:
            if status == "screen_capture" and not isinstance(payload.get("crop"), dict):
                raise PhoneNotFoundError(
                    "Chrome открыл номер, но не смог безопасно определить его область; "
                    "широкий снимок намеренно не распознаётся"
                )
            if status == "screen_capture":
                return self._read_screen_capture(
                    payload["crop"],
                    canonical_url,
                )
            png = _decode_screenshot(str(payload.get("screenshot", "")))
            artifact = self._artifact_path(canonical_url, "extension-viewport")
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_bytes(png)
            return self.ocr.read_viewport_png(png)
        if status in {"browser_infra", "page_not_ready", "stale_content_script"}:
            self._needs_active_probe = True
            raise BrowserInfrastructureError(
                str(
                    payload.get(
                        "reason",
                        "Страница объявления не успела полностью отобразиться",
                    )
                )
            )
        if status == "listing_mismatch":
            raise ListingNavigationError(
                str(payload.get("reason", "Avito открыл другое объявление"))
            )
        if status == "click_not_effective":
            raise ClickNotEffectiveError(
                str(payload.get("reason", "Avito не подтвердил раскрытие номера"))
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
            # Current extension waits until the operator solves the challenge or
            # presses STOP. A timeout can only come from a stale content script;
            # never turn it into a false CAPTCHA state that blocks the whole run.
            raise PhoneNotFoundError(
                "Устаревший сценарий Chrome завершил ожидание; строка возвращена "
                "в очередь без статуса капчи"
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

    def _handle_status(self, event: ExtensionEvent, url: str) -> None:
        if event.status == "manual_required" and not self._manual_notified:
            self._manual_notified = True
            self._manual_pending = True
            LOGGER.warning("Обычный Chrome ждёт ручного решения капчи; страница не перезагружается")
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
        elif event.status == "click_dispatched":
            LOGGER.info("Обычный Chrome: browser-level клик отправлен; ожидаем изменение DOM")
        elif event.status == "click_recovery":
            LOGGER.warning("Обычный Chrome: первый клик не подтверждён; один повтор")
        elif event.status == "reveal_confirmed":
            LOGGER.info("Обычный Chrome: раскрытие номера подтверждено DOM")

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

    def _artifact_path(self, url: str, suffix: str) -> Path:
        digest = hashlib.sha256(url.encode()).hexdigest()[:12]
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return self.settings.screenshot_dir / f"{stamp}-{digest}-{suffix}.png"

    def _read_screen_capture(self, crop: dict[str, Any], url: str) -> PhoneResult:
        recognized: dict[str, tuple[int, PhoneResult]] = {}
        errors: list[str] = []
        blank_full_frames = 0

        def confirm(result: PhoneResult, source: str) -> PhoneResult | None:
            count, _previous = recognized.get(result.phone, (0, result))
            recognized[result.phone] = (count + 1, result)
            if count + 1 < 2:
                return None
            result.source = source
            return result

        for attempt in range(1, 4):
            if attempt > 1:
                time.sleep(0.8)

            # The extension knows the exact on-screen rectangle where Avito
            # replaced the button with the image-rendered phone. OCR that small
            # area first: it is both faster and substantially more reliable than
            # searching the whole desktop.
            region_png = _capture_interactive_desktop_png(crop)
            region_artifact = self._artifact_path(url, f"extension-phone-{attempt}")
            region_artifact.parent.mkdir(parents=True, exist_ok=True)
            region_artifact.write_bytes(region_png)
            if not _is_nearly_blank_capture(region_png):
                try:
                    region_result = self.ocr.read_png(
                        region_png,
                        artifact_path=region_artifact,
                        psm=7,
                    )
                except PhoneNotFoundError as exc:
                    errors.append(str(exc))
                else:
                    confirmed = confirm(
                        region_result,
                        "ocr-confirmed-avito-region",
                    )
                    if confirmed is not None:
                        return confirmed
                    # Confirm the same phone from a fresh frame before accepting
                    # it. Do not dilute a successful exact-region result with a
                    # broad desktop scan.
                    continue

            png = _capture_interactive_desktop_png()
            artifact = self._artifact_path(url, f"extension-screen-{attempt}")
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_bytes(png)
            if _is_nearly_blank_capture(png, viewport=True):
                blank_full_frames += 1
                errors.append("страница объявления ещё не отобразилась")
                continue
            try:
                result = self.ocr.read_avito_screen_png(png)
            except PhoneNotFoundError as exc:
                errors.append(str(exc))
                continue
            confirmed = confirm(result, "ocr-confirmed-avito-screen")
            if confirmed is not None:
                return confirmed
        if recognized:
            raise PhoneNotFoundError(
                "OCR увидел номер только на одном из трёх снимков; "
                "результат отклонён как неподтверждённый"
            )
        if blank_full_frames >= 2:
            raise PageNotReadyError(
                "Страница объявления не успела отобразиться в обычном Chrome; "
                "строка будет повторена без расходования попытки открытия номера"
            )
        detail = errors[-1] if errors else "номер не попал в проверенные области"
        raise PhoneNotFoundError(f"OCR не распознал номер на трёх снимках экрана: {detail}")


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
    except ValueError as exc:
        raise BrowserOperationError("Расширение вернуло повреждённый скриншот") from exc
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise BrowserOperationError("Расширение вернуло не PNG-изображение")
    return png


def _capture_interactive_desktop_png(crop: object = None) -> bytes:
    if os.name != "nt":
        raise BrowserOperationError("Резервный снимок экрана доступен только в Windows")
    try:
        from PIL import ImageGrab

        image = ImageGrab.grab(all_screens=True).convert("RGB")
        if isinstance(crop, dict):
            try:
                screen_width = float(crop["screenWidth"])
                screen_height = float(crop["screenHeight"])
                left = float(crop["left"])
                top = float(crop["top"])
                width = float(crop["width"])
                height = float(crop["height"])
            except (KeyError, TypeError, ValueError):
                screen_width = screen_height = width = height = 0
                left = top = 0
            if screen_width > 0 and screen_height > 0 and width > 10 and height > 10:
                scale_x = image.width / screen_width
                scale_y = image.height / screen_height
                pad_x = max(8.0, width * 0.06)
                pad_y = max(6.0, height * 0.12)
                box = (
                    max(0, round((left - pad_x) * scale_x)),
                    max(0, round((top - pad_y) * scale_y)),
                    min(image.width, round((left + width + pad_x) * scale_x)),
                    min(image.height, round((top + height + pad_y) * scale_y)),
                )
                if box[2] - box[0] > 20 and box[3] - box[1] > 20:
                    image = image.crop(box)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception as exc:
        raise BrowserOperationError(f"Не удалось сделать снимок экрана Windows: {exc}") from exc


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


_DIAGNOSTIC_KEYS = frozenset(
    {
        "actualId",
        "actualListingId",
        "actualSurface",
        "attempts",
        "auth",
        "bodyLength",
        "classification",
        "content",
        "contentInjectionAttempted",
        "contentInjectionStatus",
        "contentVersion",
        "discarded",
        "elapsedMs",
        "expectedId",
        "expectedListingId",
        "inactive",
        "manual",
        "mode",
        "navigation",
        "probe",
        "probeError",
        "pendingSurface",
        "readyState",
        "reason",
        "recovered",
        "rendered",
        "stage",
        "status",
        "tabId",
        "tabStatus",
        "visibleHeadings",
    }
)

_SURFACE_CLASSES = frozenset(
    {
        "empty",
        "about:blank",
        "avito:manual",
        "avito:listing",
        "avito:root",
        "avito:path",
        "http:other",
        "https:other",
        "chrome:other",
        "chrome-extension:other",
        "other",
        "invalid",
    }
)
_CONTENT_INJECTION_STATUSES = frozenset(
    {
        "not_needed",
        "stale_content_reload",
        "injected",
        "tab_lookup_failed",
        "skipped_tab_unavailable",
        "skipped_navigation_pending",
        "skipped_document_unavailable",
        "skipped_unexpected_surface",
        "execute_failed",
    }
)


def _listing_id_for_diagnostic(url: str) -> str:
    """Return only the public numeric Avito id; never persist a full URL."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return ""
    match = re.search(r"_(\d+)(?:/)?$", path)
    return match.group(1) if match else ""


def _safe_extension_diagnostics(
    diagnostics: dict[str, Any],
    *,
    _depth: int = 0,
) -> dict[str, Any]:
    """Bound and allowlist extension telemetry before writing it to disk."""
    if _depth >= 4:
        return {}
    safe: dict[str, Any] = {}
    for key, value in diagnostics.items():
        if key not in _DIAGNOSTIC_KEYS:
            continue
        if isinstance(value, dict):
            safe[key] = _safe_extension_diagnostics(value, _depth=_depth + 1)
        elif isinstance(value, list):
            safe[key] = [
                _safe_extension_diagnostics(item, _depth=_depth + 1)
                for item in value[:3]
                if isinstance(item, dict)
            ]
        elif isinstance(value, str):
            if key in {"actualSurface", "pendingSurface"}:
                safe[key] = value if value in _SURFACE_CLASSES else "invalid"
            elif key == "contentInjectionStatus":
                safe[key] = value if value in _CONTENT_INJECTION_STATUSES else "unknown"
            else:
                safe[key] = value[:300]
        elif isinstance(value, (bool, int, float)) or value is None:
            safe[key] = value
    return safe
