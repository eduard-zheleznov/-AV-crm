from __future__ import annotations

from dataclasses import replace

import pytest

import avito_crm.chrome_extension as chrome_extension_module
from avito_crm.chrome_extension import EXPECTED_EXTENSION_VERSION, ExtensionBridge
from avito_crm.windows_native_input import (
    NativeInputError,
    target_screen_point,
    validate_native_click_payload,
)


def _payload() -> dict[str, object]:
    return {
        "commandId": "command-1",
        "nativeToken": "native-token-1",
        "listingId": "123456789",
        "tabId": 42,
        "window": {
            "left": 0,
            "top": 0,
            "width": 1280,
            "height": 800,
            "focused": True,
            "state": "normal",
        },
        "viewport": {
            "screenX": 0,
            "screenY": 0,
            "outerWidth": 1280,
            "outerHeight": 800,
            "innerWidth": 1280,
            "innerHeight": 700,
            "devicePixelRatio": 1.5,
        },
        "target": {
            "x": 640,
            "y": 350,
            "width": 220,
            "height": 48,
            "focused": True,
        },
    }


def test_native_target_uses_validated_window_viewport_and_dpi() -> None:
    geometry = validate_native_click_payload(_payload())

    assert target_screen_point(geometry, (0, 0, 1920, 1200)) == (960, 675)


@pytest.mark.parametrize(
    ("section", "key", "value", "code"),
    [
        ("window", "focused", False, "chrome_window_not_ready"),
        ("target", "focused", False, "click_target_not_focused"),
        ("target", "x", 50000, "target_geometry_invalid"),
        ("viewport", "screenX", 5000, "window_viewport_mismatch"),
    ],
)
def test_native_target_rejects_untrusted_geometry(
    section: str, key: str, value: object, code: str
) -> None:
    payload = _payload()
    payload[section][key] = value  # type: ignore[index]

    with pytest.raises(NativeInputError, match=code):
        validate_native_click_payload(payload)


def test_bridge_native_click_is_exact_single_use_and_stop_safe(
    settings, monkeypatch
) -> None:
    configured = replace(settings, avito_extension_token="n" * 64)
    bridge = ExtensionBridge(configured)
    bridge.state.command = {
        "id": "command-1",
        "type": "reveal_phone",
        "url": "https://www.avito.ru/moskva/test_123456789",
        "nativeClickToken": "native-token-1",
    }
    bridge.state.extension_version = EXPECTED_EXTENSION_VERSION
    bridge.state.extension_instance = "instance-1"
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        chrome_extension_module,
        "perform_windows_native_click",
        lambda payload: calls.append(payload),
    )

    first = bridge._receive_native_click(
        _payload(),
        extension_version=EXPECTED_EXTENSION_VERSION,
        extension_instance="instance-1",
    )
    second = bridge._receive_native_click(
        _payload(),
        extension_version=EXPECTED_EXTENSION_VERSION,
        extension_instance="instance-1",
    )

    assert first == {"ok": True, "code": "native_click_dispatched"}
    assert second == {"ok": False, "code": "native_click_already_used"}
    assert len(calls) == 1

    bridge.state.native_click_used = False
    configured.data_dir.mkdir(parents=True, exist_ok=True)
    (configured.data_dir / "STOP").write_text("stop", encoding="utf-8")
    stopped = bridge._receive_native_click(
        _payload(),
        extension_version=EXPECTED_EXTENSION_VERSION,
        extension_instance="instance-1",
    )
    assert stopped == {"ok": False, "code": "command_cancelled"}
    assert len(calls) == 1


def test_bridge_native_click_rejects_token_listing_and_instance(settings) -> None:
    bridge = ExtensionBridge(settings)
    bridge.state.command = {
        "id": "command-1",
        "type": "reveal_phone",
        "url": "https://www.avito.ru/moskva/test_123456789",
        "nativeClickToken": "native-token-1",
    }
    bridge.state.extension_version = EXPECTED_EXTENSION_VERSION
    bridge.state.extension_instance = "instance-1"

    wrong_token = _payload()
    wrong_token["nativeToken"] = "other"
    assert bridge._receive_native_click(
        wrong_token,
        extension_version=EXPECTED_EXTENSION_VERSION,
        extension_instance="instance-1",
    )["code"] == "native_token_mismatch"

    wrong_listing = _payload()
    wrong_listing["listingId"] = "987654321"
    assert bridge._receive_native_click(
        wrong_listing,
        extension_version=EXPECTED_EXTENSION_VERSION,
        extension_instance="instance-1",
    )["code"] == "listing_mismatch"

    assert bridge._receive_native_click(
        _payload(),
        extension_version=EXPECTED_EXTENSION_VERSION,
        extension_instance="other-instance",
    )["code"] == "extension_instance_mismatch"
