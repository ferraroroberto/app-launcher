"""Board backend: pending-dispatch detection, refine_pending, the plan picker, stalled dispatch and column routing."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src import board
from src.board import transcript as board_transcript

from tests.unit.board._support import (
    NOW,
    _bash_tool_use_line,
    _iso,
    _live,
    _msg_line,
    _state_row,
    _tool_result_line,
    _tool_use_notification_line,
    _transcript_file,
)


def _task_notification_line(ts: datetime, task_id: str) -> str:
    """A completion notice in Claude Code's real ``queue-operation`` shape —
    invisible to ``_last_activity`` since it carries no ``message`` field.
    Real transcripts pair the ``enqueue`` with an almost-immediate ``dequeue``
    once Claude actually receives it (#601); every caller here wants the
    fully-delivered case, so that pairing is baked in — the enqueue-only,
    still-queued case has its own dedicated fixture below."""
    enqueue = json.dumps({
        "type": "queue-operation",
        "operation": "enqueue",
        "timestamp": _iso(ts),
        "content": (
            f"<task-notification>\n<task-id>{task_id}</task-id>\n"
            "<status>completed</status>\n</task-notification>"
        ),
    })
    dequeue = json.dumps({"type": "queue-operation", "operation": "dequeue", "timestamp": _iso(ts)})
    return enqueue + "\n" + dequeue


def test_overlay_pending_background_bash_is_waiting_on_task(tmp_path: Path):
    """A backgrounded Bash command Claude is still waiting on must not render
    as needs-you just because its own turn already ended — the parent
    transcript stays quiet the whole time it's running, so this must not
    depend on the mtime-past-stamp activity check."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _tool_result_line(
        stamp_time, {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"}
    ) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "waiting-on-task"


def test_overlay_pending_background_agent_is_waiting_on_task(tmp_path: Path):
    """Same as above for an async sub-agent (Agent/Task tool) dispatch."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _tool_result_line(
        stamp_time,
        {"isAsync": True, "status": "async_launched", "agentId": "agent1"},
    ) + "\n"
    row = _state_row("E:/x/y", status="idle", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "waiting-on-task"


def test_overlay_pending_dispatch_survives_large_intervening_tool_result(tmp_path: Path):
    """#594: a background dispatch's launch line can be pushed well past the
    old 8 KiB activity window by one large intervening tool result (e.g. a
    file Read) before the turn ends — reproduced from a live transcript where
    the launch line sat 11.8 KB from EOF. The wider ``_EXCHANGE_TAIL_BYTES``
    scan must still reach it."""
    stamp_time = NOW - timedelta(minutes=10)
    launch = _tool_result_line(
        stamp_time, {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"}
    ) + "\n"
    # Padding well past the old 8 KiB window but inside the new 256 KiB one.
    filler = (json.dumps({"type": "system", "content": "x" * 400}) + "\n") * 30
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, launch + filler)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "waiting-on-task"


def _turn_duration_line(ts: datetime, pending_agent_count: int) -> str:
    """Claude Code's own end-of-turn accounting line, emitted the moment a
    turn ends — the harness-native signal
    :func:`src.board.transcript._pending_background_dispatch_launched_at`
    falls back to when its id-keyed scan comes up empty (#627)."""
    return json.dumps({
        "type": "system",
        "subtype": "turn_duration",
        "durationMs": 1000,
        "pendingBackgroundAgentCount": pending_agent_count,
        "timestamp": _iso(ts),
    })


def test_overlay_pending_dispatch_beyond_tail_window_uses_turn_duration_count(tmp_path: Path):
    """#627, reproduced from a live transcript (project-scaffolding#199's
    worker): a background ``Agent`` dispatch's launch line sat ~299 KB from
    EOF by the time its turn ended — past even the widened 256 KiB
    ``_EXCHANGE_TAIL_BYTES`` window (#594 already widened it once, from 8 KB,
    for the same class of problem). The id-keyed scan below found nothing
    outstanding, so the row read as a clean stop (``idle-finished``) while the
    dispatch was still genuinely running — exactly the false-negative #627
    warns is worse than the five prior false-``needs-you`` reports it
    replaced. Claude Code's own ``turn_duration`` line reports
    ``pendingBackgroundAgentCount`` independent of any byte window (the real
    transcript's matching line read ``1`` at the same instant); the scan must
    fall back to it when the id-keyed search finds nothing."""
    stamp_time = NOW - timedelta(minutes=10)
    launch = _tool_result_line(
        stamp_time, {"isAsync": True, "status": "async_launched", "agentId": "agent1"}
    ) + "\n"
    # Padding well past the 256 KiB _EXCHANGE_TAIL_BYTES window.
    filler = (json.dumps({"type": "system", "content": "x" * 400}) + "\n") * 700
    final_text = _msg_line("assistant", stamp_time) + "\n"
    turn_duration = _turn_duration_line(stamp_time, 1) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, stamp_time, launch + filler + final_text + turn_duration
    )
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "waiting-on-task"


def test_overlay_turn_duration_zero_pending_does_not_block_idle_finished(tmp_path: Path):
    """#627: a ``turn_duration`` line reporting zero pending agents is not
    itself evidence of outstanding work — a genuinely clean stop must still
    read as idle-finished, turn_duration line or not."""
    stamp_time = NOW - timedelta(minutes=10)
    final_text = _msg_line("assistant", stamp_time) + "\n"
    turn_duration = _turn_duration_line(stamp_time, 0) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, stamp_time, final_text + turn_duration
    )
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "idle-finished"


def test_overlay_background_dispatch_notified_keeps_needs_you(tmp_path: Path):
    """Once the completion notice lands, a genuine needs-you-family status
    still wins — the pending check must not keep matching after the work
    actually finished. #608: nothing else is pending in this fixture, so the
    split reads it as a clean stop."""
    stamp_time = NOW - timedelta(minutes=10)
    content = (
        _tool_result_line(
            stamp_time - timedelta(minutes=1),
            {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"},
        ) + "\n"
        + _task_notification_line(stamp_time - timedelta(seconds=30), "btask1") + "\n"
    )
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "idle-finished"


def test_overlay_background_dispatch_enqueued_not_dequeued_is_waiting_on_task(tmp_path: Path):
    """#601: a live transcript (app-launcher#601, home-automation issue #321)
    ended with the notification's ``enqueue`` line as the very last thing
    written — no ``dequeue``/``remove`` followed it anywhere in the tail, so
    Claude had not actually received the result yet. The notification being
    merely *ready* (enqueued) must not resolve the dispatch — only being
    handed to Claude (dequeued) may."""
    stamp_time = NOW - timedelta(minutes=10)
    content = (
        _tool_result_line(
            stamp_time - timedelta(minutes=1),
            {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"},
        ) + "\n"
        + json.dumps({
            "type": "queue-operation",
            "operation": "enqueue",
            "timestamp": _iso(stamp_time - timedelta(seconds=30)),
            "content": (
                "<task-notification>\n<task-id>btask1</task-id>\n"
                "<status>completed</status>\n</task-notification>"
            ),
        }) + "\n"
    )
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "waiting-on-task"


def test_overlay_pending_bash_dispatch_without_background_task_id_is_waiting_on_task(tmp_path: Path):
    """A backgrounded Bash dispatch whose synchronous ack carries no
    ``backgroundTaskId`` (the real-world drift #576 found) must still be
    caught via the tool_use's own id, not just the legacy toolUseResult key."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _bash_tool_use_line(stamp_time, "toolu_abc") + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "waiting-on-task"


def test_overlay_pending_powershell_dispatch_is_waiting_on_task(tmp_path: Path):
    """#594: this repo's own agents background long-running commands (e.g.
    the verify-before-ship gate) via the ``PowerShell`` tool, not ``Bash`` —
    the tool-call-id-keyed scheme must recognize it too, not just Bash."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _bash_tool_use_line(stamp_time, "toolu_ps1", tool_name="PowerShell") + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "waiting-on-task"


def test_overlay_bash_dispatch_ack_alone_does_not_resolve_it(tmp_path: Path):
    """The synchronous 'launched in background' ack is an ordinary
    tool_result for the same tool_use_id — it must not be mistaken for
    completion, or the check would resolve the dispatch the instant it
    fires, defeating the whole point."""
    stamp_time = NOW - timedelta(minutes=10)
    content = (
        _bash_tool_use_line(stamp_time - timedelta(seconds=5), "toolu_abc") + "\n"
        + _tool_result_line(stamp_time, {"tool_use_id": "toolu_abc",
                                          "stdout": "Command running in background"}) + "\n"
    )
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "waiting-on-task"


def test_overlay_bash_dispatch_notified_by_tool_use_id_keeps_needs_you(tmp_path: Path):
    """Once the notification's ``<tool-use-id>`` tag resolves the dispatch,
    a genuine needs-you-family status still wins — mirrors the legacy
    scheme's own already-notified test, for the new id space. #608: the
    notification isn't a ``tool_result`` block, so the generic split's own
    scanner still sees the Bash call as unresolved — #813: that reads as
    ``tool-pending``, not a false idle-finished claim (a materially
    different, already-notified-and-genuinely-resolved concern, which is
    what keeps the dispatch itself out of ``working``/``stalled`` here)."""
    stamp_time = NOW - timedelta(minutes=10)
    content = (
        _bash_tool_use_line(stamp_time - timedelta(minutes=1), "toolu_abc") + "\n"
        + _tool_use_notification_line(
            stamp_time - timedelta(seconds=30), "btask1", "toolu_abc"
        ) + "\n"
    )
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "tool-pending"


def test_overlay_foreground_bash_tool_use_is_not_flagged_pending(tmp_path: Path):
    """A plain (non-backgrounded) Bash call must not trip the new check —
    only ``run_in_background: true`` marks a dispatch as pending. #608: the
    fixture never adds a resolving tool_result (out of scope for what this
    test targets), so the generic split's own scanner still sees it
    unresolved — #813: that reads as ``tool-pending`` rather than
    misreading an incomplete fixture as either a confirmed clean stop or a
    confirmed need for a human."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _bash_tool_use_line(
        stamp_time, "toolu_abc", run_in_background=False
    ) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "tool-pending"


# --------------------------------------- needs-you four-way split (#608)


def _tool_use_line(ts: datetime, tool_use_id: str, name: str) -> str:
    """One assistant line dispatching an arbitrary foreground tool call —
    the generic counterpart to :func:`_bash_tool_use_line`, for exercising
    :func:`src.board.transcript._refine_waiting_status`'s own tail scan
    rather than the background-dispatch-specific one."""
    return json.dumps({
        "type": "assistant",
        "timestamp": _iso(ts),
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_use_id, "name": name, "input": {}}],
        },
    })


def _generic_tool_result_line(ts: datetime, tool_use_id: str) -> str:
    """A plain, ordinary ``tool_result`` block for ``tool_use_id`` — the
    standard Anthropic content-block shape (no ``toolUseResult`` sibling),
    resolving whatever :func:`_tool_use_line` launched."""
    return json.dumps({
        "type": "user",
        "timestamp": _iso(ts),
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}],
        },
    })


def test_refine_pending_ask_user_question_is_awaiting_decision(tmp_path: Path):
    """A pending AskUserQuestion — Claude is blocked on a human picking an
    option, not just "needs a prompt"."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _tool_use_line(stamp_time, "toolu_q1", "AskUserQuestion") + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "awaiting-decision"


def _count_exchange_tail_reads(monkeypatch) -> list:
    """Record every :data:`_EXCHANGE_TAIL_BYTES`-window read (#881)."""
    reads: list = []
    real = board_transcript._read_tail_bytes

    def counting(path, n_bytes):
        if n_bytes == board_transcript._EXCHANGE_TAIL_BYTES:
            reads.append(path)
        return real(path, n_bytes)

    monkeypatch.setattr(board_transcript, "_read_tail_bytes", counting)
    return reads


def test_waiting_card_reads_its_exchange_tail_once(tmp_path: Path, monkeypatch):
    """#881: the overlay's pending-dispatch scan and the refine step's
    pending-tool scan walk the same 256 KB window of the same transcript.
    They used to read and parse it once each, on every 5s poll, for every
    ``needs-you`` card; now they share one :class:`_ExchangeTail`."""
    reads = _count_exchange_tail_reads(monkeypatch)
    stamp_time = NOW - timedelta(minutes=10)
    content = _tool_use_line(stamp_time, "toolu_q1", "AskUserQuestion") + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "awaiting-decision"
    assert len(reads) == 1, f"expected one shared exchange-tail read, got {len(reads)}"


def test_busy_card_never_reads_its_exchange_tail(tmp_path: Path, monkeypatch):
    """The shared tail is lazy (#881): a fresh busy live title short-circuits
    the overlay to ``working`` and the refine step passes ``working`` through,
    so a card that needs neither scan must not pay for the read at all."""
    reads = _count_exchange_tail_reads(monkeypatch)
    stamp_time = NOW - timedelta(minutes=10)
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time)
    live = _live(
        "aaa", "E:/x/y", 30, live_title="◐ Working", last_output_at=NOW.timestamp()
    )
    cards = board.merge_sessions([live], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"
    assert reads == []


def test_refine_pending_exit_plan_mode_is_awaiting_decision(tmp_path: Path):
    """A pending ExitPlanMode is the same kind of human-decision block as
    AskUserQuestion — approve or revise a plan."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _tool_use_line(stamp_time, "toolu_p1", "ExitPlanMode") + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "awaiting-decision"


# ------------------------------------------ plan picker on screen (#1203)


@pytest.fixture
def picker_screen(tmp_path: Path, monkeypatch):
    """Each live session's PTY capture in ``tmp_path``, plus a counter of
    screen renders. ``show(sid)`` writes the plan picker as the session-host
    writes a capture (text mode); ``show(sid, picker=False)`` writes an
    ordinary prompt instead."""
    from src import audit
    from src.board import sessions as board_sessions
    from src.session_view import plan_picker
    from tests.test_plan_picker import PLAN_MODE, _screen

    monkeypatch.setattr(audit, "transcript_path", lambda sid: tmp_path / f"{sid}.transcript")
    monkeypatch.setattr(board_sessions, "_PICKER_SCREEN_CACHE", {}, raising=False)
    renders: list = []
    real = plan_picker.screen_lines

    def counting(text, rows, cols):
        renders.append((rows, cols))
        return real(text, rows, cols)

    monkeypatch.setattr(plan_picker, "screen_lines", counting)

    def show(sid: str, picker: bool = True) -> None:
        lines = _screen(PLAN_MODE) if picker else ["\u276f ", "", "  ? for shortcuts"]
        with (tmp_path / f"{sid}.transcript").open("w", encoding="utf-8") as fh:
            fh.write("\r\n".join(lines))

    return show, renders


def _clean_stop_row(tmp_path: Path) -> dict:
    """A ``needs-you`` row whose transcript tail has nothing pending — what
    a session on the picker looks like while Claude keeps the ExitPlanMode
    call off disk (#1151, #1203)."""
    stamp_time = NOW - timedelta(minutes=10)
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time)
    return row


def test_plan_picker_on_screen_is_awaiting_decision_without_the_call_on_disk(
    tmp_path: Path, picker_screen
):
    """#1203: the tail alone reads a clean stop; the screen shows the plan
    picker, so the card is awaiting decision in Your turn."""
    show, _renders = picker_screen
    show("aaa")
    live = _live("aaa", "E:/x/y", 30, rows=40, cols=60)
    cards = board.merge_sessions([live], {"t": _clean_stop_row(tmp_path)}, now=NOW)
    assert cards[0]["status"] == "awaiting-decision"
    columns = board.build_board(cards, {}, [])
    assert [c["session_id"] for c in columns["your_turn"]] == ["aaa"]


@pytest.mark.parametrize("case", ["no-picker", "no-capture", "detached", "dead", "working"])
def test_plan_picker_check_never_forces_a_status(tmp_path: Path, picker_screen, case):
    """Guard: without a readable picker on a live launcher PTY the card keeps
    its transcript answer. A detached session has no screen to read, a dead
    one's last frame proves nothing, and a ``working`` card isn't checked."""
    show, renders = picker_screen
    row = _clean_stop_row(tmp_path)
    extra = {}
    if case == "no-picker":
        show("aaa", picker=False)
    elif case == "detached":
        show("aaa")
        extra["kind"] = "remote"
    elif case == "dead":
        show("aaa")
        extra["alive"] = False
    elif case == "working":
        show("aaa")
        row["status"] = "working"
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30, **extra)], {"t": row}, now=NOW)
    assert cards[0]["status"] == ("working" if case == "working" else "idle-finished")
    if case != "no-picker":
        assert renders == []


def test_plan_picker_screen_is_rendered_only_when_the_capture_changes(
    tmp_path: Path, picker_screen
):
    """The render is the per-poll cost (#1203): an unchanged capture is
    answered from the cache, a changed one is read again, and a session that
    leaves the live list leaves the cache."""
    show, renders = picker_screen
    show("aaa")
    row = _clean_stop_row(tmp_path)
    live = [_live("aaa", "E:/x/y", 30)]
    for _ in range(3):
        cards = board.merge_sessions(live, {"t": row}, now=NOW)
        assert cards[0]["status"] == "awaiting-decision"
    assert len(renders) == 1
    show("aaa", picker=False)
    capture = tmp_path / "aaa.transcript"
    later = capture.stat().st_mtime + 5
    os.utime(capture, (later, later))
    cards = board.merge_sessions(live, {"t": row}, now=NOW)
    assert cards[0]["status"] == "idle-finished"
    assert len(renders) == 2
    board.merge_sessions([], {"t": row}, now=NOW)
    # Back on the list with the same capture: a forgotten session is read
    # afresh, so the answer cannot be a stale one left from its last stay.
    cards = board.merge_sessions(live, {"t": row}, now=NOW)
    assert cards[0]["status"] == "idle-finished"
    assert len(renders) == 3


def test_refine_resolved_ask_user_question_is_idle_finished(tmp_path: Path):
    """Once the decision tool_use resolves (a real tool_result lands), the
    session isn't blocked on a decision anymore — nothing else is pending,
    so it reads as a clean stop."""
    stamp_time = NOW - timedelta(minutes=10)
    content = (
        _tool_use_line(stamp_time - timedelta(seconds=5), "toolu_q1", "AskUserQuestion") + "\n"
        + _generic_tool_result_line(stamp_time, "toolu_q1") + "\n"
    )
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "idle-finished"


def test_refine_pending_unrecognized_tool_use_is_tool_pending(tmp_path: Path):
    """A pending tool_use that isn't a decision tool and isn't a background
    dispatch (e.g. a permission-gated Read waiting on approval, or a plain
    Read still executing) is genuinely ambiguous between the two — #813
    reports it as its own ``tool-pending`` state rather than folding it into
    ``awaiting-input`` ("needs you"), which over-claimed a human was needed
    on every ordinary in-flight tool call."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _tool_use_line(stamp_time, "toolu_r1", "Read") + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "tool-pending"


def test_refine_pending_bash_tool_use_is_tool_pending_not_needs_you(tmp_path: Path):
    """Regression (#813): the real observed timeline — a foreground Bash
    tool_use pending with no tool_result, and the needs-you stamp newer than
    the tool_use line's own timestamp (mirroring a long-running command
    during which the transcript writes nothing new until it returns). Before
    the fix this rendered ``awaiting-input`` ("needs you") mid-turn; it must
    now render ``tool-pending`` and route to Claude's turn, not Your turn."""
    tool_use_time = NOW - timedelta(minutes=12)
    content = _tool_use_line(tool_use_time, "toolu_b1", "Bash") + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, tool_use_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "tool-pending"
    columns = board.build_board(cards, {}, [])
    assert columns["your_turn"] == []
    assert [c["session_id"] for c in columns["claude_turn"]] == ["aaa"]


def test_refine_only_applies_to_needs_you_not_idle(tmp_path: Path):
    """The split is scoped to needs-you — a plain ``idle`` row with a pending
    decision tool_use must not be reclassified; idle isn't part of #608."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _tool_use_line(stamp_time, "toolu_q1", "AskUserQuestion") + "\n"
    row = _state_row("E:/x/y", status="idle", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "idle"


# --------------------------------------------- stalled threshold (#608)


def test_stalled_background_dispatch_under_threshold_stays_waiting_on_task(tmp_path: Path):
    """A background dispatch outstanding 10 minutes is healthy waiting, not
    a stall — mirrors the fleet chief's own observed ~9-minute e2e-gate wait
    that must never misread as stalled."""
    stamp_time = NOW - timedelta(minutes=25)
    launch_time = NOW - timedelta(minutes=10)
    content = _tool_result_line(
        launch_time, {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"}
    ) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=25)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "waiting-on-task"


def test_stalled_background_dispatch_over_threshold_is_stalled(tmp_path: Path):
    """A background dispatch outstanding 35 minutes (past the 30-minute
    threshold) is a genuine anomaly worth surfacing distinctly from healthy
    waiting."""
    stamp_time = NOW - timedelta(minutes=40)
    launch_time = NOW - timedelta(minutes=35)
    content = _tool_result_line(
        launch_time, {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"}
    ) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=40)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 45)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "stalled"


def test_stalled_dispatch_with_unparseable_launch_stamp_stays_waiting_on_task(tmp_path: Path):
    """A dispatch with no parseable launch timestamp is genuinely
    outstanding but not age-able — per explicit direction, a low-confidence
    stalled call is worse than a late one, so this must not be guessed as
    stalled just because the sentinel age looks ancient."""
    stamp_time = NOW - timedelta(minutes=40)
    content = json.dumps({
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_x", "content": "ack"}],
        },
        "toolUseResult": {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"},
        # No "timestamp" field at all — _parse_iso(None) is None.
    }) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=40)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 45)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "waiting-on-task"


def test_stalled_status_stays_stalled_for_idle_hook_status(tmp_path: Path):
    """The stalled check only fires for a ``needs-you`` row — an ``idle``
    row with the same outstanding dispatch keeps the #464 pre-#608
    behavior (always ``working``), since idle is out of the four-way
    split's scope."""
    stamp_time = NOW - timedelta(minutes=40)
    launch_time = NOW - timedelta(minutes=35)
    content = _tool_result_line(
        launch_time, {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"}
    ) + "\n"
    row = _state_row("E:/x/y", status="idle", updated_min_ago=40)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 45)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "waiting-on-task"


# ---------------------------------------------- build_board routing (#608)


def test_build_board_idle_finished_routes_to_claude_turn():
    """idle-finished isn't an alert — it belongs in Claude's turn alongside
    working/idle/unknown, not Your turn."""
    cards = [{"session_id": "s1", "status": "idle-finished", "label": ""}]
    columns = board.build_board(cards, {}, [])
    assert columns["your_turn"] == []
    assert [c["session_id"] for c in columns["claude_turn"]] == ["s1"]


def test_build_board_tool_pending_routes_to_claude_turn():
    """tool-pending (#813) isn't an alert either — genuinely ambiguous
    between "still executing" and "permission-gated", so it stays out of
    Your turn the same way idle-finished does."""
    cards = [{"session_id": "s1", "status": "tool-pending", "label": ""}]
    columns = board.build_board(cards, {}, [])
    assert columns["your_turn"] == []
    assert [c["session_id"] for c in columns["claude_turn"]] == ["s1"]


def test_build_board_needs_you_family_routes_to_your_turn():
    """stalled / awaiting-decision / awaiting-input all still mean "a human
    needs to look at this" and route to Your turn."""
    cards = [
        {"session_id": "s-stalled", "status": "stalled", "label": ""},
        {"session_id": "s-decision", "status": "awaiting-decision", "label": ""},
        {"session_id": "s-input", "status": "awaiting-input", "label": ""},
    ]
    columns = board.build_board(cards, {}, [])
    assert {c["session_id"] for c in columns["your_turn"]} == {
        "s-stalled", "s-decision", "s-input",
    }
    assert columns["claude_turn"] == []
