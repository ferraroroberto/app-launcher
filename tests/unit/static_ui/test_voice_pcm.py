"""Unit pin for dictation's PCM capture pieces (``shared/voice-pcm.js``, ``voice-wav.js``).

A 48 kHz or 44.1 kHz microphone must become 16 kHz samples of the right count
and level, and the WAV writers must produce a valid 16 kHz mono header, closed
for a finished take and open-ended for a streamed one. Pinned without a browser
in plain Node (``tests/js/shared/voice_pcm.test.mjs``); machines without Node
skip cleanly. The browser leg is ``tests/e2e/compose/test_dictation_wav.py``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "shared" / "voice_pcm.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_pcm_resampler_and_wav_writers() -> None:
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "voice-pcm.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ok" in result.stdout
