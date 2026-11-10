"""Plain-Node pins for pure client-module helpers that used to be probed in a browser.

Each script in ``tests/js`` listed here imports the real ES module under
``_browser_stubs.mjs`` and asserts a pure helper's result, so no browser
context is paid for it. Machines without Node skip cleanly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js is required"
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

_SCRIPTS = [
    "terminal_module_helpers.test.mjs",
    "readback_extraction.test.mjs",
    "session_title.test.mjs",
]


@pytest.mark.parametrize("script", _SCRIPTS)
def test_node_module_helper(script: str) -> None:
    result = subprocess.run(
        ["node", str(_REPO_ROOT / "tests" / "js" / script)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        f"{script} failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
