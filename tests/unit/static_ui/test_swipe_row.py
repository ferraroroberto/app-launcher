"""Unit pin for the swipe gesture's maths (issue #1503).

Life's skill rows swipe (``app/webapp/static/swipe-row.js``): the thresholds,
the angle lock, the edge zone and the commit/cancel decision are pure
functions, because Playwright's touch emulation only taps and cannot judge a
swipe. Like ``test_attach_batch``, this shells out to plain Node to import the
real ES module and assert against it, in ``tests/js/swipe_row.test.mjs``;
machines without Node skip cleanly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "swipe_row.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_swipe_thresholds_angle_lock_edge_and_release():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "swipe-row.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
