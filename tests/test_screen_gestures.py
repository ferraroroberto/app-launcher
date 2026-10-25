"""Unit pin for Screen control's gesture maths (issue #1521).

``app/webapp/static/screen/screen-gestures.js`` holds the letterbox + zoom mapping
and the tap / long-press / double-tap / pinch / scroll decisions as pure
functions, because Playwright's touch emulation only taps. Like
``test_swipe_row``, this shells out to plain Node to import the real ES module
and assert against it, in ``tests/js/screen_gestures.test.mjs``; machines
without Node skip cleanly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "screen_gestures.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_frame_mapping_gesture_thresholds_and_wheel_directions():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "screen-gestures.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
