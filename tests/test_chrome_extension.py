from __future__ import annotations

import base64
import json
import socket
import threading
import urllib.error
import urllib.request
from dataclasses import replace

import pytest

import avito_crm.chrome_extension as chrome_extension_module
from avito_crm.chrome_extension import ChromeExtensionBrowser, ExtensionBridge, ExtensionEvent
from avito_crm.errors import OperatorStopRequested
from avito_crm.models import PhoneResult


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _request(
    port: int,
    token: str,
    path: str,
    *,
    payload: dict[str, object] | None = None,
) -> tuple[int, dict[str, object] | None]:
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        method="POST" if payload is not None else "GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        raw = response.read()
        return response.status, json.loads(raw) if raw else None


def test_bridge_requires_the_per_install_token(settings):
    port = _free_port()
    configured = replace(
        settings,
        avito_extension_port=port,
        avito_extension_token="a" * 64,
    )
    bridge = ExtensionBridge(configured)
    bridge.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _request(port, "wrong-token", "/v1/health")
        assert exc_info.value.code == 401
    finally:
        bridge.close()


def test_bridge_delivers_one_command_and_correlates_the_result(settings):
    port = _free_port()
    token = "b" * 64
    configured = replace(
        settings,
        avito_extension_port=port,
        avito_extension_token=token,
        avito_extension_connect_timeout=2,
        avito_page_timeout=2,
        avito_manual_timeout=2,
    )
    bridge = ExtensionBridge(configured)
    bridge.start()
    failures: list[BaseException] = []

    def extension() -> None:
        try:
            status, command = _request(port, token, "/v1/poll")
            assert status == 200
            assert command is not None
            assert command["type"] == "reveal_phone"
            assert command["manualTimeoutMs"] == 0
            _request(
                port,
                token,
                "/v1/event",
                payload={
                    "id": command["id"],
                    "type": "status",
                    "status": "manual_required",
                    "reason": "ручная проверка",
                },
            )
            _request(
                port,
                token,
                "/v1/event",
                payload={
                    "id": command["id"],
                    "type": "result",
                    "status": "phone",
                    "phone": "+79991234567",
                },
            )
        except BaseException as exc:  # pragma: no cover - surfaced in the main thread
            failures.append(exc)

    thread = threading.Thread(target=extension)
    thread.start()
    statuses = []
    try:
        result = bridge.execute(
            url="https://www.avito.ru/moskva/test_123",
            row_id="2",
            max_clicks=1,
            status_callback=statuses.append,
        )
    finally:
        thread.join(timeout=5)
        bridge.close()

    assert not failures
    assert result.status == "phone"
    assert result.payload["phone"] == "+79991234567"
    assert [event.status for event in statuses] == ["manual_required"]


def test_bridge_exposes_stop_to_the_waiting_extension(settings):
    bridge = ExtensionBridge(settings)
    bridge.state.command = {"id": "waiting-command"}

    assert bridge._command_status("waiting-command") == "active"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "STOP").write_text("stop", encoding="utf-8")

    assert bridge._command_status("waiting-command") == "cancelled"


class _FakeNotifier:
    enabled = False

    def close(self) -> None:
        return


class _RecordingNotifier:
    enabled = True

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def send_captcha_detected(self, **kwargs: object) -> None:
        self.events.append(("detected", kwargs))

    def send_captcha_reminder(self, **kwargs: object) -> None:
        self.events.append(("reminder", kwargs))

    def send_captcha_stopped(self, **kwargs: object) -> None:
        self.events.append(("stopped", kwargs))

    def close(self) -> None:
        return


class _FakeOcr:
    def read_avito_screen_png(self, png: bytes) -> PhoneResult:
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        return PhoneResult("+79991234567", "fake-avito-screen")

    def read_png(self, png: bytes, _artifact_path=None, *, psm: int = 7) -> PhoneResult:
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        return PhoneResult("+79991234567", f"fake-ocr-region-{psm}")

    def read_viewport_png(self, png: bytes) -> PhoneResult:
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        return PhoneResult("+79991234567", "fake-ocr")


class _FakeBridge:
    def __init__(self, result: ExtensionEvent) -> None:
        self.result = result

    def start(self) -> None:
        return

    def close(self) -> None:
        return

    def execute(self, **_kwargs: object) -> ExtensionEvent:
        return self.result


def test_extension_browser_normalizes_a_dom_phone(settings):
    browser = ChromeExtensionBrowser(settings, _FakeOcr(), _FakeNotifier())
    browser.bridge = _FakeBridge(
        ExtensionEvent(
            "result",
            "phone",
            {"phone": "8 (999) 123-45-67", "source": "chrome-extension-dom"},
        )
    )

    with browser:
        result = browser.reveal_phone("https://www.avito.ru/moskva/test_123", max_clicks=1)

    assert result.phone == "+79991234567"
    assert result.source == "chrome-extension-dom"


def test_extension_browser_maps_stop_to_a_non_failure_signal(settings):
    browser = ChromeExtensionBrowser(settings, _FakeOcr(), _FakeNotifier())
    browser.bridge = _FakeBridge(
        ExtensionEvent(
            "result",
            "cancelled",
            {"reason": "остановлено оператором"},
        )
    )

    with browser, pytest.raises(OperatorStopRequested):
        browser.reveal_phone("https://www.avito.ru/moskva/test_123", max_clicks=1)


def test_extension_browser_counts_each_solved_captcha_once(settings):
    browser = ChromeExtensionBrowser(settings, _FakeOcr(), _FakeNotifier())
    required = ExtensionEvent("status", "manual_required", {"reason": "капчу"})
    cleared = ExtensionEvent("status", "manual_cleared", {})

    browser._handle_status(required, "https://www.avito.ru/moskva/test_123")
    browser._handle_status(cleared, "https://www.avito.ru/moskva/test_123")
    browser._handle_status(cleared, "https://www.avito.ru/moskva/test_123")

    assert browser.captchas_solved == 1


def test_extension_browser_reminds_and_reports_operator_stop(settings):
    notifier = _RecordingNotifier()
    browser = ChromeExtensionBrowser(settings, _FakeOcr(), notifier)
    url = "https://www.avito.ru/moskva/test_123"

    browser._handle_status(
        ExtensionEvent("status", "manual_required", {"reason": "ручную проверку"}),
        url,
    )
    browser._handle_status(
        ExtensionEvent(
            "status",
            "manual_reminder",
            {"reason": "ручную проверку", "elapsed_seconds": 3600, "escalate": True},
        ),
        url,
    )
    browser._notify_captcha_stopped(url)

    assert [name for name, _kwargs in notifier.events] == ["detected", "reminder", "stopped"]
    assert notifier.events[-1][1]["include_backup"] is True


def test_extension_browser_sends_a_viewport_screenshot_to_ocr(settings):
    png = b"\x89PNG\r\n\x1a\nplaceholder"
    browser = ChromeExtensionBrowser(settings, _FakeOcr(), _FakeNotifier())
    browser.bridge = _FakeBridge(
        ExtensionEvent(
            "result",
            "screenshot",
            {"screenshot": "data:image/png;base64," + base64.b64encode(png).decode()},
        )
    )

    with browser:
        result = browser.reveal_phone("https://www.avito.ru/moskva/test_123", max_clicks=1)

    assert result.phone == "+79991234567"
    assert result.source == "fake-ocr"


def test_extension_browser_can_capture_the_interactive_windows_desktop(settings, monkeypatch):
    png = b"\x89PNG\r\n\x1a\nplaceholder"
    monkeypatch.setattr(chrome_extension_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        chrome_extension_module,
        "_capture_interactive_desktop_png",
        lambda _crop=None: png,
    )
    browser = ChromeExtensionBrowser(settings, _FakeOcr(), _FakeNotifier())
    browser.bridge = _FakeBridge(
        ExtensionEvent(
            "result",
            "screen_capture",
            {
                "crop": {
                    "left": 100,
                    "top": 200,
                    "width": 300,
                    "height": 80,
                    "screenWidth": 1920,
                    "screenHeight": 1080,
                }
            },
        )
    )

    with browser:
        result = browser.reveal_phone("https://www.avito.ru/moskva/test_123", max_clicks=1)

    assert result.phone == "+79991234567"
    assert result.source == "ocr-confirmed-avito-screen"


def test_extension_browser_uses_multiline_ocr_for_a_phone_dialog(settings, monkeypatch):
    png = b"\x89PNG\r\n\x1a\nplaceholder"
    monkeypatch.setattr(chrome_extension_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        chrome_extension_module,
        "_capture_interactive_desktop_png",
        lambda _crop=None: png,
    )
    browser = ChromeExtensionBrowser(settings, _FakeOcr(), _FakeNotifier())
    browser.bridge = _FakeBridge(
        ExtensionEvent(
            "result",
            "screen_capture",
            {
                "crop": {
                    "left": 300,
                    "top": 150,
                    "width": 650,
                    "height": 450,
                    "screenWidth": 1280,
                    "screenHeight": 720,
                    "kind": "dialog",
                }
            },
        )
    )

    with browser:
        result = browser.reveal_phone("https://www.avito.ru/moskva/test_123", max_clicks=1)

    assert result.phone == "+79991234567"
    assert result.source == "ocr-confirmed-avito-screen"


def test_extension_browser_rejects_an_uncropped_desktop_screenshot(settings):
    from avito_crm.errors import PhoneNotFoundError

    browser = ChromeExtensionBrowser(settings, _FakeOcr(), _FakeNotifier())
    browser.bridge = _FakeBridge(ExtensionEvent("result", "screen_capture", {"crop": None}))

    with pytest.raises(PhoneNotFoundError, match="безопасно определить"), browser:
        browser.reveal_phone("https://www.avito.ru/moskva/test_123", max_clicks=1)
