"""``PtySession.resize()`` defends the PTY from resize storms (issue #930).

Every PTY resize SIGWINCHes an inline agent (Claude Code) into a full-viewport
repaint, and the copy already scrolled into xterm's scrollback survives it —
so each resize can land a duplicate of the visible conversation. The phone
client settles its resize frames, but the WS frame and
``POST /sessions/{sid}/resize`` must not rely on that. Two server-side
defences, pinned here against a mock PTY:

- a size identical to the current one never reaches ``setwinsize``;
- a transient sub-viewport sample (the iOS keyboard sweep reports 1- and
  6-row sizes mid-animation) is clamped to a floor before it reaches the PTY.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from unittest.mock import MagicMock

from src import session_host
from src.session_host import PtySession
from src.vt_snapshot import VtSnapshot

_TERMINAL_JS = (
    Path(__file__).resolve().parents[1] / "app" / "webapp" / "static" / "terminal.js"
)


def _session(rows: int = 44, cols: int = 51, vt: bool = False) -> PtySession:
    return PtySession(
        session_id="sid-resize",
        project_dir=r"C:\stub",
        name="proj",
        flags="",
        started_at=time.time(),
        _loop=MagicMock(),
        _pty=MagicMock(),
        agent="codex" if vt else "claude",
        rows=rows,
        cols=cols,
        _vt=VtSnapshot(rows, cols) if vt else None,
    )


def test_unchanged_size_is_a_noop():
    session = _session(rows=44, cols=51)

    session.resize(44, 51)

    session._pty.setwinsize.assert_not_called()


def test_changed_size_reaches_the_pty():
    session = _session(rows=44, cols=51)

    session.resize(20, 51)

    session._pty.setwinsize.assert_called_once_with(20, 51)
    assert (session.rows, session.cols) == (20, 51)


def test_keyboard_sweep_only_resizes_on_real_changes():
    session = _session(rows=44, cols=51)

    for rows in (44, 20, 20, 44, 44):
        session.resize(rows, 51)

    assert session._pty.setwinsize.call_args_list == [
        ((20, 51),),
        ((44, 51),),
    ]


def test_sub_floor_sample_is_clamped_before_the_pty():
    session = _session(rows=44, cols=51)

    session.resize(1, 3)

    rows, cols = session._pty.setwinsize.call_args.args
    assert rows == session_host.PTY_MIN_ROWS
    assert cols == session_host.PTY_MIN_COLS
    assert rows >= 8 and cols >= 20
    assert (session.rows, session.cols) == (rows, cols)


def test_sub_floor_sample_at_the_floor_is_a_noop():
    session = _session(rows=44, cols=51)
    session.resize(1, 51)
    session._pty.setwinsize.reset_mock()

    # A second transient sample clamps to the same floored size — no SIGWINCH.
    session.resize(6, 51)

    session._pty.setwinsize.assert_not_called()


def test_client_floor_matches_the_session_host_floor():
    # The phone floors its frames client-side (terminal.js) and the host
    # clamps again here; two different floors would make the host resize the
    # PTY to a size the phone never asked for.
    js = _TERMINAL_JS.read_text(encoding="utf-8")
    for name in ("PTY_MIN_ROWS", "PTY_MIN_COLS"):
        match = re.search(rf"export const {name} = (\d+);", js)
        assert match, f"{name} not found in terminal.js"
        assert int(match.group(1)) == getattr(session_host, name), name


def test_noop_leaves_the_vt_mirror_untouched():
    session = _session(rows=30, cols=90, vt=True)
    session._vt = MagicMock()

    session.resize(30, 90)

    session._vt.resize.assert_not_called()
    session._pty.setwinsize.assert_not_called()
