from __future__ import annotations

import base64
import json
import socket
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest

import avito_crm.chrome_extension as chrome_extension_module
from avito_crm.chrome_extension import (
    EXPECTED_EXTENSION_VERSION,
    ChromeExtensionBrowser,
    ExtensionBridge,
    ExtensionEvent,
)
from avito_crm.errors import (
    BrowserInfrastructureError,
    BrowserOperationError,
    ClickNotEffectiveError,
    ManualActionRequired,
    OperatorStopRequested,
    PageNotReadyError,
    PhoneNotFoundError,
)
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
    extension_version: str = "",
    extension_instance: str = "",
) -> tuple[int, dict[str, object] | None]:
    body = None if payload is None else json.dumps(payload).encode()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if extension_version:
        headers["X-Avito-CRM-Extension-Version"] = extension_version
    if extension_instance:
        headers["X-Avito-CRM-Extension-Instance"] = extension_instance
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        method="POST" if payload is not None else "GET",
        headers=headers,
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
            status, command = _request(
                port,
                token,
                "/v1/poll",
                extension_version=EXPECTED_EXTENSION_VERSION,
            )
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


def test_bridge_runs_a_no_click_health_probe_and_records_the_instance(settings):
    port = _free_port()
    token = "c" * 64
    configured = replace(
        settings,
        avito_extension_port=port,
        avito_extension_token=token,
        avito_extension_connect_timeout=2,
        avito_page_timeout=2,
    )
    bridge = ExtensionBridge(configured)
    bridge.start()
    failures: list[BaseException] = []

    def extension() -> None:
        try:
            _status, command = _request(
                port,
                token,
                "/v1/poll",
                extension_version=EXPECTED_EXTENSION_VERSION,
                extension_instance="test-instance-1",
            )
            assert command is not None
            assert command["type"] == "health_probe"
            assert command["maxClicks"] == 0
            _request(
                port,
                token,
                "/v1/event",
                payload={
                    "id": command["id"],
                    "type": "result",
                    "status": "healthy",
                },
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)

    thread = threading.Thread(target=extension)
    thread.start()
    try:
        result = bridge.health_probe()
        snapshot = bridge.health_snapshot()
    finally:
        thread.join(timeout=5)
        bridge.close()

    assert not failures
    assert result.status == "healthy"
    assert snapshot["connected"] is True
    assert snapshot["extension_instance"] == "test-instance-1"


def test_bridge_rejects_a_stale_extension_before_dispatch(settings):
    bridge = ExtensionBridge(settings)
    bridge.start()
    try:
        with bridge.state.condition:
            bridge.state.last_seen = chrome_extension_module.time.monotonic()
            bridge.state.extension_version = "1.0.5"
        with pytest.raises(BrowserOperationError, match="устарело"):
            bridge.execute(
                url="https://www.avito.ru/moskva/test_123",
                row_id="2",
                max_clicks=1,
            )
        assert bridge.state.command is None
    finally:
        bridge.close()


def test_manifest_matches_the_required_extension_version():
    manifest_path = Path(__file__).parents[1] / "chrome-extension" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["version"] == EXPECTED_EXTENSION_VERSION
    assert "debugger" in manifest["permissions"]
    assert "scripting" in manifest["permissions"]


def test_diagnostic_log_keeps_ids_but_redacts_urls_and_unknown_payload(settings):
    bridge = ExtensionBridge(settings)
    bridge._write_diagnostic(
        "extension_result",
        url="https://www.avito.ru/moskva/secret-slug_123456789?token=do-not-log",
        diagnostics={
            "actualSurface": "about:blank",
            "pendingSurface": "avito:listing",
            "contentInjectionAttempted": True,
            "contentInjectionStatus": "injected",
            "contentVersion": EXPECTED_EXTENSION_VERSION,
            "stableSamples": 4,
            "stableForMs": 1500,
            "actualUrl": "https://www.avito.ru/moskva/secret-slug_123456789",
            "attempts": [
                {
                    "stage": "navigation",
                    "actualListingId": "123456789",
                    "actualSurface": "https://www.avito.ru/moskva/secret-path",
                    "contentInjectionStatus": "https://do-not-log.example",
                    "tabStatus": "loading",
                    "phone": "+79991234567",
                }
            ],
        },
    )

    content = (settings.logs_dir / "chrome-extension-health.jsonl").read_text(encoding="utf-8")
    record = json.loads(content)
    assert record["listing_id"] == "123456789"
    assert record["diagnostics"]["attempts"][0]["tabStatus"] == "loading"
    assert record["diagnostics"]["attempts"][0]["actualSurface"] == "invalid"
    assert record["diagnostics"]["attempts"][0]["contentInjectionStatus"] == "unknown"
    assert record["diagnostics"]["contentVersion"] == EXPECTED_EXTENSION_VERSION
    assert record["diagnostics"]["actualSurface"] == "about:blank"
    assert record["diagnostics"]["pendingSurface"] == "avito:listing"
    assert record["diagnostics"]["contentInjectionAttempted"] is True
    assert record["diagnostics"]["contentInjectionStatus"] == "injected"
    assert record["diagnostics"]["stableSamples"] == 4
    assert record["diagnostics"]["stableForMs"] == 1500
    assert "https://" not in content
    assert "do-not-log" not in content
    assert "+79991234567" not in content


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

    def read_png(self, png: bytes, artifact_path=None, *, psm: int = 7) -> PhoneResult:
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

    def health_snapshot(self) -> dict[str, object]:
        return {
            "connected": True,
            "extension_version": EXPECTED_EXTENSION_VERSION,
            "extension_instance": "test-extension-instance",
        }

    def health_probe(self) -> ExtensionEvent:
        return ExtensionEvent("result", "healthy", {})

    def execute(self, **_kwargs: object) -> ExtensionEvent:
        return self.result


class _ProbeBridge(_FakeBridge):
    def __init__(self, probe_result: ExtensionEvent) -> None:
        super().__init__(ExtensionEvent("result", "phone", {"phone": "+79991234567"}))
        self.probe_result = probe_result
        self.probe_calls = 0

    def health_probe(self) -> ExtensionEvent:
        self.probe_calls += 1
        return self.probe_result


def test_extension_browser_caches_a_successful_active_probe(settings):
    browser = ChromeExtensionBrowser(settings, _FakeOcr(), _FakeNotifier())
    bridge = _ProbeBridge(ExtensionEvent("result", "healthy", {}))
    browser.bridge = bridge

    with browser:
        browser.preflight(force=True)
        browser.preflight()

    assert bridge.probe_calls == 1


def test_extension_browser_never_treats_captcha_as_healthy(settings):
    browser = ChromeExtensionBrowser(settings, _FakeOcr(), _FakeNotifier())
    browser.bridge = _ProbeBridge(
        ExtensionEvent("result", "manual_required", {"reason": "ручная проверка Avito"})
    )

    with browser, pytest.raises(ManualActionRequired, match="ручная проверка"):
        browser.preflight(force=True)


def test_extension_browser_marks_browser_infra_for_a_fresh_canary(settings):
    browser = ChromeExtensionBrowser(settings, _FakeOcr(), _FakeNotifier())
    browser.bridge = _ProbeBridge(
        ExtensionEvent("result", "browser_infra", {"reason": "renderer timeout"})
    )

    with browser, pytest.raises(BrowserInfrastructureError, match="renderer timeout"):
        browser.preflight(force=True)


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


def test_extension_browser_never_maps_a_legacy_timeout_to_captcha(settings):
    browser = ChromeExtensionBrowser(settings, _FakeOcr(), _FakeNotifier())
    browser.bridge = _FakeBridge(
        ExtensionEvent(
            "result",
            "manual_timeout",
            {"reason": "ручная проверка Avito не завершена за отведённое время"},
        )
    )

    with browser, pytest.raises(PhoneNotFoundError, match="без статуса капчи"):
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
    crop = {
        "left": 100,
        "top": 200,
        "width": 300,
        "height": 80,
        "screenWidth": 1920,
        "screenHeight": 1080,
    }
    captures = []
    monkeypatch.setattr(chrome_extension_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        chrome_extension_module,
        "_capture_interactive_desktop_png",
        lambda requested_crop=None: captures.append(requested_crop) or png,
    )
    browser = ChromeExtensionBrowser(settings, _FakeOcr(), _FakeNotifier())
    browser.bridge = _FakeBridge(
        ExtensionEvent(
            "result",
            "screen_capture",
            {
                "crop": crop,
            },
        )
    )

    with browser:
        result = browser.reveal_phone("https://www.avito.ru/moskva/test_123", max_clicks=1)

    assert result.phone == "+79991234567"
    assert result.source == "ocr-confirmed-avito-region"
    assert captures == [crop, crop]


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
    assert result.source == "ocr-confirmed-avito-region"


def test_extension_browser_maps_an_unloaded_page_to_a_safe_retry(settings):
    browser = ChromeExtensionBrowser(settings, _FakeOcr(), _FakeNotifier())
    browser.bridge = _FakeBridge(
        ExtensionEvent(
            "result",
            "page_not_ready",
            {"reason": "Страница объявления не успела полностью отобразиться"},
        )
    )

    with browser, pytest.raises(PageNotReadyError, match="не успела"):
        browser.reveal_phone("https://www.avito.ru/moskva/test_123", max_clicks=1)


def test_extension_browser_stops_before_ocr_when_click_has_no_effect(settings):
    browser = ChromeExtensionBrowser(settings, _FakeOcr(), _FakeNotifier())
    browser.bridge = _FakeBridge(
        ExtensionEvent(
            "result",
            "click_not_effective",
            {"reason": "DOM не изменился"},
        )
    )

    with browser, pytest.raises(ClickNotEffectiveError, match="DOM не изменился"):
        browser.reveal_phone("https://www.avito.ru/moskva/test_123", max_clicks=1)


def test_extension_browser_maps_stale_content_to_browser_infrastructure(settings):
    browser = ChromeExtensionBrowser(settings, _FakeOcr(), _FakeNotifier())
    browser.bridge = _FakeBridge(
        ExtensionEvent(
            "result",
            "stale_content_script",
            {"reason": "Content script Chrome не совпадает с версией расширения"},
        )
    )

    with browser, pytest.raises(BrowserInfrastructureError, match="не совпадает"):
        browser.reveal_phone("https://www.avito.ru/moskva/test_123", max_clicks=1)


def test_content_script_does_not_report_dispatch_as_reveal_success():
    content_path = Path(__file__).parents[1] / "chrome-extension" / "content.js"
    content = content_path.read_text(encoding="utf-8")

    assert 'notifyStatus("clicked"' not in content
    assert '"click_dispatched"' in content
    assert 'notifyStatus("reveal_confirmed"' in content
    assert "actionAttempt <= 2" in content
    assert "avito_crm_browser_click" in content
    assert "expectedContentVersion" in (
        Path(__file__).parents[1] / "chrome-extension" / "service-worker.js"
    ).read_text(encoding="utf-8")
    assert ".dispatchEvent(" not in content
    assert "button.click()" not in content
    assert "topElement.contains(button)" not in content

    service_worker = (
        Path(__file__).parents[1] / "chrome-extension" / "service-worker.js"
    ).read_text(encoding="utf-8")
    assert "NAVIGATION.shouldInject" in service_worker
    assert "NAVIGATION.injectContentFiles" in service_worker
    assert "handleBrowserClick" in service_worker
    assert "dispatchUserGestureClick" in service_worker
    assert "measureBrowserClickTarget" not in service_worker
    assert "native-click" not in service_worker
    assert "x: message.x" not in service_worker
    assert "content_script_reload" not in service_worker
    assert "advanceRenderedStability" in service_worker
    assert "chrome_tab_not_complete" not in service_worker

    assert "findPhoneButtons" in content
    assert "button === previousButton" in content
    assert 'probe.readyState === "complete"' not in content

    trusted_click = (
        Path(__file__).parents[1] / "chrome-extension" / "trusted-click.js"
    ).read_text(encoding="utf-8")
    assert '"Runtime.evaluate"' in trusted_click
    assert "userGesture: true" in trusted_click
    assert "button.click()" in trusted_click
    assert "document_not_complete" not in trusted_click
    assert "document_not_ready" not in trusted_click
    assert '"Input.dispatchMouseEvent"' not in trusted_click
    assert "SendInput" not in trusted_click

    navigation_core = (
        Path(__file__).parents[1] / "chrome-extension" / "navigation-core.js"
    ).read_text(encoding="utf-8")
    assert "chromeApi.scripting.executeScript" in navigation_core
    assert '["runtime-core.js", "content.js"]' in navigation_core
    assert 'raw === "about:blank"' in navigation_core


def test_extension_browser_rejects_an_uncropped_desktop_screenshot(settings):
    from avito_crm.errors import PhoneNotFoundError

    browser = ChromeExtensionBrowser(settings, _FakeOcr(), _FakeNotifier())
    browser.bridge = _FakeBridge(ExtensionEvent("result", "screen_capture", {"crop": None}))

    with pytest.raises(PhoneNotFoundError, match="безопасно определить"), browser:
        browser.reveal_phone("https://www.avito.ru/moskva/test_123", max_clicks=1)
