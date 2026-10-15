"""Unit pin for the Screen view's reconnect decisions (issue #1603).

``app/webapp/static/screen-reconnect.js`` holds the backoff steps, their bound
and the remembered-monitor choice as pure functions. Like
``test_screen_gestures``, this shells out to plain Node to import the real ES
module and assert against it, in ``tests/js/screen_reconnect.test.mjs``;
machines without Node skip cleanly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "screen_reconnect.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_backoff_steps_and_the_remembered_monitor_choice():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "screen-reconnect.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
