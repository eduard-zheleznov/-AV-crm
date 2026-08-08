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
BASELINE_CLICK_OCR_SHA256 = "f41ecb7805125b0f3982f285fb045134f4358ef1f9cb22940fda94e35524bf94"


def test_working_click_and_ocr_block_is_byte_identical_to_48bf() -> None:
    content = (ROOT / "chrome-extension" / "content.js").read_bytes()
    start = content.index(b"  const button = await waitForPhoneButton")
    end = content.index(b"\n}\n\nasync function waitForManualAction", start) + 2
    assert hashlib.sha256(content[start:end]).hexdigest() == BASELINE_CLICK_OCR_SHA256


def test_unchanged_post_click_contract_covers_inline_and_modal_ocr_fallback() -> None:
    content = (ROOT / "chrome-extension" / "content.js").read_text(encoding="utf-8")
    start = content.index("  const button = await waitForPhoneButton")
    end = content.index("\n}\n\nasync function waitForManualAction", start) + 2
    post_click = content[start:end]
    assert 'return { status: "phone", phone, source: "chrome-extension-dom" };' in post_click
    assert "if (hasTemporaryNumberLabel())" in post_click
    assert (
        'return { status: "screenshot", crop: captureRegion(button, phoneRegion) };'
        in post_click
    )
    assert "button.click();" in post_click
    assert '[role="dialog"]' in content[content.index("function captureRegion"):]


def test_manifest_loads_readiness_around_unchanged_content_at_document_start() -> None:
    manifest = json.loads(
        (ROOT / "chrome-extension" / "manifest.json").read_text(encoding="utf-8")
    )
    script = manifest["content_scripts"][0]
    assert script["js"] == ["readiness-core.js", "content.js"]
    assert script["run_at"] == "document_start"


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
