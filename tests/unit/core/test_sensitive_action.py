"""The shared sensitive-action wrapper (``static/shared/sensitive-action.js``).

Shells out to plain Node to import the real ES module
(``tests/js/shared/sensitive_action.test.mjs``): the pending toast fires before the
request answers, a repeat tap on the same target sends nothing, and a timeout
never reads as success. Machines without Node skip it.
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
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "shared" / "sensitive_action.test.mjs"


def test_sensitive_action_feedback_and_in_flight_guard() -> None:
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "sensitive-action.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert result.stdout.splitlines()[-1] == "OK"
