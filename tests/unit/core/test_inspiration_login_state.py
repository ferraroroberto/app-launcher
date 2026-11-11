"""The X login window state, server and browser copy.

``src/inspiration/login.py`` declares the set; ``static/settings/login-state.js``
is the browser's copy. The UI test shells out to plain Node to import the real
ES module (``tests/js/settings/inspiration_login_state.test.mjs``) and checks its values
are exactly the server's. Machines without Node skip that test.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src.inspiration.login import LoginState, exit_state
from src.subprocess_flags import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "settings" / "inspiration_login_state.test.mjs"


def test_a_finished_helper_never_reads_as_open_and_outside_values_are_refused() -> None:
    """Only a clean exit closes the window; any other exit is a failure, never still running.

    state-machine-proof: inspiration-login-state
    """
    assert exit_state(0) is LoginState.CLOSED
    for code in (1, 2, -1, 3221225786):
        assert exit_state(code) is LoginState.FAILED
    for code in (0, 1):
        assert exit_state(code) not in (LoginState.IDLE, LoginState.RUNNING)
    with pytest.raises(ValueError):
        LoginState("open")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_ui_only_holds_the_button_for_a_running_window() -> None:
    """The sheet's open check and its value set are exactly the server's.

    state-machine-proof: inspiration-login-state-ui
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
        "login-state.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    lines = result.stdout.splitlines()
    assert lines[-1] == "OK"

    def printed(tag: str) -> list[str]:
        return json.loads(next(line for line in lines if line.startswith(tag + " "))[len(tag) + 1:])

    assert printed("VALUES") == sorted(member.value for member in LoginState)
    assert printed("OPEN") == [LoginState.RUNNING.value]
