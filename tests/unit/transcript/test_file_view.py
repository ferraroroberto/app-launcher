"""Unit pin for the Changed files panel's File view (issue #1689).

``app/webapp/static/changes/file-view.js`` draws a changed file for its type
(Markdown, JSON, code, plain text) and builds the Windows path Copy path
hands out. It is pinned without a browser: this module shells out to plain
Node to import the real ES module, in ``tests/js/file_view.test.mjs`` (same
pattern as ``test_markdown_tables``), including an inert ``<script>`` in every
kind. Machines without Node skip cleanly.
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
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "file_view.test.mjs"


def test_file_view():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "file-view.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
