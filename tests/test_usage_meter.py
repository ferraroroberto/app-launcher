"""Unit pin for the usage meter's reading and pace colour (issue #1433).

``app/webapp/static/usage-meter.js``'s ``meterReading`` / ``windowTone`` decide
what both tabs' meters show: accent under pace, attention ahead of pace,
danger at 90% or more, a stale reading dimmed with a chip, and unknown or
unsupported kept as text. Pinned without a browser: this module shells out to
plain Node to import the real ES module, in ``tests/js/usage_meter.test.mjs``
(same pattern as ``test_quota_pace``). Machines without Node skip cleanly.
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
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "usage_meter.test.mjs"


def test_usage_meter_reading():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        "usage-meter.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
