"""Unit pin for the shared ago, byte and plural formatters.

``shared/format.js`` owns the one "N min ago" ladder, the byte rendering and
the count-with-noun phrase every view imports. Pinned without a browser by
shelling out to plain Node (``tests/js/shared/ago_format.test.mjs``, same pattern as
``test_duration_days``). Machines without Node skip cleanly.
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

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "shared" / "ago_format.test.mjs"


def test_ago_format():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "shared formatters failed their Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
