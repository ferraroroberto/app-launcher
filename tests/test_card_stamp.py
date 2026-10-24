"""Unit pin for the Board card's date stamp (issue #1638).

``dom-utils.js``'s ``fmtCardStamp`` gives the date on a Board card's second
line: a clock time for today, a short date for any other day, in the viewer's
local time. Pinned without a browser by shelling out to plain Node
(``tests/js/card_stamp.test.mjs``, same pattern as ``test_duration_days``).
Machines without Node skip cleanly.
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
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "card_stamp.test.mjs"


def test_card_stamp():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "card date stamp failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
