"""A send's submit fate, declared in ``static/chat/submit-state.js``.

The test shells out to plain Node to import the real ES module
(``tests/js/chat/submit_state.test.mjs``) and checks the browser's set is
exactly the ``SUBMIT_*`` values ``src/session_host_input`` sends. Machines
without Node skip it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src import session_host_input
from src.subprocess_flags import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "chat" / "submit_state.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_only_a_server_submit_fate_is_a_submit_state() -> None:
    """A value outside the session-host's SUBMIT_* set is refused (null), never read as a fate.

    state-machine-proof: send-submit-state-ui
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
        "submit-state.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    lines = result.stdout.splitlines()
    assert lines[-1] == "OK"
    values = json.loads(next(line for line in lines if line.startswith("VALUES "))[len("VALUES "):])
    server = sorted(
        value for name, value in vars(session_host_input).items() if name.startswith("SUBMIT_")
    )
    assert values == server
