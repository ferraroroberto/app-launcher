"""Unit pin for the quota rows' weekly linear pace (issue #1330).

``app/webapp/static/dom-utils.js``'s ``quotaPace`` turns a weekly window's
``resets_at`` into the share of the window already elapsed, which the quota
rows print after the reset day. Pinned without a browser: this module shells
out to plain Node to import the real ES module, in
``tests/js/quota_pace.test.mjs`` (same pattern as ``test_markdown_tables``):
epoch and ISO resets, the clamp at both ends, the backend's window length
over the 7-day default, and no pace from a missing or past reset. Machines
without Node skip cleanly.
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
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "quota_pace.test.mjs"


def test_quota_pace():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        "dom-utils.js quotaPace failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
