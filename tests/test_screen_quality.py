"""Unit pin for the Screen view's quality presets and readout (issue #1721).

``app/webapp/static/screen/screen-quality.js`` holds the preset ids, the remembered
choice and the readout's arithmetic (fps, kB/s, average frame size, ack to the
next frame) as pure functions and one small class. Like
``test_screen_reconnect``, this shells out to plain Node to import the real ES
module and assert against it, in ``tests/js/screen_quality.test.mjs``;
machines without Node skip cleanly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "screen_quality.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_preset_choice_and_the_readout_arithmetic():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "screen-quality.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
