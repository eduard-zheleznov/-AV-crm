from __future__ import annotations

import base64
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
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from avito_crm.config import Settings
from avito_crm.errors import (
    BrowserOperationError,
    InactiveListingError,
    ManualActionRequired,
    NotificationError,
    PhoneButtonUnavailableError,
    PhoneNotFoundError,
)
from avito_crm.models import PhoneResult
from avito_crm.notifications import NotificationRouter
from avito_crm.ocr import PhoneOcr
from avito_crm.phone import canonical_avito_url, normalize_phone

LOGGER = logging.getLogger(__name__)
MAX_EVENT_BYTES = 12 * 1024 * 1024


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
            "manualTimeoutMs": round(self.settings.avito_manual_timeout * 1000),
            "phoneWaitMs": round(self.settings.avito_temp_number_wait_max * 1000),
            "retryDelayMs": round(self.settings.avito_phone_retry_max * 1000),
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
            if self.state.command is not None:
                raise BrowserOperationError("Локальный мост Chrome уже выполняет другую команду")
            self.state.command = command
            self.state.dispatched = False
            self.state.events.clear()
            self.state.result = None
            self.state.condition.notify_all()

        deadline = (
            time.monotonic()
            + self.settings.avito_page_timeout
            + self.settings.avito_manual_timeout
            + 60
        )
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
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ManualActionRequired(
                                "Обычный Chrome не завершил открытие номера за отведённое время"
                            )
                        self.state.condition.wait(timeout=min(remaining, 1.0))
                        continue
                if event is not None and status_callback is not None:
                    status_callback(event)
        finally:
            with self.state.condition:
                if self.state.command and self.state.command.get("id") == command_id:
                    self.state.command = None
                    self.state.dispatched = False
                    self.state.events.clear()
                    self.state.result = None
                    self.state.condition.notify_all()

    def _poll(self, timeout: float = 20.0) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        with self.state.condition:
            self.state.last_seen = time.monotonic()
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
                self.state.result = event
            else:
                self.state.events.append(event)
            self.state.condition.notify_all()

    def _authorized(self, header: str | None) -> bool:
        expected = f"Bearer {self.settings.avito_extension_token}"
        return bool(header) and secrets.compare_digest(header, expected)

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "AvitoChromeBridge/1"

            def do_OPTIONS(self) -> None:  # noqa: N802
                self.send_response(HTTPStatus.NO_CONTENT)
                self._cors_headers()
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                if not bridge._authorized(self.headers.get("Authorization")):
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                path = urlsplit(self.path).path
                if path == "/v1/health":
                    self._send_json(HTTPStatus.OK, {"ok": True})
                    return
                if path != "/v1/poll":
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                command = bridge._poll()
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
        event = self.bridge.execute(
            url=canonical_url,
            row_id=row_id,
            max_clicks=max_clicks,
            status_callback=lambda status: self._handle_status(status, canonical_url),
        )
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
        if status == "inactive":
            raise InactiveListingError(str(payload.get("reason", "объявление недоступно")))
        if status == "button_missing":
            raise PhoneButtonUnavailableError(
                str(payload.get("reason", "Кнопка показа телефона не найдена"))
            )
        if status in {"manual_timeout", "challenge"}:
            raise ManualActionRequired(
                str(payload.get("reason", "Ручная проверка Avito не завершена"))
            )
        if status in {"phone_error", "phone_missing"}:
            raise PhoneNotFoundError(str(payload.get("reason", "Avito не показал временный номер")))
        raise BrowserOperationError(
            str(payload.get("reason", f"Неизвестный ответ расширения: {status}"))
        )

    def _handle_status(self, event: ExtensionEvent, url: str) -> None:
        if event.status == "manual_required" and not self._manual_notified:
            self._manual_notified = True
            LOGGER.warning("Обычный Chrome ждёт ручного решения капчи; страница не перезагружается")
            self._notify_safely(
                "send_captcha_detected",
                reason=str(event.payload.get("reason", "ручную проверку")),
                url=url,
                wait_seconds=self.settings.avito_manual_timeout,
            )
        elif event.status == "manual_cleared":
            LOGGER.info("Ручная проверка в обычном Chrome завершена; продолжаем текущую строку")
        elif event.status == "clicking":
            LOGGER.info("Обычный Chrome: кнопка показа телефона найдена")
        elif event.status == "clicked":
            LOGGER.info("Обычный Chrome: команда клика отправлена")

    def _notify_safely(self, method: str, **kwargs: object) -> None:
        if not self.notifier.enabled:
            return
        try:
            getattr(self.notifier, method)(**kwargs)
        except NotificationError as exc:
            LOGGER.warning("Уведомление не доставлено: %s", exc)
        except Exception as exc:
            LOGGER.warning("Уведомление не доставлено (%s)", exc.__class__.__name__)

    def _artifact_path(self, url: str, suffix: str) -> Path:
        digest = hashlib.sha256(url.encode()).hexdigest()[:12]
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return self.settings.screenshot_dir / f"{stamp}-{digest}-{suffix}.png"

    def _read_screen_capture(self, _crop: dict[str, Any], url: str) -> PhoneResult:
        recognized: dict[str, tuple[int, PhoneResult]] = {}
        errors: list[str] = []
        for attempt in range(1, 4):
            if attempt > 1:
                time.sleep(0.8)
            png = _capture_interactive_desktop_png()
            artifact = self._artifact_path(url, f"extension-screen-{attempt}")
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_bytes(png)
            try:
                result = self.ocr.read_avito_screen_png(png)
            except PhoneNotFoundError as exc:
                errors.append(str(exc))
                continue
            count, _previous = recognized.get(result.phone, (0, result))
            recognized[result.phone] = (count + 1, result)
            if count + 1 >= 2:
                result.source = "ocr-confirmed-avito-screen"
                return result
        if recognized:
            raise PhoneNotFoundError(
                "OCR увидел номер только на одном из трёх снимков; "
                "результат отклонён как неподтверждённый"
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
