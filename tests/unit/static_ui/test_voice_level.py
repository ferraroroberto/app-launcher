"""Unit pin for the dictation input-level watch.

A recording whose mic carries no sound (iOS routing to a Bluetooth headset)
used to be noticed only at finish. ``app/webapp/static/voice-level.js`` now
judges the live stream within seconds. Like ``test_swipe_row``, this shells out
to plain Node to import the real ES module and assert against a fake Web Audio
in ``tests/js/voice_level.test.mjs``; machines without Node skip cleanly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "voice_level.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_silent_input_is_flagged_and_a_tone_is_not():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "voice-level.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
