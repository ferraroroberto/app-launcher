"""The Jobs tab's run-status module against the server's declared machine.

``static/jobs/run-status.js`` is the browser's copy of ``RunStatus``
(``src/jobs/run_status.py``). This shells out to plain Node to import the real
ES module (``tests/js/job_run_status.test.mjs``), then checks its values are
exactly the server's. Machines without Node skip cleanly.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src.jobs.run_status import RunStatus
from src.subprocess_flags import NO_WINDOW

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "job_run_status.test.mjs"


def test_ui_refuses_kill_and_live_stream_for_a_finished_run() -> None:
    """A finished or unknown run is never live in the tab, and the tab's set is the server's.

    state-machine-proof: job-run-status-ui
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
        "run-status.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    lines = result.stdout.splitlines()
    assert lines[-1] == "OK"
    values = json.loads(next(line for line in lines if line.startswith("VALUES "))[len("VALUES "):])
    assert values == sorted(member.value for member in RunStatus)
