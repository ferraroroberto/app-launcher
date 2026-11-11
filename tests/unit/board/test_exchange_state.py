"""The Board drawer's last-exchange preview state, declared in ``static/board/exchange-state.js``.

The test shells out to plain Node to import the real ES module
(``tests/js/board/exchange_state.test.mjs``), checks the no-exchange reason
the drawer treats as Empty is the one ``src/board/exchange`` sends, and that
every state ``board.css`` styles is a declared one. Machines without Node skip it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from src.board import exchange
from src.subprocess_flags import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "board" / "exchange_state.test.mjs"
_BOARD_CSS = _REPO_ROOT / "app" / "webapp" / "static" / "styles" / "board.css"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_an_unavailable_or_failed_exchange_is_never_ready(tmp_path: Path) -> None:
    """Every answer maps to one of four states; unavailable or failed never reads as Ready.

    state-machine-proof: board-exchange-state
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
        "exchange-state.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    lines = result.stdout.splitlines()
    assert lines[-1] == "OK"

    def printed(tag: str):
        return json.loads(next(line for line in lines if line.startswith(tag + " "))[len(tag) + 1:])

    values = printed("VALUES")
    assert values == ["empty", "error", "loading", "ready"]
    # The reason the server gives when no source has an exchange yet.
    no_source = exchange._unavailable_reason({}, None, tmp_path / "absent.log")
    assert printed("NO_EXCHANGE") == no_source

    styled = set(re.findall(r'\.board-exchange\[data-state="([a-z]+)"\]', _BOARD_CSS.read_text(encoding="utf-8")))
    assert styled and styled <= set(values)
