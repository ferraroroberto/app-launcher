"""Unit pin for Chat's send confirmation (issue #1744).

``sessions.js``'s ``sendOutcome`` maps the ``/input`` verdict to its toast, and
``send-confirm.js`` judges whether the agent took an unconfirmed send. Pinned
without a browser: this module shells out to plain Node to import the real ES
modules, in ``tests/js/send_confirm.test.mjs`` (same pattern as
``test_session_status``). Machines without Node skip cleanly.
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
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "send_confirm.test.mjs"


def test_send_outcome_mapping_and_evidence_judge():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "the send outcome mapping or evidence judge failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
