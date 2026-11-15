"""Unit pin for dictation's silence auto-stop detector (``voice-autostop.js``).

The detector is a pure function of level readings over time, so the pause
rules (only after speech, only once, against the room's own level) are pinned
in plain Node, in ``tests/js/shared/voice_autostop.test.mjs``; machines without Node
skip cleanly. The browser leg is in ``tests/e2e/compose/test_voice_silent_input.py``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "shared" / "voice_autostop.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_pause_after_speech_stops_once_against_the_room_level():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "voice-autostop.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
