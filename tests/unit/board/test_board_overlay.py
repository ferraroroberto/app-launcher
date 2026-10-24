"""Board backend: the transcript overlay, live-title status, idle-finished downgrade and claim contract."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path

import pytest

from app.webapp.routers import board as board_router
from src import active_issue_claims, board

from tests.unit.board._support import (
    NOW,
    _claim_contract,
    _iso,
    _live,
    _msg_line,
    _state_row,
    _tool_result_line,
    _transcript_file,
)


def test_overlay_needs_you_with_active_transcript_is_working(tmp_path: Path):
    """Resume paths fire no hook (#305): a transcript appended well after the
    row's stamp means Claude is working, whatever the last hook event said."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, NOW - timedelta(minutes=1))
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"
    # The age re-anchors to the transcript activity, not the stale hook stamp.
    assert cards[0]["age_seconds"] == 60


def test_overlay_idle_with_active_transcript_is_working(tmp_path: Path):
    row = _state_row("E:/x/y", status="idle", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, NOW - timedelta(minutes=1))
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"


def test_overlay_inside_stop_epsilon_keeps_needs_you(tmp_path: Path):
    """Stop's row stamp and the final transcript write land seconds apart (in
    either order) — inside the epsilon the hook status wins, so a genuine
    needs-you alert is never delayed. #608: the default fixture transcript
    is a plain text reply with nothing pending, so the split correctly reads
    it as a clean stop, not just an undifferentiated needs-you."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=5)
    )
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "idle-finished"


@pytest.mark.parametrize(
    ("live_title", "working"),
    [
        ("⠂ chief", True),  # a real observed Braille spinner frame
        ("⠐ Start work on issue 635", True),  # another Braille frame
        # #815: the CLI moved to the circle-quadrant rotation; both frames seen
        # live plus the two remaining frames of that 4-frame rotation.
        ("◐ Issue add", True),
        ("◑ Issue add", True),
        ("◒ x", True),
        ("◓ x", True),
        ("✳ Some Conversation Title", False),  # idle marker, between both families
        ("", False),
        (None, False),
        ("   ", False),
        ("app-launcher | gpt-5.5", False),  # Codex folder-echo title
        ("Some Title ⠂", False),  # the glyph must lead the title, not sit mid-string
    ],
)
def test_live_title_busy_glyph_decides_working(tmp_path: Path, live_title, working):
    """A needs-you row inside the resume epsilon reads idle-finished off its
    transcript; only a title that *leads* with a spinner frame, on a PTY that
    is still printing, flips the card to working (#631, #815)."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=3)
    )
    cards = board.merge_sessions(
        [_live(
            "aaa", "E:/x/y", 30,
            live_title=live_title,
            last_output_at=(NOW - timedelta(seconds=1)).timestamp(),
        )],
        {"t": row},
        now=NOW,
    )
    assert cards[0]["status"] == ("working" if working else "idle-finished")


def test_unrecognized_title_glyph_logs_one_breadcrumb_per_glyph(caplog):
    """#815's real lesson was that the fall-through is silent: the spinner
    changed, the override died, and nothing said so. An unrecognized glyph
    lead now leaves exactly one info line naming the codepoint — once per
    distinct glyph, since the Board re-polls every 5s — while ASCII titles
    and the known idle marker stay quiet, and the verdict is unchanged."""
    from src.board_transcript import (
        _LOGGED_UNKNOWN_TITLE_GLYPHS,
        _live_title_is_busy,
    )

    _LOGGED_UNKNOWN_TITLE_GLYPHS.clear()
    with caplog.at_level(logging.INFO, logger="src.board_transcript"):
        for _ in range(3):
            assert _live_title_is_busy("\u2699 future spinner") is False
        assert _live_title_is_busy("\u2733 chief") is False
        assert _live_title_is_busy("app-launcher | gpt-5.5") is False
        assert _live_title_is_busy("") is False
    breadcrumbs = [r for r in caplog.records if "unrecognized glyph" in r.getMessage()]
    assert len(breadcrumbs) == 1
    assert "U+2699" in breadcrumbs[0].getMessage()
    _LOGGED_UNKNOWN_TITLE_GLYPHS.clear()


def test_overlay_wedged_circle_spinner_title_does_not_force_working(tmp_path: Path):
    """#636's freshness gate must apply to the new glyph family too — a
    circle spinner frozen on a PTY that has gone quiet is stale chrome, not
    proof of a live turn, and still falls through to the transcript."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=3)
    )
    cards = board.merge_sessions(
        [_live(
            "aaa", "E:/x/y", 30,
            live_title="\u25d0 Issue add",
            last_output_at=(NOW - timedelta(minutes=5)).timestamp(),
        )],
        {"t": row},
        now=NOW,
    )
    assert cards[0]["status"] == "idle-finished"


def test_overlay_busy_live_title_overrides_stale_needs_you_inside_epsilon(tmp_path: Path):
    """#631: the classifier's tail-scan can land in the gap between a
    streamed text block and its own following tool_use block — landing
    inside the resume epsilon, with the transcript tail ending on a
    text-only assistant message, previously resolved to idle-finished even
    though a turn was genuinely still in progress. A busy live_title (the
    session's own OSC-title spinner glyph) is sourced from the live PTY, not
    this transcript file, so it short-circuits straight to working before
    the epsilon/tail-scan logic ever runs."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=3)
    )
    cards = board.merge_sessions(
        [_live("aaa", "E:/x/y", 30, live_title="\u2802 doing the thing")],
        {"t": row},
        now=NOW,
    )
    assert cards[0]["status"] == "working"
    assert cards[0]["age_seconds"] == 0


# ------------------------------------------- wedged-PTY freshness gate (#636)


def test_pty_output_is_fresh_boundaries():
    from src.board_transcript import _WEDGED_PTY_AFTER, _pty_output_is_fresh

    assert _pty_output_is_fresh(None, NOW) is True  # no signal -> trust the glyph
    assert _pty_output_is_fresh(0.0, NOW) is True  # never recorded, same as no signal
    fresh = (NOW - timedelta(seconds=1)).timestamp()
    assert _pty_output_is_fresh(fresh, NOW) is True
    at_threshold = (NOW - _WEDGED_PTY_AFTER).timestamp()
    assert _pty_output_is_fresh(at_threshold, NOW) is True  # inclusive boundary
    stale = (NOW - _WEDGED_PTY_AFTER - timedelta(seconds=1)).timestamp()
    assert _pty_output_is_fresh(stale, NOW) is False


def test_overlay_busy_live_title_with_fresh_last_output_at_still_overrides(tmp_path: Path):
    """#636 must not regress #631: a busy live_title backed by genuinely
    recent PTY output (last_output_at inside the wedged-PTY threshold) keeps
    short-circuiting straight to working, same fixture as #631's own
    regression test."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=3)
    )
    cards = board.merge_sessions(
        [_live(
            "aaa", "E:/x/y", 30,
            live_title="\u2802 doing the thing",
            last_output_at=(NOW - timedelta(seconds=1)).timestamp(),
        )],
        {"t": row},
        now=NOW,
    )
    assert cards[0]["status"] == "working"


def test_overlay_wedged_busy_live_title_does_not_force_working(tmp_path: Path):
    """A busy live_title frozen on a PTY that has gone genuinely quiet (#636,
    #627's remainder) must not be trusted at face value — same fixture as
    the #631 busy-title regression test above, but with a stale
    last_output_at, must fall through to the ordinary transcript-based
    outcome instead of blindly reporting working."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=3)
    )
    cards = board.merge_sessions(
        [_live(
            "aaa", "E:/x/y", 30,
            live_title="\u2802 doing the thing",
            last_output_at=(NOW - timedelta(minutes=5)).timestamp(),
        )],
        {"t": row},
        now=NOW,
    )
    assert cards[0]["status"] != "working"
    assert cards[0]["status"] == "idle-finished"


def test_overlay_wedged_busy_live_title_falls_through_to_stalled_dispatch(tmp_path: Path):
    """A wedged busy live_title on top of a genuinely stalled background
    dispatch (#608) must resolve to stalled, not working — the freshness
    gate falling through has to reach the existing stalled-dispatch check,
    not just any transcript-based outcome. Same transcript fixture as
    test_stalled_background_dispatch_over_threshold_is_stalled."""
    stamp_time = NOW - timedelta(minutes=40)
    launch_time = NOW - timedelta(minutes=35)
    content = _tool_result_line(
        launch_time, {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"}
    ) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=40)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions(
        [_live(
            "aaa", "E:/x/y", 45,
            live_title="\u2802 doing the thing",
            last_output_at=(NOW - timedelta(minutes=5)).timestamp(),
        )],
        {"t": row},
        now=NOW,
    )
    assert cards[0]["status"] == "stalled"


def test_idle_finished_downgraded_when_repo_has_active_issue(tmp_path: Path):
    """#627: a clean stop with nothing pending is only an *absence* of
    evidence, not proof the session's own work is done — the same shape a
    turn cut off mid-task leaves behind. When the session's repo still has
    an open issue workflow (fleet-config's active-issue marker, already
    fetched every poll for the Backlog column), that ``idle-finished`` claim
    is downgraded to the safe ``awaiting-input`` rather than asserted from
    silence alone."""
    row = _state_row("E:/automation/app-launcher", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=5)
    )
    cards = board.merge_sessions(
        [_live("aaa", "E:/automation/app-launcher", 30)],
        {"t": row},
        now=NOW,
        active_issue_repos={"app-launcher"},
    )
    assert cards[0]["status"] == "awaiting-input"


def test_idle_finished_stays_finished_without_matching_active_issue(tmp_path: Path):
    """The downgrade is repo-scoped, not a blanket suppression — a session in
    a repo with no open issue workflow (or none at all) still reports
    ``idle-finished`` as before."""
    row = _state_row("E:/automation/app-launcher", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=5)
    )
    cards = board.merge_sessions(
        [_live("aaa", "E:/automation/app-launcher", 30)],
        {"t": row},
        now=NOW,
        active_issue_repos={"some-other-repo"},
    )
    assert cards[0]["status"] == "idle-finished"

    cards_no_repos = board.merge_sessions(
        [_live("aaa", "E:/automation/app-launcher", 30)], {"t": row}, now=NOW,
    )
    assert cards_no_repos[0]["status"] == "idle-finished"


def test_idle_finished_downgrade_matches_worktree_sibling(tmp_path: Path):
    """A session built in an isolated sibling worktree (``<repo>-wt-<N>``,
    ``skills/_lib/worktree_claim.py``) must resolve to the same repo as its
    primary checkout, or its own still-open issue marker would never match."""
    row = _state_row(
        "E:/automation/app-launcher-wt-627", status="needs-you", updated_min_ago=10
    )
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=5)
    )
    cards = board.merge_sessions(
        [_live("aaa", "E:/automation/app-launcher-wt-627", 30)],
        {"t": row},
        now=NOW,
        active_issue_repos={"app-launcher"},
    )
    assert cards[0]["status"] == "awaiting-input"


def test_idle_finished_downgrade_applies_to_external_cards(tmp_path: Path):
    """The same repo-scoped downgrade applies to unmatched/external rows
    (:func:`src.board_sessions.merge_sessions`'s second, no-live-match loop),
    not just live-session-claimed cards. ``updated_min_ago=2`` keeps the
    transcript mtime inside both the #305 resume epsilon (so the status
    doesn't flip to ``working``) and #613's 5-minute external-liveness
    freshness window (so the row still renders as a card at all)."""
    row = _state_row("E:/automation/reporting", status="needs-you", updated_min_ago=2)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=2) + timedelta(seconds=3)
    )
    cards = board.merge_sessions(
        [], {"t": row}, now=NOW, active_issue_repos={"reporting"},
    )
    assert cards[0]["kind"] == "external"
    assert cards[0]["status"] == "awaiting-input"


def test_active_issue_repos_lowercases_and_dedupes():
    rows = {
        "App-Launcher#627": {"repo": "App-Launcher", "number": 627},
        "app-launcher#628": {"repo": "app-launcher", "number": 628},
        "reporting#12": {"repo": "reporting", "number": 12},
        "bad-row": {"number": 1},
    }
    assert board.active_issue_repos(rows) == frozenset({"app-launcher", "reporting"})


def test_classify_claims_maps_each_row_through_the_contract(tmp_path: Path):
    rows = {
        "a#1": {"repo": "a", "number": 1, "owner_session_id": "alive"},
        "a#2": {"repo": "a", "number": 2, "owner_session_id": "gone"},
        "a#3": {"repo": "a", "number": 3},
        "a#4": {"repo": "a", "number": 4, "owner_session_id": "raise"},
        "a#5": {"repo": "a", "number": 5, "owner_session_id": "bogus"},
    }
    states = active_issue_claims.classify_claims(
        rows, live_session_ids={"alive"}, fleet_config_dir=_claim_contract(tmp_path)
    )
    assert states == {
        "a#1": "live", "a#2": "dead", "a#3": "unknown",
        # A classifier that raises or answers outside the contract is unknown,
        # never folded into either verdict.
        "a#4": "unknown", "a#5": "unknown",
    }


def test_classify_claims_unreadable_session_list_is_unknown(tmp_path: Path):
    rows = {"a#2": {"repo": "a", "number": 2, "owner_session_id": "gone"}}
    states = active_issue_claims.classify_claims(
        rows, live_session_ids=None, fleet_config_dir=_claim_contract(tmp_path)
    )
    assert states == {"a#2": "unknown"}


def test_classify_claims_missing_contract_is_unknown_not_dead(tmp_path: Path, caplog):
    rows = {"a#2": {"repo": "a", "number": 2, "owner_session_id": "gone"}}
    with caplog.at_level(logging.WARNING):
        states = active_issue_claims.classify_claims(
            rows, live_session_ids=set(), fleet_config_dir=tmp_path / "no-fleet-config"
        )
    assert states == {"a#2": "unknown"}
    assert "claims unverified" in caplog.text


def test_mark_active_backlog_three_claim_states():
    columns = {"backlog": [
        {"repo": "App-Launcher", "number": n} for n in (1, 2, 3, 4, 5)
    ]}
    rows = {
        "app-launcher#1": {}, "app-launcher#2": {}, "app-launcher#3": {},
        "app-launcher#5": {},
    }
    claim_states = {
        "app-launcher#1": "live", "app-launcher#2": "dead",
        "app-launcher#3": "unknown",
        # #5 has a row but no verdict: unknown, never an unclaimed card.
    }
    board_router._mark_active_backlog(columns, rows, claim_states)
    seen = [(c["claim_state"], c["in_progress"]) for c in columns["backlog"]]
    assert seen == [
        ("live", True),
        ("dead", False),      # the owner is provably gone: a stale claim
        ("unknown", True),    # pre-#948 behaviour, flagged unverified
        (None, False),        # no claim at all
        ("unknown", True),
    ]


def test_active_issue_repos_drops_only_dead_claims():
    rows = {
        "a#1": {"repo": "a", "number": 1},
        "b#1": {"repo": "b", "number": 1},
        "c#1": {"repo": "c", "number": 1},
        "c#2": {"repo": "c", "number": 2},
    }
    states = {"a#1": "dead", "b#1": "unknown", "c#1": "dead", "c#2": "live"}
    assert board.active_issue_repos(rows, states) == frozenset({"b", "c"})


def test_dead_claim_no_longer_holds_finished_session_awaiting_input(tmp_path: Path):
    """#948 on #627's downgrade: a repo whose only claim is dead lets a
    genuinely finished session read idle-finished again."""
    row = _state_row("E:/automation/app-launcher", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=5)
    )
    live = [_live("aaa", "E:/automation/app-launcher", 30)]
    claims = {"app-launcher#9": {"repo": "app-launcher", "number": 9}}

    held = board.merge_sessions(
        live, {"t": dict(row)}, now=NOW,
        active_issue_repos=board.active_issue_repos(claims, {"app-launcher#9": "unknown"}),
    )
    released = board.merge_sessions(
        live, {"t": dict(row)}, now=NOW,
        active_issue_repos=board.active_issue_repos(claims, {"app-launcher#9": "dead"}),
    )
    assert held[0]["status"] == "awaiting-input"
    assert released[0]["status"] == "idle-finished"


def test_overlay_missing_transcript_keeps_hook_status(tmp_path: Path):
    """#608: a transcript path that can't be read gives no usable signal —
    the split must not misread that as "checked, nothing pending" and claim
    idle-finished; it falls back to the safe, generic value."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = str(tmp_path / "gone.jsonl")
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "awaiting-input"


def test_overlay_applies_to_external_cards(tmp_path: Path):
    row = _state_row("E:/automation/reporting", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, NOW - timedelta(minutes=1))
    cards = board.merge_sessions([], {"t": row}, now=NOW)
    assert cards[0]["kind"] == "external"
    assert cards[0]["status"] == "working"


def test_overlay_metadata_only_appends_keep_needs_you(tmp_path: Path):
    """#309: post-Stop metadata lines (system, pr-link, snapshots) advance the
    file mtime past the epsilon with no real resume — the tail probe sees the
    last conversation line is still pre-stamp and keeps the hook status.
    #608: the only real conversation line is a plain text reply with no
    tool_use pending, so the split correctly reads it as a clean stop."""
    stamp_time = NOW - timedelta(minutes=10)
    content = (
        _msg_line("assistant", stamp_time - timedelta(seconds=3)) + "\n"
        + json.dumps({"type": "system",
                      "timestamp": _iso(stamp_time + timedelta(minutes=2))}) + "\n"
        + json.dumps({"type": "pr-link", "url": "https://x"}) + "\n"
        + json.dumps({"type": "file-history-snapshot", "snapshot": {"f": 1}}) + "\n"
    )
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=1), content
    )
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "idle-finished"
    # Age still anchors to the hook stamp, not the metadata mtime.
    assert cards[0]["age_seconds"] == 600


def test_overlay_metadata_only_appends_keep_idle(tmp_path: Path):
    stamp_time = NOW - timedelta(minutes=10)
    content = (
        _msg_line("assistant", stamp_time - timedelta(seconds=3)) + "\n"
        + json.dumps({"type": "ai-title", "title": "t"}) + "\n"
    )
    row = _state_row("E:/x/y", status="idle", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=1), content
    )
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "idle"


def test_overlay_message_buried_under_metadata_still_flips(tmp_path: Path):
    """A real resume followed by metadata lines: the reverse walk skips the
    metadata and finds the conversation line, so the flip still happens."""
    resumed = NOW - timedelta(minutes=1)
    content = (
        _msg_line("user", resumed) + "\n"
        + json.dumps({"type": "file-history-snapshot", "snapshot": {}}) + "\n"
    )
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, resumed, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"
    assert cards[0]["age_seconds"] == 60


def test_overlay_malformed_tail_keeps_hook_status(tmp_path: Path):
    """Unparseable tail (torn write, junk) degrades to the hook status. #608:
    no line parsed at all is "no usable signal", not "checked, clean" — the
    split must not read this as idle-finished."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=1), "{torn line no json\nnot json either\n"
    )
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "awaiting-input"


# ---------------------------------------- attach_transcript_model (model pill)


def _assistant_line(model: str) -> str:
    return json.dumps({"type": "assistant", "message": {"model": model, "content": []}}) + "\n"


def _model_transcript(folder: Path, *lines: str) -> str:
    path = folder / "t.jsonl"
    path.write_text("".join(lines), encoding="utf-8")
    return str(path)


def _transcript_model_of(tmp_path: Path, *lines: str, agent: str = "claude"):
    row = _state_row("E:/x/y", transcript_path=_model_transcript(tmp_path, *lines))
    out = board.attach_transcript_model([_live("aaa", "E:/x/y", 10, agent=agent)], {"t": row})
    return out[0]["transcript_model"]


def test_transcript_model_is_the_newest_assistant_record(tmp_path: Path):
    """A resumed session, then an in-session /model switch: the last answer wins."""
    assert _transcript_model_of(
        tmp_path,
        _assistant_line("claude-opus-5-5"),
        json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n",
        _assistant_line("claude-sonnet-5-5"),
        json.dumps({"type": "system", "timestamp": "x"}) + "\n",
    ) == "claude-sonnet-5-5"


def test_transcript_model_skips_synthetic_and_torn_lines(tmp_path: Path):
    assert _transcript_model_of(
        tmp_path,
        _assistant_line("claude-opus-5-5"),
        _assistant_line("<synthetic>"),
        '{"type": "assistant", "message": {"model": "claude-fab',
    ) == "claude-opus-5-5"


def test_transcript_model_unknown_without_a_readable_answer(tmp_path: Path):
    """No assistant record yet, an empty file, a missing file, no row: unknown."""
    assert _transcript_model_of(tmp_path, json.dumps({"type": "user"}) + "\n") is None
    assert _transcript_model_of(tmp_path, "") is None
    gone = _state_row("E:/x/y", transcript_path=str(tmp_path / "missing.jsonl"))
    live = [_live("aaa", "E:/x/y", 10), _live("bbb", "E:/x/norow", 5)]
    for sess in board.attach_transcript_model(live, {"t": gone}):
        assert sess["transcript_model"] is None, sess["session_id"]


def test_transcript_model_reads_claude_only(tmp_path: Path):
    assert _transcript_model_of(tmp_path, _assistant_line("claude-opus-5-5"), agent="codex") is None


def test_attach_transcript_model_does_not_mutate_input():
    live = [_live("aaa", "E:/x/y", 30)]
    board.attach_transcript_model(live, {})
    assert "transcript_model" not in live[0]
