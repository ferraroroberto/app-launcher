"""A Backlog card's claim owner liveness, declared in ``static/board/claim-state.js``.

The test shells out to plain Node to import the real ES module
(``tests/js/board/claim_state.test.mjs``) and checks the browser's set is
exactly the verdicts ``src/board/active_issue_claims`` sends. Machines without
Node skip it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src.board import active_issue_claims
from src.subprocess_flags import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "board" / "claim_state.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_only_a_server_verdict_is_a_claim_state() -> None:
    """A value outside live / dead / unknown is refused (null), never read as a verdict.

    state-machine-proof: board-claim-state-ui
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
        "claim-state.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    lines = result.stdout.splitlines()
    assert lines[-1] == "OK"
    values = json.loads(next(line for line in lines if line.startswith("VALUES "))[len("VALUES "):])
    assert values == sorted(active_issue_claims._CLAIM_STATES)
