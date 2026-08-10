from __future__ import annotations

import base64
import io
import json
import socket
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

import avito_crm.chrome_extension as chrome_extension_module
from avito_crm.chrome_extension import (
    EXPECTED_EXTENSION_VERSION,
    ChromeExtensionBrowser,
    ExtensionBridge,
    ExtensionEvent,
    _decode_screenshot,
    _tab_capture_regions,
)
from avito_crm.errors import (
    BrowserOperationError,
    InvalidListingError,
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
) -> tuple[int, dict[str, object] | None]:
    body = None if payload is None else json.dumps(payload).encode()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if extension_version:
        headers["X-Avito-CRM-Extension-Version"] = extension_version
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


def test_bridge_command_declares_bounded_navigation_budget(settings):
    bridge = ExtensionBridge(settings)
    bridge.state.last_seen = chrome_extension_module.time.monotonic()
    bridge.state.extension_version = EXPECTED_EXTENSION_VERSION
    bridge.server = object()  # type: ignore[assignment]
    captured: dict[str, object] = {}

    def receive() -> None:
        with bridge.state.condition:
            while bridge.state.command is None:
                bridge.state.condition.wait(timeout=1)
            captured.update(bridge.state.command)
            bridge.state.result = ExtensionEvent(
                "result", "page_not_ready", {"reason": "not ready"}
            )
            bridge.state.condition.notify_all()

    thread = threading.Thread(target=receive)
    thread.start()
    try:
        bridge.execute(
            url="https://www.avito.ru/moskva/test_123456",
            row_id="2",
            max_clicks=1,
        )
    finally:
        thread.join(timeout=5)
        bridge.server = None

    assert captured["navigationAttempts"] == 2
    assert captured["navigationRetryDelayMs"] == 1500


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


class _FailingOcr:
    def read_png(self, png: bytes, artifact_path=None, *, psm: int = 7) -> PhoneResult:
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        raise PhoneNotFoundError(f"diagnostic failure psm={psm}")

    def read_viewport_png(self, png: bytes) -> PhoneResult:
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        raise PhoneNotFoundError("diagnostic viewport failure")


class _FakeBridge:
    def __init__(self, result: ExtensionEvent) -> None:
        self.result = result

    def start(self) -> None:
        return

    def close(self) -> None:
        return

    def execute(self, **_kwargs: object) -> ExtensionEvent:
        return self.result


def _tab_png(width: int = 2400, height: int = 1350) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (230, 235, 240)).save(buffer, format="PNG")
    return buffer.getvalue()


def _capture_metadata(*, kind: str = "control") -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "viewport": {"cssWidth": 1200, "cssHeight": 675, "devicePixelRatio": 2},
        "region": {
            "left": 600,
            "top": 250,
            "width": 300 if kind == "control" else 650,
            "height": 60 if kind == "control" else 350,
            "kind": kind,
        },
    }


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


def test_extension_browser_logs_bounded_reload_without_personal_data(settings, caplog):
    browser = ChromeExtensionBrowser(settings, _FakeOcr(), _FakeNotifier())
    event = ExtensionEvent(
        "status",
        "retrying_after_click_no_effect",
        {"reason": "Кнопка не раскрылась"},
    )

    with caplog.at_level("WARNING", logger="avito_crm.chrome_extension"):
        browser._handle_status(event, "https://www.avito.ru/moskva/test_123")

    assert "вкладка перезагружается для последней попытки" in caplog.text
    assert "test_123" not in caplog.text


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


def test_extension_browser_sends_a_tab_capture_to_inline_ocr(settings):
    png = _tab_png()
    browser = ChromeExtensionBrowser(
        settings,
        _FakeOcr(),
        _FakeNotifier(),
        save_failed_captures=True,
    )
    browser.bridge = _FakeBridge(
        ExtensionEvent(
            "result",
            "tab_capture",
            {
                "screenshot": "data:image/png;base64," + base64.b64encode(png).decode(),
                "capture": _capture_metadata(),
            },
        )
    )

    with browser:
        result = browser.reveal_phone("https://www.avito.ru/moskva/test_123", max_clicks=1)

    assert result.phone == "+79991234567"
    assert result.source == "ocr-tab-control"
    assert not (settings.output_dir / "ocr-failed-captures").exists()


def test_failed_tab_capture_is_not_persisted_by_default(settings):
    png = _tab_png()
    browser = ChromeExtensionBrowser(settings, _FailingOcr(), _FakeNotifier())
    browser.bridge = _FakeBridge(
        ExtensionEvent(
            "result",
            "tab_capture",
            {
                "screenshot": "data:image/png;base64," + base64.b64encode(png).decode(),
                "capture": _capture_metadata(),
            },
        )
    )

    with browser, pytest.raises(PhoneNotFoundError):
        browser.reveal_phone("https://www.avito.ru/moskva/test_123", max_clicks=1)

    assert not (settings.output_dir / "ocr-failed-captures").exists()


def test_opt_in_persists_exact_failed_tab_capture_and_safe_metadata(settings):
    png = _tab_png()
    capture = _capture_metadata()
    capture["fallback"] = "offscreen_region"
    browser = ChromeExtensionBrowser(
        settings,
        _FailingOcr(),
        _FakeNotifier(),
        save_failed_captures=True,
    )
    browser.bridge = _FakeBridge(
        ExtensionEvent(
            "result",
            "tab_capture",
            {
                "screenshot": "data:image/png;base64," + base64.b64encode(png).decode(),
                "capture": capture,
            },
        )
    )

    with browser, pytest.raises(PhoneNotFoundError):
        browser.reveal_phone("https://www.avito.ru/moskva/test_123", max_clicks=1)

    directory = settings.output_dir / "ocr-failed-captures"
    png_files = list(directory.glob("*.png"))
    metadata_files = list(directory.glob("*.json"))
    assert len(png_files) == 1
    assert len(metadata_files) == 1
    assert png_files[0].read_bytes() == png
    metadata_text = metadata_files[0].read_text(encoding="utf-8")
    metadata = json.loads(metadata_text)
    assert metadata["reason"] == "ocr_failed"
    assert metadata["contains_visible_page_data"] is True
    assert metadata["png_file"] == png_files[0].name
    assert metadata["png_sha256"] == chrome_extension_module.hashlib.sha256(png).hexdigest()
    assert metadata["capture"]["capture_fallback"] == "offscreen_region"
    assert metadata["capture"]["region_origin_css"] == "600,250"
    assert "https://" not in metadata_text
    assert "test_123" not in metadata_text
    assert "+7999" not in metadata_text
    assert not list(directory.glob("*.tmp"))


def test_tab_capture_maps_css_region_to_high_dpi_png() -> None:
    regions, diagnostics = _tab_capture_regions(_tab_png(), _capture_metadata())

    assert [label for label, _png, _psm in regions] == ["control", "control-context"]
    exact = Image.open(io.BytesIO(regions[0][1]))
    assert 730 <= exact.width <= 750
    assert 195 <= exact.height <= 205
    assert diagnostics["effective_scale"] == "2.000x2.000"
    assert diagnostics["region_kind"] == "control"


def test_extension_browser_uses_multiline_ocr_for_a_phone_dialog(settings):
    png = _tab_png()
    browser = ChromeExtensionBrowser(settings, _FakeOcr(), _FakeNotifier())
    browser.bridge = _FakeBridge(
        ExtensionEvent(
            "result",
            "tab_capture",
            {
                "screenshot": "data:image/png;base64," + base64.b64encode(png).decode(),
                "capture": _capture_metadata(kind="dialog"),
            },
        )
    )

    with browser:
        result = browser.reveal_phone("https://www.avito.ru/moskva/test_123", max_clicks=1)

    assert result.phone == "+79991234567"
    assert result.source == "ocr-tab-dialog"


def test_tab_capture_diagnostics_never_log_png_or_phone(settings, caplog):
    png = _tab_png()
    browser = ChromeExtensionBrowser(settings, _FakeOcr(), _FakeNotifier())
    browser.bridge = _FakeBridge(
        ExtensionEvent(
            "result",
            "tab_capture",
            {
                "screenshot": "data:image/png;base64," + base64.b64encode(png).decode(),
                "capture": _capture_metadata(kind="dialog"),
            },
        )
    )

    with caplog.at_level("INFO", logger="avito_crm.chrome_extension"), browser:
        result = browser.reveal_phone("https://www.avito.ru/moskva/test_123", max_clicks=1)

    diagnostics = "\n".join(record.getMessage() for record in caplog.records)
    assert result.phone == "+79991234567"
    assert "png_sha256" in diagnostics
    assert "region_kind" in diagnostics
    assert "data:image/png" not in diagnostics
    assert base64.b64encode(png).decode() not in diagnostics
    assert result.phone not in diagnostics


def test_decode_screenshot_rejects_payload_over_the_bound(monkeypatch):
    monkeypatch.setattr(chrome_extension_module, "MAX_TAB_CAPTURE_PNG_BYTES", 8)
    oversized = base64.b64encode(b"\x89PNG\r\n\x1a\nX").decode()

    with pytest.raises(BrowserOperationError, match="слишком большой"):
        _decode_screenshot("data:image/png;base64," + oversized)


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


def test_extension_browser_maps_a_wrong_listing_to_invalid_input(settings):
    browser = ChromeExtensionBrowser(settings, _FakeOcr(), _FakeNotifier())
    browser.bridge = _FakeBridge(
        ExtensionEvent(
            "result",
            "invalid_listing",
            {"reason": "Avito открыл другое объявление"},
        )
    )

    with browser, pytest.raises(InvalidListingError, match="другое объявление"):
        browser.reveal_phone("https://www.avito.ru/moskva/test_123", max_clicks=1)


def test_extension_browser_rejects_a_legacy_desktop_screenshot(settings):
    browser = ChromeExtensionBrowser(settings, _FakeOcr(), _FakeNotifier())
    browser.bridge = _FakeBridge(ExtensionEvent("result", "screen_capture", {"crop": None}))

    with pytest.raises(BrowserOperationError, match="устаревший снимок Windows"), browser:
        browser.reveal_phone("https://www.avito.ru/moskva/test_123", max_clicks=1)


def test_extension_browser_rejects_tab_capture_without_region_metadata(settings):
    png = _tab_png()
    browser = ChromeExtensionBrowser(settings, _FakeOcr(), _FakeNotifier())
    browser.bridge = _FakeBridge(
        ExtensionEvent(
            "result",
            "tab_capture",
            {"screenshot": "data:image/png;base64," + base64.b64encode(png).decode()},
        )
    )

    with pytest.raises(BrowserOperationError, match="координаты"), browser:
        browser.reveal_phone("https://www.avito.ru/moskva/test_123", max_clicks=1)


def test_tab_capture_rejects_incompatible_axis_scaling() -> None:
    with pytest.raises(BrowserOperationError, match="масштаб по осям"):
        _tab_capture_regions(_tab_png(2400, 900), _capture_metadata())
