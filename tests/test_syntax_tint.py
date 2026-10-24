"""Unit pin for the File view's token colours (issue #1749).

``app/webapp/static/syntax-tint.js`` colours a changed code file for the
Changed files panel's File view: a regex tokenizer per language family, escape
first, one balanced HTML string per source line. It is pinned without a
browser: this module shells out to plain Node to import the real ES module, in
``tests/js/syntax_tint.test.mjs`` (same pattern as ``test_file_view``): token
fixtures per language, the strip-and-unescape round trip, multi-line tokens, an
inert ``<script>`` in every family, the size cut-off and a linear-time check.
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
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "syntax_tint.test.mjs"


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
        "syntax-tint.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
