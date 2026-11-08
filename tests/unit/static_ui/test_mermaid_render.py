"""Unit pin for the shared Mermaid renderer's pure parts (Settings > Documents).

``app/webapp/static/shared/mermaid-render.js`` imports no DOM at load, so the
block finder, the token-to-theme mapping and the width floor are pinned
without a browser: this module shells out to plain Node, which imports the
real ES module in ``tests/js/mermaid_render.test.mjs`` (same pattern as
``test_markdown_tables``). The drawing itself is covered in the browser suite.
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

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "mermaid_render.test.mjs"


def test_mermaid_render_pure_parts():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "mermaid-render.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
