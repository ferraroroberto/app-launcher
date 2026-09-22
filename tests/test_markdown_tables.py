"""Unit pin for the shared markdown renderer's GFM tables (issue #1141).

``app/webapp/static/markdown.js`` had no table rule, so an agent's table fell
through to a paragraph and rendered as one run-on line of pipes in the Chat
pane and Life OS. The parser is pinned without a browser: this module shells
out to plain Node to import the real ES module, in
``tests/js/markdown.test.mjs`` (same pattern as ``test_terminal_keys_bytes``).
That works because the renderer imports only ``dom-utils.js``, which touches
no DOM or storage at import time. Machines without Node skip cleanly.
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
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "markdown.test.mjs"


def test_markdown_tables():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        "markdown.js table rendering failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
