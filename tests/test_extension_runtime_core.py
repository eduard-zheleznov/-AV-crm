from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "script_name",
    [
        "test-runtime-core.cjs",
        "test-content-bootstrap.cjs",
        "test-navigation-core.cjs",
        "test-trusted-click.cjs",
    ],
)
def test_extension_runtime_core_offline_contract(script_name: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    script = Path(__file__).parent / "js" / script_name

    subprocess.run([node, str(script)], check=True, timeout=10)
