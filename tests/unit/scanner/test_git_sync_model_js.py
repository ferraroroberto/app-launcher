"""Unit pin for how the Sync all dialog reads a run.

``app/webapp/static/apps/git-sync-model.js`` words the progress line, counts the
outcomes, builds the closing summary and orders the report. Pinned without a
browser: this module shells out to plain Node to import the real ES module, in
``tests/js/git_sync_model.test.mjs``. Machines without Node skip cleanly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "apps" / "git_sync_model.test.mjs"


def test_git_sync_model() -> None:
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "git-sync-model.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
