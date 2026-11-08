"""Unit pin for the client's reading of the Board's per-issue buckets.

``app/webapp/static/board/plan-buckets.js`` groups the chief's plan rows by
the bucket the server stamped on each Backlog issue, so the plan card's
Queued, Blocked and Umbrellas counts equal the Backlog's. Like
``test_swipe_row``, this shells out to plain Node to import the real ES module
and assert against it, in ``tests/js/plan_buckets.test.mjs``; machines without
Node skip cleanly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "plan_buckets.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_plan_rows_group_by_the_backlog_bucket():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "plan-buckets.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
