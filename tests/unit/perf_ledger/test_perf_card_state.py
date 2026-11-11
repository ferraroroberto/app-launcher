"""The Apps tab Performance card's read state, declared in ``static/apps/perf-card-state.js``.

The test shells out to plain Node to import the real ES module
(``tests/js/apps/perf_card_state.test.mjs``), and checks the ledger value the
card treats as unreadable is the one ``src/perf_ledger`` sends. Machines
without Node skip it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src import perf_ledger
from src.subprocess_flags import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "apps" / "perf_card_state.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_an_unreadable_or_failed_read_is_never_ok() -> None:
    """Every read maps to one of three states; unreadable or failed never reads as OK.

    state-machine-proof: apps-perf-card-state
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
        "perf-card-state.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    lines = result.stdout.splitlines()
    assert lines[-1] == "OK"

    def printed(tag: str):
        return json.loads(next(line for line in lines if line.startswith(tag + " "))[len(tag) + 1:])

    assert printed("VALUES") == ["loading", "ok", "unknown"]
    assert printed("UNREADABLE") == perf_ledger.LEDGER_UNREADABLE
