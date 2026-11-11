"""A session background task's state, server and browser copy.

``src/session_view/task_state.py`` declares the phases and the one
transition rule; ``static/background-tasks/task-state.js`` is the browser's
copy. The UI test shells out to plain Node to import the real ES module
(``tests/js/session/bg_task_state.test.mjs``) and checks its values are the
server's. Machines without Node skip that test.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src.session_view.task_state import COMPLETED, TaskPhase, next_status, task_phase
from src.subprocess_flags import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "session" / "bg_task_state.test.mjs"


def test_a_finished_task_never_reopens_or_changes_status() -> None:
    """The first reported status is final; a status-less event leaves a task running.

    state-machine-proof: background-task-state
    """
    assert next_status(None, "") is None
    assert next_status(None, "failed") == "failed"
    assert next_status(None, "Not A Word!") == TaskPhase.FINISHED
    for later in ("", "completed", "running", "killed"):
        assert next_status("failed", later) == "failed"
    assert task_phase(None) is TaskPhase.RUNNING
    assert task_phase(TaskPhase.RUNNING.value) is TaskPhase.RUNNING
    for ended in ("failed", COMPLETED, TaskPhase.FINISHED.value):
        assert task_phase(ended) is TaskPhase.FINISHED
    with pytest.raises(ValueError):
        TaskPhase("completed")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_ui_shows_no_chip_only_for_running_or_success() -> None:
    """The sheet's chip rule uses exactly the server's phase values and success word.

    state-machine-proof: background-task-state-ui
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
        "task-state.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    lines = result.stdout.splitlines()
    assert lines[-1] == "OK"

    def printed(tag: str):
        return json.loads(next(line for line in lines if line.startswith(tag + " "))[len(tag) + 1:])

    assert printed("VALUES") == sorted(member.value for member in TaskPhase)
    assert printed("COMPLETED") == COMPLETED
