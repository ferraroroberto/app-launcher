"""Unit pin for the shared clipboard write's http fallback.

``api.js``'s ``writeClipboard`` is the one copy every button uses. Pinned without
a browser by shelling out to plain Node (``tests/js/shared/write_clipboard.test.mjs``,
same pattern as ``test_ago_format``): ``writeText`` first, the selected-textarea
``execCommand`` copy when the page has no clipboard API, a rejection when both
fail. Machines without Node skip cleanly.
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
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "shared" / "write_clipboard.test.mjs"


def test_write_clipboard():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "writeClipboard failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
