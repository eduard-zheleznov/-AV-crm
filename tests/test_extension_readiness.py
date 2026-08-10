from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from avito_crm.chrome_extension import _extension_operation_timeout

ROOT = Path(__file__).parents[1]
BASELINE_GET_MANAGED_TAB_SHA256 = "1882aac323cf3979e7ccabe1147a0793c57f79a52d11965344c82fad09f88a29"
BASELINE_SEND_REVEAL_SHA256 = "3994e10b3da99b905cf273506682e0934122043fc9d3b9fec522d668832b31c5"
BASELINE_NAVIGATE_TAB_SHA256 = "7dce16eec6e69ae727c64dfb15dc3f2f662dc9d78d48beb5517831f9c5217c47"


def test_post_click_contract_preserves_success_paths_and_adds_only_bounded_retry() -> None:
    content = (ROOT / "chrome-extension" / "content.js").read_text(encoding="utf-8")
    start = content.index("  const button = await waitForPhoneButton")
    end = content.index("\n}\n\nasync function waitForManualAction", start) + 2
    post_click = content[start:end]
    assert 'return { status: "phone", phone, source: "chrome-extension-dom" };' in post_click
    assert "if (hasTemporaryNumberLabel())" in post_click
    assert 'return { status: "screenshot", crop }' in post_click
    assert "button.click();" in post_click
    assert post_click.count("return postClickCaptureResult(button, phoneRegion);") == 2
    assert 'status: "click_no_effect"' in post_click
    assert "crop === phoneRegion" in post_click
    assert "isVisibleInViewport(remainingButton)" in post_click
    assert '[role="dialog"]' in content[content.index("function captureRegion") :]


def test_manifest_loads_readiness_around_unchanged_content_at_document_start() -> None:
    manifest = json.loads((ROOT / "chrome-extension" / "manifest.json").read_text(encoding="utf-8"))
    script = manifest["content_scripts"][0]
    assert script["js"] == ["readiness-core.js", "content.js"]
    assert script["run_at"] == "document_start"
    assert manifest["version"] == "1.0.7.7"
    assert "<all_urls>" in manifest["host_permissions"]


def test_readiness_core_accepts_rendered_listing_without_window_load() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    result = subprocess.run(
        [node, str(ROOT / "tests" / "js" / "readiness-core.test.cjs")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "readiness-core: ok" in result.stdout


def test_phone_button_selection_prefers_current_viewport_without_losing_fallback() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    result = subprocess.run(
        [node, str(ROOT / "tests" / "js" / "phone-button-selection.test.cjs")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "phone-button-selection: ok" in result.stdout


def test_unrevealed_button_retry_requires_unchanged_control_capture() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    result = subprocess.run(
        [node, str(ROOT / "tests" / "js" / "phone-reveal-retry.test.cjs")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "phone-reveal-retry: ok" in result.stdout


def test_reveal_retry_core_allows_only_one_bounded_reload() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    result = subprocess.run(
        [node, str(ROOT / "tests" / "js" / "reveal-retry-core.test.cjs")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "reveal-retry-core: ok" in result.stdout


def test_tab_capture_core_validates_bounded_metadata_and_png() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    result = subprocess.run(
        [node, str(ROOT / "tests" / "js" / "tab-capture-core.test.cjs")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "tab-capture-core: ok" in result.stdout


def test_operation_timeout_covers_bounded_navigation_and_reveal(settings) -> None:
    configured = replace(
        settings,
        avito_page_timeout=45,
        avito_temp_number_wait_max=10,
        avito_phone_retry_max=15,
    )
    assert _extension_operation_timeout(configured, 1) == 176.5
    assert _extension_operation_timeout(configured, 2) == 338.0


def test_service_worker_finishes_current_command_before_polling_next() -> None:
    worker = (ROOT / "chrome-extension" / "service-worker.js").read_text(encoding="utf-8")
    poll_loop = worker[
        worker.index("async function pollLoop") : worker.index("async function executeCommand")
    ]
    execute = worker[
        worker.index("async function executeCommand") : worker.index("async function getManagedTab")
    ]
    assert "await executeCommand(command);" in poll_loop
    assert "await postEvent" in execute
    assert "NAVIGATION_ATTEMPTS = 2" in worker
    assert 'status: "page_not_ready"' not in poll_loop
    assert 'status: "tab_capture"' in execute
    assert "REVEAL_RETRY.shouldRetry(result.status, attempt, command.maxClicks)" in execute
    assert 'status: "retrying_after_click_no_effect"' in execute
    assert "REVEAL_RETRY.shouldCapture(result.status)" in execute
    assert "getManagedTab(command.url, command.pageTimeoutMs, true)" in execute
    assert '"reveal-retry-core.js"' in worker[: worker.index("const CONFIG")]
    assert 'status: "screen_capture"' not in execute
    assert "captureVisibleTab" in worker
    assert "TAB_CAPTURE_TIMEOUT_MS = 5000" in worker
    capture = worker[
        worker.index("async function captureManagedAvitoTab") : worker.index(
            "async function withTimeout"
        )
    ]
    assert capture.count("sameListing") == 2
    assert capture.count("activeBefore") >= 2
    assert capture.count("activeAfter") >= 2


def test_navigation_and_click_transport_are_byte_identical_to_readiness_hotfix() -> None:
    worker = (ROOT / "chrome-extension" / "service-worker.js").read_bytes()

    def function_blob(start: bytes, end: bytes) -> bytes:
        start_at = worker.index(b"async function " + start)
        end_at = worker.index(b"async function " + end, start_at)
        return worker[start_at:end_at]

    assert hashlib.sha256(function_blob(b"getManagedTab", b"resetManagedTab")).hexdigest() == (
        BASELINE_GET_MANAGED_TAB_SHA256
    )
    reveal_hash = hashlib.sha256(
        function_blob(b"sendRevealMessage", b"waitForCancellation")
    ).hexdigest()
    assert reveal_hash == BASELINE_SEND_REVEAL_SHA256
    assert hashlib.sha256(function_blob(b"navigateTab", b"postEvent")).hexdigest() == (
        BASELINE_NAVIGATE_TAB_SHA256
    )
