from __future__ import annotations

import ctypes
import math
import os
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any


class NativeInputError(RuntimeError):
    """Fail-closed native input error carrying a privacy-safe code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def perform_windows_native_click(payload: dict[str, Any]) -> None:
    """Perform one real left click on a strictly validated ordinary-Chrome target."""

    geometry = validate_native_click_payload(payload)
    if os.name != "nt":
        raise NativeInputError("windows_only")

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _configure_apis(user32, kernel32)
    _require_unlocked_interactive_desktop(user32)

    previous_dpi = _set_thread_dpi_awareness(user32)
    original_cursor = wintypes.POINT()
    cursor_saved = bool(user32.GetCursorPos(ctypes.byref(original_cursor)))
    try:
        hwnd, rect = _select_chrome_window(user32, kernel32, geometry)
        _bring_verified_window_foreground(user32, hwnd)
        x, y = target_screen_point(geometry, rect)
        if not user32.SetCursorPos(x, y):
            raise NativeInputError("cursor_position_failed")
        time.sleep(0.05)
        if user32.GetForegroundWindow() != hwnd:
            raise NativeInputError("chrome_lost_foreground")
        inputs = (_INPUT * 2)(
            _mouse_input(_MOUSEEVENTF_LEFTDOWN),
            _mouse_input(_MOUSEEVENTF_LEFTUP),
        )
        sent = int(user32.SendInput(2, inputs, ctypes.sizeof(_INPUT)))
        if sent != 2:
            raise NativeInputError("send_input_failed")
        time.sleep(0.08)
    finally:
        if cursor_saved:
            user32.SetCursorPos(original_cursor.x, original_cursor.y)
        _restore_thread_dpi_awareness(user32, previous_dpi)


def validate_native_click_payload(payload: dict[str, Any]) -> dict[str, Any]:
    window = _mapping(payload.get("window"), "window_geometry_invalid")
    viewport = _mapping(payload.get("viewport"), "viewport_geometry_invalid")
    target = _mapping(payload.get("target"), "target_geometry_invalid")

    state = str(window.get("state", ""))
    if not bool(window.get("focused")) or state not in {"normal", "maximized", "fullscreen"}:
        raise NativeInputError("chrome_window_not_ready")

    window_values = _numbers(window, ("left", "top", "width", "height"))
    viewport_values = _numbers(
        viewport,
        (
            "screenX",
            "screenY",
            "outerWidth",
            "outerHeight",
            "innerWidth",
            "innerHeight",
            "devicePixelRatio",
        ),
    )
    target_values = _numbers(target, ("x", "y", "width", "height"))

    if not bool(target.get("focused")):
        raise NativeInputError("click_target_not_focused")
    if not (320 <= window_values["width"] <= 10000):
        raise NativeInputError("window_geometry_invalid")
    if not (240 <= window_values["height"] <= 10000):
        raise NativeInputError("window_geometry_invalid")
    if not (0.5 <= viewport_values["devicePixelRatio"] <= 5):
        raise NativeInputError("dpi_geometry_invalid")
    if not (
        200 <= viewport_values["innerWidth"] <= window_values["width"] + 16
        and 120 <= viewport_values["innerHeight"] <= window_values["height"] + 16
    ):
        raise NativeInputError("viewport_geometry_invalid")
    if not (
        abs(viewport_values["outerWidth"] - window_values["width"]) <= 96
        and abs(viewport_values["outerHeight"] - window_values["height"]) <= 96
        and abs(viewport_values["screenX"] - window_values["left"]) <= 96
        and abs(viewport_values["screenY"] - window_values["top"]) <= 96
    ):
        raise NativeInputError("window_viewport_mismatch")
    if not (
        0 <= target_values["x"] < viewport_values["innerWidth"]
        and 0 <= target_values["y"] < viewport_values["innerHeight"]
        and 2 <= target_values["width"] <= 5000
        and 2 <= target_values["height"] <= 5000
    ):
        raise NativeInputError("target_geometry_invalid")

    return {"window": window_values, "viewport": viewport_values, "target": target_values}


def target_screen_point(
    geometry: dict[str, Any], physical_rect: tuple[int, int, int, int]
) -> tuple[int, int]:
    left, top, right, bottom = physical_rect
    physical_width = right - left
    physical_height = bottom - top
    window = geometry["window"]
    viewport = geometry["viewport"]
    target = geometry["target"]
    if physical_width < 320 or physical_height < 240:
        raise NativeInputError("chrome_window_geometry_invalid")
    scale_x = physical_width / window["width"]
    scale_y = physical_height / window["height"]
    expected_scale = viewport["devicePixelRatio"]
    if not (
        0.5 <= scale_x <= 5
        and 0.5 <= scale_y <= 5
        and abs(scale_x - scale_y) <= 0.35
        and abs(((scale_x + scale_y) / 2) - expected_scale) <= 0.6
    ):
        raise NativeInputError("dpi_geometry_mismatch")

    side_inset = max(0.0, (window["width"] - viewport["innerWidth"]) / 2)
    top_inset = max(
        0.0,
        window["height"] - viewport["innerHeight"] - side_inset,
    )
    x = round(left + ((side_inset + target["x"]) * scale_x))
    y = round(top + ((top_inset + target["y"]) * scale_y))
    if not (left + 2 <= x < right - 2 and top + 2 <= y < bottom - 2):
        raise NativeInputError("target_outside_chrome")
    return x, y


def _mapping(value: object, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise NativeInputError(code)
    return value


def _numbers(source: dict[str, Any], keys: tuple[str, ...]) -> dict[str, float]:
    result: dict[str, float] = {}
    for key in keys:
        value = source.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise NativeInputError("native_geometry_invalid")
        number = float(value)
        if not math.isfinite(number):
            raise NativeInputError("native_geometry_invalid")
        result[key] = number
    return result


def _configure_apis(user32: Any, kernel32: Any) -> None:
    user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.SwitchDesktop.argtypes = [wintypes.HANDLE]
    user32.SwitchDesktop.restype = wintypes.BOOL
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    user32.CloseDesktop.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL


def _require_unlocked_interactive_desktop(user32: Any) -> None:
    desktop = user32.OpenInputDesktop(0, False, 0x0100)
    if not desktop:
        raise NativeInputError("interactive_desktop_unavailable")
    try:
        if not user32.SwitchDesktop(desktop):
            raise NativeInputError("session_locked")
    finally:
        user32.CloseDesktop(desktop)


def _select_chrome_window(
    user32: Any, kernel32: Any, geometry: dict[str, Any]
) -> tuple[int, tuple[int, int, int, int]]:
    candidates: list[tuple[int, tuple[int, int, int, int]]] = []

    @_WNDENUMPROC
    def visitor(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        if _window_process_name(user32, kernel32, hwnd) != "chrome.exe":
            return True
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        physical = (rect.left, rect.top, rect.right, rect.bottom)
        if _window_matches_geometry(physical, geometry):
            candidates.append((int(hwnd), physical))
        return True

    if not user32.EnumWindows(visitor, 0):
        raise NativeInputError("chrome_window_lookup_failed")
    foreground = int(user32.GetForegroundWindow() or 0)
    for candidate in candidates:
        if candidate[0] == foreground:
            return candidate
    if len(candidates) != 1:
        raise NativeInputError("chrome_window_ambiguous")
    return candidates[0]


def _window_matches_geometry(
    physical: tuple[int, int, int, int], geometry: dict[str, Any]
) -> bool:
    left, top, right, bottom = physical
    actual_width = right - left
    actual_height = bottom - top
    window = geometry["window"]
    dpr = geometry["viewport"]["devicePixelRatio"]
    expected_width = window["width"] * dpr
    expected_height = window["height"] * dpr
    expected_left = window["left"] * dpr
    expected_top = window["top"] * dpr
    tolerance_x = max(48.0, expected_width * 0.12)
    tolerance_y = max(48.0, expected_height * 0.12)
    return (
        abs(actual_width - expected_width) <= tolerance_x
        and abs(actual_height - expected_height) <= tolerance_y
        and abs(left - expected_left) <= max(96.0, tolerance_x)
        and abs(top - expected_top) <= max(96.0, tolerance_y)
    )


def _window_process_name(user32: Any, kernel32: Any, hwnd: int) -> str:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = kernel32.OpenProcess(0x1000, False, pid.value)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
            return ""
        return Path(buffer.value).name.lower()
    finally:
        kernel32.CloseHandle(handle)


def _bring_verified_window_foreground(user32: Any, hwnd: int) -> None:
    if user32.GetForegroundWindow() != hwnd:
        if not user32.SetForegroundWindow(hwnd):
            raise NativeInputError("chrome_foreground_failed")
        time.sleep(0.12)
    if user32.GetForegroundWindow() != hwnd:
        raise NativeInputError("chrome_not_foreground")


def _set_thread_dpi_awareness(user32: Any) -> int | None:
    setter = getattr(user32, "SetThreadDpiAwarenessContext", None)
    if setter is None:
        return None
    setter.restype = ctypes.c_void_p
    previous = setter(ctypes.c_void_p(-4))
    return int(previous) if previous else None


def _restore_thread_dpi_awareness(user32: Any, previous: int | None) -> None:
    if previous is None:
        return
    setter = getattr(user32, "SetThreadDpiAwarenessContext", None)
    if setter is not None:
        setter(ctypes.c_void_p(previous))


_ULONG_PTR = wintypes.WPARAM


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("union",)
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


def _mouse_input(flags: int) -> _INPUT:
    return _INPUT(type=0, mi=_MOUSEINPUT(0, 0, 0, flags, 0, 0))


_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_WNDENUMPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
)
