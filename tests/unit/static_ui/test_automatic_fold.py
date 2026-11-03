"""Unit pin for the Chat pane's fold of automatic turns.

``app/webapp/static/chat/automatic-fold.js`` decides which list items form a
folded group from their kinds alone, and keeps the per-session default. Like
``test_swipe_row``, this shells out to plain Node to import the real ES module,
in ``tests/js/automatic_fold.test.mjs``; machines without Node skip cleanly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "automatic_fold.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_groups_split_on_barriers_and_default_follows_the_chief():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "automatic-fold.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
