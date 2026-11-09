"""Golden table: (raw hook stamp, evidence) -> Board card ``status`` and age.

Every branch of the card derivation (raw-stamp gate, transcript overlay,
needs-you split, plan-picker screen check, open-issue downgrade) is driven
through :func:`src.board.merge_sessions` for both a live and an external card,
with each piece of evidence stubbed at the function that reads it. The
expected table, ``golden_card_status.json``, was recorded from the derivation
before it was declared in one place; any change to a card's status or age for
any combination fails here. Regenerate only for a deliberate semantic change:
``python -m tests.unit.board.test_card_status_golden``.
"""

from __future__ import annotations

import itertools
import json
from datetime import timedelta
from pathlib import Path
from typing import Callable, Dict

from src import board
from src.board import sessions as board_sessions
from src.board import transcript as board_transcript

from tests.unit.board._support import NOW, _iso

_GOLDEN = Path(__file__).with_name("golden_card_status.json")

_UPDATED = NOW - timedelta(minutes=5)
_TRANSCRIPT = "golden-transcript.jsonl"

# Raw hook stamps: the three the writer emits, one it never does, and (live
# cards only) no state row at all.
_RAWS = ("working", "needs-you", "idle", "bogus", "<no-row>")
_TITLES = {
    "plain": ("", None),
    "busy-fresh": ("⠋ busy", NOW.timestamp()),
    "busy-stale": ("⠋ busy", (NOW - timedelta(minutes=1)).timestamp()),
}
# (transcript mtime, last conversation line) relative to the hook stamp.
_RESUMES = {
    "none": (_UPDATED, _UPDATED),
    "meta-only": (_UPDATED + timedelta(minutes=1), _UPDATED),
    "resumed": (_UPDATED + timedelta(minutes=1), _UPDATED + timedelta(minutes=1)),
}
_DISPATCHES = {
    "none": None,
    "recent": NOW - timedelta(minutes=1),
    "old": NOW - timedelta(hours=1),
    "unstamped": board_transcript._UNKNOWN_LAUNCH_STAMP,
}
_TOOLS = {
    "unreadable": None,
    "decision": {"AskUserQuestion"},
    "ordinary": {"Bash"},
    "nothing": set(),
}
_SCREENS = {"no-picker": False, "picker": True}
_REPOS = {"no-issue": frozenset(), "open-issue": frozenset({"golden"})}


def _row(raw: str) -> Dict[str, object]:
    return {
        "project": "golden",
        "status": raw,
        "transcript_path": _TRANSCRIPT,
        "cwd": "E:/golden",
        "updated_at": _iso(_UPDATED),
    }


def _one_card(setattr_: Callable[..., None], *, kind: str, raw: str, title: str,
              resume: str, dispatch: str, tools: str, screen: str, repo: str) -> str:
    mtime, activity = _RESUMES[resume]
    setattr_(board_transcript, "_transcript_mtime", lambda _p: mtime)
    setattr_(board_transcript, "_last_activity", lambda _p: activity)
    setattr_(board_transcript, "_pending_background_dispatch_launched_at",
             lambda _t: _DISPATCHES[dispatch])
    setattr_(board_transcript, "_pending_tool_use_names", lambda _t: _TOOLS[tools])
    setattr_(board_sessions, "_plan_picker_on_screen", lambda _s: _SCREENS[screen])
    setattr_(board_sessions, "_external_row_liveness", lambda *_a, **_k: (True, "golden"))
    if kind == "live":
        live_title, last_output_at = _TITLES[title]
        live = [{
            "session_id": "golden-live", "kind": "pty", "agent": "claude",
            "project_dir": "E:/golden", "name": "golden", "alive": True,
            "started_at": _iso(NOW - timedelta(minutes=30)),
            "live_title": live_title, "last_output_at": last_output_at,
            "prompt_title": "",
        }]
        rows = {} if raw == "<no-row>" else {
            "golden-row": {**_row(raw), "launcher_session_id": "golden-live"},
        }
    else:
        live, rows = [], {"golden-row": _row(raw)}
    cards = board.merge_sessions(live, rows, now=NOW, active_issue_repos=_REPOS[repo])
    assert len(cards) == 1, cards
    return f"{cards[0]['status']}@{cards[0]['age_seconds']}"


def build_table(setattr_: Callable[..., None]) -> Dict[str, str]:
    """Every combination, keyed ``kind|raw|title|resume|dispatch|tools|screen|repo``.

    External cards have no live title or screen, so those axes are fixed for
    them; ``<no-row>`` exists only for a live session.
    """
    table: Dict[str, str] = {}
    for kind in ("live", "external"):
        raws = _RAWS if kind == "live" else _RAWS[:-1]
        titles = list(_TITLES) if kind == "live" else ["plain"]
        screens = list(_SCREENS) if kind == "live" else ["no-picker"]
        for combo in itertools.product(raws, titles, _RESUMES, _DISPATCHES, _TOOLS, screens, _REPOS):
            raw, title, resume, dispatch, tools, screen, repo = combo
            key = "|".join((kind, *combo))
            table[key] = _one_card(
                setattr_, kind=kind, raw=raw, title=title, resume=resume,
                dispatch=dispatch, tools=tools, screen=screen, repo=repo,
            )
    return table


def test_golden_card_status_table(monkeypatch):
    expected = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    actual = build_table(monkeypatch.setattr)
    changed = {k: (expected.get(k), v) for k, v in actual.items() if expected.get(k) != v}
    assert not changed, f"{len(changed)} card(s) changed, e.g. {dict(list(changed.items())[:5])}"
    assert set(actual) == set(expected)


def test_golden_never_emits_the_raw_needs_you_stamp():
    expected = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    assert not [k for k, v in expected.items() if v.startswith("needs-you@")]


if __name__ == "__main__":
    import pytest

    patcher = pytest.MonkeyPatch()
    try:
        recorded = build_table(patcher.setattr)
    finally:
        patcher.undo()
    _GOLDEN.write_text(json.dumps(recorded, indent=0, sort_keys=True) + "\n", encoding="utf-8")
