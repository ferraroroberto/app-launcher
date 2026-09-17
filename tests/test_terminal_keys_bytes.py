"""Unit pin for the keys-popover byte table (issue #986).

The key -> bytes mapping (arrows/Esc/Tab/Enter, Shift-modified, and the new
Ctrl-modified C/U/X) is pure data in
``app/webapp/static/terminal-keys-bytes.js``, extracted specifically so it
can be pinned without a browser. This module has no Python mirror — it
shells out to plain Node (present on this dev box and on GitHub's
``windows-2025`` runner image; no npm install needed) to import the real
ES module and assert against it, in ``tests/js/terminal_keys_bytes.test.mjs``.
Machines without Node skip cleanly, same pattern as the real-CLI PTY tests.
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
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "terminal_keys_bytes.test.mjs"


def test_terminal_keys_bytes_table():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        "terminal-keys-bytes.js mapping failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
