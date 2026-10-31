"""Unit pin for the Rename sheet's pre-fill trim (``fitTitle`` in format.js).

A Life OS conversation's topic is often longer than the 120-character title
limit, so the pre-fill is cut at a word boundary. Like ``test_swipe_row``, this
shells out to plain Node to import the real ES module, in
``tests/js/fit_title.test.mjs``; machines without Node skip cleanly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "fit_title.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_fit_title_cuts_at_a_word_and_leaves_short_text_alone():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "fitTitle failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
