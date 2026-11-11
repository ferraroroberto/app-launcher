"""The declared Life OS title-sync verdict (src/life_os/title_sync.py) and its browser copy."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src.life_os.title_sync import TitleSync, title_sync
from src.subprocess_flags import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "title_sync.test.mjs"


@pytest.mark.parametrize(
    ("synced", "failed", "state"),
    [
        (0, 0, None),
        (1, 0, TitleSync.SYNCED),
        (3, 0, TitleSync.SYNCED),
        (0, 1, TitleSync.FAILED),
        (2, 1, TitleSync.FAILED),
    ],
)
def test_title_sync_table(synced, failed, state):
    assert title_sync(synced, failed) is state


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_ui_reads_only_a_failed_verdict_as_failed() -> None:
    """The phone toasts a stale name for ``failed`` alone, and its set is the server's.

    state-machine-proof: life-os-title-sync-ui
    """
    proc = subprocess.run(
        ["node", str(_TEST_SCRIPT)], capture_output=True, text=True, encoding="utf-8",
        cwd=_REPO_ROOT, timeout=60, creationflags=NO_WINDOW,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    values = next(line for line in proc.stdout.splitlines() if line.startswith("VALUES "))
    assert json.loads(values.removeprefix("VALUES ")) == sorted(m.value for m in TitleSync)
