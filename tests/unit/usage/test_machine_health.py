"""Unit pin for the machine-health row's reading.

``app/webapp/static/machine-health.js``'s ``machineReading`` decides what the
row on the Board and Code tabs shows: the hub's verdict word, the worse of the
ports and commit percents, the tone, and "Not confirmed" (never a percent)
for a verdict that could not be read. Pinned without a browser: this module
shells out to plain Node to import the real ES module, in
``tests/js/shell/machine_health.test.mjs``. Machines without Node skip cleanly.
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
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "shell" / "machine_health.test.mjs"


def test_machine_health_reading():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "machine-health.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "machine_health ok" in result.stdout
