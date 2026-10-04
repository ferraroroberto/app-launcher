"""Unit pin for the Telegram sessions' context alert threshold (issue #1402).

``app/webapp/static/dom-utils.js``'s ``contextAlert`` decides whether a
context figure is high enough to raise the alert icon on the summary line and
to highlight a popup row's Compact button. Pinned without a browser: this
module shells out to plain Node to import the real ES module, in
``tests/js/context_alert.test.mjs`` (same pattern as ``test_quota_pace``).
Machines without Node skip cleanly.
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
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "context_alert.test.mjs"


def test_context_alert():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        "dom-utils.js contextAlert failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
