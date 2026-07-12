from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_model_viewer_node_suite() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            node,
            "--test",
            "tests/js/model_layout.test.mjs",
            "tests/js/model_share.test.mjs",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
