"""A pending input entry's state, declared in ``static/chat/pending-state.js``.

The test shells out to plain Node to import the real ES module
(``tests/js/chat/pending_state.test.mjs``) and checks the browser's set is
exactly the places ``src/session_hosting/pending_input`` sends. Machines
without Node skip it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src.session_hosting import pending_input
from src.subprocess_flags import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "chat" / "pending_state.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_only_a_server_place_is_a_pending_state() -> None:
    """A value outside in_composer / queued / discarding is refused (null), never read as queued.

    state-machine-proof: chat-pending-state-ui
    """
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "pending-state.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    lines = result.stdout.splitlines()
    assert lines[-1] == "OK"
    values = json.loads(next(line for line in lines if line.startswith("VALUES "))[len("VALUES "):])
    assert values == sorted(
        [pending_input.PENDING_IN_COMPOSER, pending_input.PENDING_QUEUED, pending_input.PENDING_DISCARDING]
    )
