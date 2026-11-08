"""Unit pin for the History sheet's reading of ``/api/progress``.

``app/webapp/static/progress/progress-model.js`` works out the totals, the
chart series, the per-repo and per-agent rows and the words a figure carries
when it was not measured (unknown, never zero). Pinned without a browser:
this module shells out to plain Node to import the real ES module, in
``tests/js/progress_model.test.mjs``. Machines without Node skip cleanly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "progress_model.test.mjs"


def test_progress_model() -> None:
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "progress-model.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
