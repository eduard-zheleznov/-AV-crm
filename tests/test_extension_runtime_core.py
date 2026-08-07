from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_extension_runtime_core_offline_contract() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    script = Path(__file__).parent / "js" / "test-runtime-core.cjs"

    subprocess.run([node, str(script)], check=True, timeout=10)
