"""Unit pin for the full-viewport repaint detector (#930).

``app/webapp/static/repaint-scrollback.js`` clears the terminal's scrollback
right after Claude Code's full-viewport repaint preamble, so the rows the
redraw repeats can't land in scrollback twice. Pinned without a browser: this
module shells out to plain Node to import the real ES module, in
``tests/js/repaint_scrollback.test.mjs`` (same pattern as
``test_markdown_tables``): the preamble split across socket messages at every
point, partial erases left alone, no byte ever held back. Machines without
Node skip cleanly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js is required"
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "repaint_scrollback.test.mjs"


def test_repaint_scrollback():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        "repaint-scrollback.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
