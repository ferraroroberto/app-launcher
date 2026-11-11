"""The Board job/CI attention card state, server and browser copy.

``src/board/attention_state.py`` declares the set; ``static/board/attention-state.js``
is the browser's copy. The UI test shells out to plain Node to import the real
ES module (``tests/js/board_attention_state.test.mjs``) and checks its values
and groupings are exactly the server's. Machines without Node skip that test.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src.board.attention_state import (
    DISMISSIBLE,
    FAILING,
    AttentionState,
    finished_run_state,
)
from src.jobs.outcome import OUTCOME_DEFERRED, OUTCOME_UNCONFIRMED
from src.subprocess_flags import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "board_attention_state.test.mjs"


def test_a_live_problem_is_never_dismissible_and_outside_values_are_refused() -> None:
    """Stuck and unreadable stay until their cause clears; only a finished run is a past event.

    state-machine-proof: board-attention-state
    """
    for live in (AttentionState.STUCK, AttentionState.UNREADABLE, AttentionState.UNKNOWN):
        assert live not in DISMISSIBLE
    assert AttentionState.UNKNOWN not in FAILING
    with pytest.raises(ValueError):
        AttentionState("running")
    assert finished_run_state(OUTCOME_UNCONFIRMED) is AttentionState.UNCONFIRMED
    for outcome in (None, "", "failed", OUTCOME_DEFERRED):
        assert finished_run_state(outcome) is AttentionState.FAILED


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_ui_never_counts_or_dismisses_a_state_the_server_does_not() -> None:
    """The Board's failing count and dismiss verb use exactly the server's groupings.

    state-machine-proof: board-attention-state-ui
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
        "attention-state.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    lines = result.stdout.splitlines()
    assert lines[-1] == "OK"

    def printed(tag: str) -> list[str]:
        return json.loads(next(line for line in lines if line.startswith(tag + " "))[len(tag) + 1:])

    assert printed("VALUES") == sorted(member.value for member in AttentionState)
    assert printed("FAILING") == sorted(member.value for member in FAILING)
    assert printed("DISMISSIBLE") == sorted(member.value for member in DISMISSIBLE)
