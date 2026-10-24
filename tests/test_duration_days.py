"""Unit pin for the days rollover in the duration formatters (issue #1523).

``dom-utils.js``'s ``fmtDuration`` (granular mode, the Coding tab's session
age) and ``jobs-row.js``'s ``formatDuration`` (the Jobs tab's run duration)
used to keep counting hours past a day ("268h 12m"); they now read "11d 4h".
Pinned without a browser: this module shells out to plain Node to import the
real ES modules, in ``tests/js/duration_days.test.mjs`` (same pattern as
``test_context_alert``). Machines without Node skip cleanly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from src.subprocess_flags import NO_WINDOW

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js is required"
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "duration_days.test.mjs"


def test_duration_days():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "duration formatters failed their Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
