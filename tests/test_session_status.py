"""Unit pin for the shared session "status · age" formatter (issue #1580).

``glance.js``'s ``sessionStatusBits`` is the one function the Board's session
rows and the Code tab's rows print their status and last-activity age through.
Pinned without a browser: this module shells out to plain Node to import the
real ES module, in ``tests/js/session_status.test.mjs`` (same pattern as
``test_duration_days``). Machines without Node skip cleanly.
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

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "session_status.test.mjs"


def test_session_status_formatter():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "the shared session status formatter failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
