"""Transcript-JSONL parsing for the Board tab (issue #408 split of ``board.py``).

Claude Code's transcript files are the ground truth for three things the hook
state file can't tell you on its own:

* whether a session that's stamped ``needs-you``/``idle`` has actually
  resumed working without any hook firing (:func:`_transcript_overlay`,
  #305/#309), and whether an unmatched hook row has recent transcript
  activity that independently proves an external process still exists
  (:func:`_external_row_liveness`, #322/#455);
* whether a session stamped ``needs-you``/``idle`` is actually still waiting
  on its *own* backgrounded work — a background sub-agent or shell dispatch
  it hasn't heard back from yet
  (:func:`_pending_background_dispatch_launched_at`, #464, hardened by #576
  and #601 — see that function's docstring for why the original
  ``toolUseResult``-keyed check alone isn't reliable);
* the last completed user→assistant exchange, for the drill-down drawer
  (:func:`src.board.transcript_exchange.last_exchange`, #301).

All read only a bounded tail of the transcript (never the whole file — a
long session's JSONL can run to many MB) and degrade to "no signal" on any
IO/parse error; callers keep whatever status/state they already had.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AbstractSet, Any, Dict, List, Optional, Tuple

from src._log_once import log_once
from src.board.dispatch_ids import (
    _launched_background_ids,
    _launched_bash_dispatch_ids,
    _launched_monitor_use_ids,
    _monitor_ack,
    _monitor_notification,
    _notified_background_ids,
    _notified_bash_dispatch_ids,
)
from src.board.state import _now, _parse_iso

logger = logging.getLogger(__name__)

# Transcript-activity overlay: a transcript appended this much later
# than the row's stamp means Claude resumed without any hook firing. The
# margin absorbs the Stop hook and the final transcript write landing a
# couple of seconds apart in either order.
_RESUME_EPSILON = timedelta(seconds=10)

# External-row liveness: a hook
# state is semantic evidence, not proof that its process still exists. Only
# recent transcript activity lets an unmatched row render as an external
# session. Missing cloud/bridge transcripts and quiet waiting rows otherwise
# linger for 24 hours. The window is 5 minutes, tight enough that a stale
# ghost row still clears quickly; this is still
# an inherently imperfect fallback (a process that writes once and exits
# within the window is indistinguishable from one still running), not a fix
# for that class — the two deterministic checks in
# :func:`_external_row_liveness` (reaped launcher session, claimed
# transcript) are the real fix and run first.
_EXTERNAL_ACTIVITY_AFTER = timedelta(minutes=5)

# Live-title busy override: Claude Code prefixes its own OSC
# window title with a brand glyph that doubles as a busy/idle marker — an
# animated spinner glyph while a turn is genuinely in progress, "*" (U+2733)
# once it returns to an idle prompt. Verified live against real sessions on
# this box: the glyph stays a spinner throughout text generation *and* tool
# dispatch, only flipping to the idle marker once control returns to the
# user — so it is immune to the race where the tail scan landing in the ~1.8s
# gap between a streamed text block and its own following tool_use block),
# which is entirely a transcript-file phenomenon this field never touches.
#
# Two spinner families are recognized: Braille Patterns (U+2800-U+28FF) and
# the circle-quadrant rotation "◐◓◑◒" (U+25D0-U+25D3). Both must stay
# recognized together — an unrecognized glyph is designed to fall through
# silently, so a CLI that switches spinner glyph without this set covering
# the new one makes the whole override go inert with no error and no
# failing test: every live working session reads as not-busy and the Board
# misreads a running tool call as needing the human. "◓"/"◒" are the
# remaining frames of the circle-quadrant rotation, included as a
# deliberate superset of what has been directly observed; the Braille
# range is kept for older CLIs and costs nothing to retain.
#
# This is undocumented CLI UI chrome, not a versioned contract, so a title
# still counts as busy ONLY on a recognized glyph; anything else (a future
# CLI's different spinner, no title yet, a non-Claude agent, a "remote" kind
# session with no PTY) falls through to the existing transcript-based checks
# below, unchanged. What the unrecognized-glyph note adds is that the fall-through is no longer
# *silent*: :func:`_note_unrecognized_title_glyph` leaves one info-level
# breadcrumb per distinct unrecognized glyph, so the next such drift is one
# grep away instead of another live probe.
#
# This is a positive, PTY-sourced signal that a turn was in progress as of
# the *last title paint* — not a heartbeat. A genuinely wedged PTY (no more
# output at all) freezes on whatever glyph was last painted, which could be
# this same spinner, so it cannot by itself distinguish "still legitimately
# working" from "wedged mid-spinner". Closing that remaining gap needs a
# freshness check against the session's own last PTY output, closed by
# :data:`_WEDGED_PTY_AFTER` below.
_BUSY_LIVE_TITLE_RE = re.compile(r"^[⠀-⣿◐-◓]")

# The idle counterpart of the spinner above: control is back at the prompt.
# Not part of the busy test — it is named only so the drift breadcrumb can
# stay quiet about the one non-busy glyph that is already understood.
_IDLE_LIVE_TITLE_GLYPH = "✳"  # U+2733

# Drift breadcrumb bookkeeping. The Board polls every 5s and a title
# glyph is stable for a session's whole life, so logging every rejection
# would bury the one line that matters. One info line per distinct glyph,
# capped and cleared wholesale on overflow — the same shape as
# board_sessions.py's _LOGGED_SUPPRESSED_ROWS.
_LOGGED_UNKNOWN_TITLE_GLYPHS: "set[str]" = set()
_UNKNOWN_TITLE_GLYPH_LOG_CAP = 64

# Wedged-PTY staleness: how old ``last_output_at`` (the PTY's raw-read
# timestamp, session_host.py's ``PtySession._last_output_at``) can be before
# a busy ``live_title`` is no longer trusted at face value. Live-probed
# against two real, concurrently-busy production sessions on this box (a
# 20s WS-attach sample per session, read-only, no input sent): raw output
# arrived at a median ~100ms cadence in both, with the single largest
# inter-arrival gap observed at 1.06s across ~400 combined frames — a
# genuine spinner repaints continuously regardless of whether the title
# *string* itself changes. 10s is a ~10x margin over that worst observed
# gap: generous enough to absorb a slow disk flush or scheduler hiccup
# without crying wolf, while still being an order of magnitude tighter than
# :data:`_STALLED_DISPATCH_AFTER` (a *background-dispatch* staleness check,
# not a PTY-heartbeat one — the two measure different things at different
# timescales). A ``last_output_at`` past this age means the PTY has gone
# genuinely quiet, so the busy glyph is stale chrome, not proof of a live
# turn — the override below falls through to the earlier transcript-based
# checks instead of trusting it.
_WEDGED_PTY_AFTER = timedelta(seconds=10)


def _note_unrecognized_title_glyph(title: str) -> None:
    """Leave one info-level breadcrumb per distinct unrecognized leading
    glyph on a ``live_title`` (#815).

    Scoped to *non-ASCII* leads that aren't the known idle marker: an ASCII
    lead is an ordinary title (a Codex folder echo, a plain prompt title)
    and proves nothing, while a glyph lead is the CLI's own brand chrome —
    exactly the thing that changed underneath this check once already and
    silently disabled it. Purely observational — it never influences the
    verdict — and nothing here can raise into the caller: the lead is a
    single character (so ``ord`` is total), and the message is logged
    lazily, leaving any formatting failure to ``logging``'s own handler.
    """
    lead = title[:1]
    if not lead or lead.isascii() or lead == _IDLE_LIVE_TITLE_GLYPH:
        return
    log_once(
        _LOGGED_UNKNOWN_TITLE_GLYPHS, lead, _UNKNOWN_TITLE_GLYPH_LOG_CAP,
        logger.info,
        "ℹ️ board: live_title leads with unrecognized glyph U+%04X (%s) — "
        "not treated as busy. If the CLI changed its spinner again, widen "
        "_BUSY_LIVE_TITLE_RE (#815).",
        ord(lead),
        lead,
    )


def _live_title_is_busy(live_title: Optional[str]) -> bool:
    """Whether ``live_title`` opens with Claude Code's animated spinner glyph.

    See the module-level comment above :data:`_BUSY_LIVE_TITLE_RE` for what
    this does and does not prove, and why an unrecognized glyph falls
    through to the transcript-based checks with a one-off breadcrumb
    (#815) rather than silently.
    """
    title = (live_title or "").strip()
    if _BUSY_LIVE_TITLE_RE.match(title):
        return True
    _note_unrecognized_title_glyph(title)
    return False


def strip_status_glyph(live_title: Optional[str]) -> str:
    """``live_title`` with Claude Code's leading status glyph removed.

    The PTY window title is the conversation's own name behind one status
    glyph: the animated spinner while a turn runs
    (:data:`_BUSY_LIVE_TITLE_RE`) or :data:`_IDLE_LIVE_TITLE_GLYPH` once
    control is back at the prompt. Both are stripped, because both are
    chrome rather than name — measured for #1034 against the live sessions
    on this box, 2 of the 3 PTYs were sitting on the *idle* glyph, so a
    reader that knew only the busy one would read
    ``✳ Wooden floor repair investigation`` as differing from the
    conversation named ``Wooden floor repair investigation`` and act on a
    difference that is pure chrome.

    Deliberately *not* :func:`_live_title_is_busy`, which is a verdict
    carrying #815's drift breadcrumb as a side effect: this is a pure
    string normalisation used on the 5s-polled drawer path, and it reuses
    that function's own two patterns rather than spelling the glyphs a
    third time.

    An unrecognized future glyph is left in place. That can only make two
    titles compare as *different*, which is the conservative direction for
    the one caller — :func:`transcript_locate._disprove_by_live_title`'s
    disproof, where a difference refuses an inferred answer rather than
    accepting one.
    """
    title = (live_title or "").strip()
    if _BUSY_LIVE_TITLE_RE.match(title) or title[:1] == _IDLE_LIVE_TITLE_GLYPH:
        title = title[1:].strip()
    return title

_ACTIVITY_TAIL_BYTES = 8 * 1024

# Only the transcript's tail is read for either an exchange or a pending
# background dispatch — a long session's JSONL runs to many MB, but the last
# exchange (or the launch line of a dispatch still in flight) always sits
# within the final few hundred KB, even behind one large intervening tool
# result (e.g. a file Read). Shared by :func:`last_exchange` and
# :func:`_pending_background_dispatch_launched_at` (widened to the latter
# from the much smaller ``_ACTIVITY_TAIL_BYTES`` after a live dispatch's
# launch line — 11.8 KB back — was pushed out of an 8 KB window by one such
# Read).
_EXCHANGE_TAIL_BYTES = 256 * 1024

# The ``stalled`` status: how long a pending background dispatch must sit
# unresolved before a Stop-hook ``needs-you`` stamp is a real anomaly rather
# than healthy waiting. Deliberately generous, per explicit direction: a
# fleet chief was observed correctly *not* alerting through two ~9-minute e2e
# gate runs in one day (this repo's own full verify-before-ship gate is
# documented at ~10-11 minutes, CI's own investigate threshold is >12
# minutes) — that is ordinary waiting, not a stall. The distinguishing
# property a caller actually wants is "a turn that ended with nothing that
# will ever wake it", which this module cannot observe directly; duration is
# only a proxy. A threshold comfortably above every observed healthy wait
# trades a slower alert for not crying wolf — "a stalled that is right 60% of
# the time is worse than useless" than one that is occasionally a few minutes
# late.
_STALLED_DISPATCH_AFTER = timedelta(minutes=30)

# The sentinel :func:`_pending_background_dispatch_launched_at` returns when
# something is genuinely outstanding but its launch line carried no
# parseable timestamp — real, just not age-able. Shared with
# :func:`_transcript_overlay` so the ``stalled`` check can tell "unknown age"
# apart from "actually old" instead of misreading the epoch bound as proof of
# staleness.
_UNKNOWN_LAUNCH_STAMP = datetime.min.replace(tzinfo=timezone.utc)

# A session whose turn ended while its own background Bash, sub-agent or
# ``Monitor`` is still running. Claude's turn, never Your turn: a harness
# notification wakes it when the task finishes, so no human is being waited on.
WAITING_ON_TASK = "waiting-on-task"


def _read_tail_bytes(path: Any, n_bytes: int) -> bytes:
    """Read the last ``n_bytes`` of ``path`` as raw bytes.

    Best-effort: any OSError (missing file, read failure) returns ``b""`` —
    callers degrade to "no signal" the same way a parse failure would.
    Shared with :func:`board_exchange._read_tail`, which differs only in
    decoding the result to ``str`` instead of splitting it into lines.
    """
    try:
        with Path(str(path)).open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - n_bytes))
            return fh.read()
    except OSError:
        return b""


def _tail_lines(path: Any, n_bytes: int) -> Tuple[List[str], bool]:
    """Read the last ``n_bytes`` of ``path``, decoded and split into lines.

    Also returns whether the read was truncated (the file is bigger than
    ``n_bytes`` — the first returned line is then likely a torn partial
    record, which callers that care skip). Best-effort: any OSError (missing
    file, read failure) returns ``([], False)`` — callers degrade to "no
    signal" the same way a parse failure would. Shared by
    :func:`_last_activity` and :func:`last_exchange`, which differ only in
    the byte window and what they do with the lines.
    """
    raw = _read_tail_bytes(path, n_bytes)
    try:
        size = Path(str(path)).stat().st_size
    except OSError:
        return [], False
    return raw.decode("utf-8", errors="replace").splitlines(), size > n_bytes


class _ExchangeTail:
    """One card's :data:`_EXCHANGE_TAIL_BYTES` transcript tail, read and
    parsed at most once (#881).

    :func:`_transcript_overlay`'s pending-dispatch scan and
    :func:`_refine_waiting_status`'s pending-tool scan both walk this same
    window of the same transcript, back to back, for every waiting card on
    every 5s Board poll. Each used to read and ``json.loads`` the whole
    256 KB on its own; :func:`src.board.sessions.merge_sessions` now hands
    both one of these, so
    the pair costs a single read. Lazy, because either scan can be skipped
    (a busy live title short-circuits the overlay, a non-``needs-you``
    status skips the refine) and a card that needs neither must not pay for
    the read at all.

    ``objects()`` returns every parseable JSON object in the tail, oldest
    first; a truncated read's first line is dropped (likely torn, matching
    :func:`last_exchange`), and blank, unparseable or non-object lines are
    skipped. Any IO error degrades to an empty list — "no signal", the same
    as before.
    """

    def __init__(self, transcript_path: Any) -> None:
        self.path = transcript_path
        self._objects: Optional[List[Dict[str, Any]]] = None

    def objects(self) -> List[Dict[str, Any]]:
        if self._objects is None:
            lines, truncated = _tail_lines(self.path, _EXCHANGE_TAIL_BYTES)
            if truncated and lines:
                lines = lines[1:]
            parsed: List[Dict[str, Any]] = []
            for raw in lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    parsed.append(obj)
            self._objects = parsed
        return self._objects


def _last_activity(transcript_path: Any) -> Optional[datetime]:
    """Timestamp of the newest real conversation event in the transcript tail.

    Claude Code appends non-message metadata lines (``system``, ``pr-link``,
    ``ai-title``, ``file-history-snapshot``, …) seconds-to-minutes after a
    turn ends, so the file's mtime overstates activity (#309). Only
    ``assistant``/``user`` lines carrying a ``message`` payload mark a live
    turn; the newest one's ``timestamp`` is the activity anchor. Torn or
    unparseable lines are skipped (a line may be appended mid-read); no
    conversation event in the tail → ``None`` — callers keep the hook status.
    """
    lines, _truncated = _tail_lines(transcript_path, _ACTIVITY_TAIL_BYTES)
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") not in ("assistant", "user"):
            continue
        if not isinstance(obj.get("message"), dict):
            continue
        stamp = _parse_iso(obj.get("timestamp"))
        if stamp is not None:
            return stamp
    return None


def _last_assistant_model(transcript_path: Any) -> Optional[str]:
    """The ``model`` of the newest assistant record in the transcript tail.

    That record names the model that actually answered, so it follows a
    resume and an in-session ``/model`` switch, which the spawn flags never
    do. Claude Code's own local replies carry ``<synthetic>`` there; only a
    ``claude-*`` id counts. Torn or unparseable lines are skipped; no
    assistant record in the window, or an unreadable file, is ``None``.
    """
    lines, truncated = _tail_lines(transcript_path, _EXCHANGE_TAIL_BYTES)
    if truncated and lines:
        lines = lines[1:]
    for raw in reversed(lines):
        # Cheap pre-filter: most tail lines are tool results and user turns.
        if '"assistant"' not in raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        message = obj.get("message")
        model = message.get("model") if isinstance(message, dict) else None
        if isinstance(model, str) and model.startswith("claude-"):
            return model
    return None


def _transcript_mtime(transcript_path: Any) -> Optional[datetime]:
    """The transcript file's mtime as a UTC datetime, or ``None`` on any
    OSError (missing file, permission, …). Shared by
    :func:`_transcript_overlay` and :func:`_external_row_liveness` — both need
    this same cheap stat probe before deciding whether a costlier tail read
    (or, for the ghost check, a status flip) is warranted.
    """
    try:
        return datetime.fromtimestamp(
            Path(str(transcript_path)).stat().st_mtime, tz=timezone.utc
        )
    except OSError:
        return None


def _pty_output_is_fresh(
    last_output_at: Optional[float], now: datetime
) -> bool:
    """Whether ``last_output_at`` (``PtySession._last_output_at``, a raw
    ``time.time()`` epoch stamp) is recent enough to trust a busy
    ``live_title`` at face value — see :data:`_WEDGED_PTY_AFTER`.

    ``None`` (an older ``to_api()`` build with no such field yet, or a
    non-PTY/unmatched row with no live session to read from at all) means
    "no freshness signal available", not "stale" — treated as fresh so the
    pre-#636 behavior (trust the busy glyph unconditionally) is unchanged
    when this signal simply isn't there. A non-positive value is the same
    "never recorded" case, not a real epoch-0 timestamp (session_host.py
    always stamps this in the same read that first populates ``live_title``,
    so a genuinely busy title with a zero stamp can't happen in practice).
    """
    if last_output_at is None or last_output_at <= 0:
        return True
    age = now - datetime.fromtimestamp(last_output_at, tz=timezone.utc)
    return age <= _WEDGED_PTY_AFTER


def _transcript_overlay(
    row: Optional[Dict[str, Any]],
    status: str,
    anchor: Optional[datetime],
    *,
    now: Optional[datetime] = None,
    live_title: Optional[str] = None,
    last_output_at: Optional[float] = None,
    tail: Optional[_ExchangeTail] = None,
) -> tuple:
    """Override a waiting status with ``working`` (or, for a long-stuck
    dispatch, ``stalled``) when the transcript says so.

    Checked first (#631/#627): a busy ``live_title`` (see
    :func:`_live_title_is_busy`) short-circuits straight to ``working``,
    before any of the transcript-file reads below run — but only while
    ``last_output_at`` is also fresh (:func:`_pty_output_is_fresh`, #636): a
    busy glyph frozen on a genuinely wedged PTY (no more raw output at all)
    is stale chrome, not proof of a live turn, so it falls through to the
    transcript-based checks below instead — the same path a non-busy title
    already takes. It is sourced from the live PTY, not this row's
    transcript, so it is immune to the JSONL write-ordering race the rest of
    this function has to work around.

    The hooks flip status only on prompt-submit / stop / notification — but
    Claude *resumes* without any of those firing (an answered permission
    prompt or AskUserQuestion, a prompt queued into a running turn), so a
    ``needs-you``/``idle`` stamp sticks while the agent visibly works (#305).
    The transcript JSONL is ground truth: it is appended continuously during
    a turn and goes quiet on stop — and the Stop hook re-stamps the row
    *after* the final transcript write, so a genuine ``needs-you`` still wins
    immediately.

    The probe is two-stage (#309): the mtime ``os.stat`` is only a cheap
    pre-filter — post-Stop metadata lines advance mtime with no real resume —
    so when mtime clears the epsilon, :func:`_last_activity` reads the tail
    and only a real conversation line past the stamp flips the status.

    Separately (#464), :func:`_pending_background_dispatch_launched_at`
    always checks the tail for an outstanding background sub-agent/shell
    dispatch launched *before* Claude's own turn ended — a case the mtime
    pre-filter above would otherwise skip, since nothing is written to this
    transcript past the stamp until the background work's own completion
    notice lands. #608 refines this further: a dispatch outstanding past
    :data:`_STALLED_DISPATCH_AFTER` is no longer healthy waiting but a real
    anomaly, so it renders ``stalled`` instead of :data:`WAITING_ON_TASK` — but only when
    the launch line's own timestamp actually resolved
    (:data:`_UNKNOWN_LAUNCH_STAMP` means "genuinely outstanding, age
    unknown", not "very old"; guessing stalled from that sentinel would be
    wrong far too often, per the explicit caution against a low-confidence
    stalled call). ``idle`` never gets a ``stalled`` verdict — the five-way
    split is scoped to ``needs-you``, and :func:`_refine_waiting_status` does
    the rest of that split once this function is done overriding.

    ``tail`` is the card's shared :class:`_ExchangeTail` (#881) — pass the
    same one on to :func:`_refine_waiting_status` so the two scans share one
    read. Omitted, a private one is built for this path.

    Any failure keeps the hook status. Returns the (possibly overridden)
    ``(status, age-anchor)``.
    """
    if row is None or status not in ("needs-you", "idle"):
        return status, anchor
    now = now or _now()
    if _live_title_is_busy(live_title) and _pty_output_is_fresh(last_output_at, now):
        # Re-anchor to "now", not the stale hook stamp `anchor` carries in —
        # this override only proves busy as of this instant, not since when.
        return "working", now
    updated = _parse_iso(row.get("updated_at"))
    transcript = row.get("transcript_path")
    if updated is None or not transcript:
        return status, anchor
    mtime = _transcript_mtime(transcript)
    if mtime is None:
        return status, anchor
    if mtime - updated > _RESUME_EPSILON:
        activity = _last_activity(transcript)
        if activity is not None and activity - updated > _RESUME_EPSILON:
            return "working", activity
    pending_since = _pending_background_dispatch_launched_at(
        tail if tail is not None else _ExchangeTail(transcript)
    )
    if pending_since is not None:
        if (
            status == "needs-you"
            and pending_since != _UNKNOWN_LAUNCH_STAMP
            and now - pending_since > _STALLED_DISPATCH_AFTER
        ):
            return "stalled", anchor
        return WAITING_ON_TASK, anchor
    return status, anchor




def _outstanding_dispatch_stamps(tail: _ExchangeTail) -> Tuple[List[Optional[datetime]], Optional[int]]:
    """Launch stamps of every dispatch the tail never saw end, plus the last
    ``turn_duration`` line's ``pendingBackgroundAgentCount``.

    A ``<task-notification>``'s ``<task-id>``/``<tool-use-id>`` tags first
    appear on a ``queue-operation`` line whose ``operation`` is ``"enqueue"``
    — that only means the background result is *ready*, not that Claude has
    received it yet: an enqueue with no later ``dequeue``/``remove`` is a
    notification still sitting unconsumed in the queue, under a stale
    ``needs-you`` Stop stamp. So an enqueued notification is only counted as delivered once a
    *later* ``dequeue``/``remove`` operation pops it off the (FIFO) queue —
    tracked positionally, since a ``dequeue`` line carries no content of its
    own to correlate by id. Other queue traffic (e.g. a prompt typed into an
    already-running session, which rides the same enqueue/dequeue mechanism)
    occupies a queue slot too, so it is tracked untagged rather than skipped —
    skipping it would misalign a later dequeue onto the wrong notification.
    The ``attachment``-shaped notification (:func:`_notified_background_ids`'s
    other fallback) is not queue-gated: it isn't a ``queue-operation`` line at
    all, so it already represents a delivered conversation event.
    """
    launched: Dict[str, Optional[datetime]] = {}
    completed: set = set()
    bash_launched: Dict[str, Optional[datetime]] = {}
    bash_completed: set = set()
    monitor_use_stamps: Dict[str, Optional[datetime]] = {}
    monitors: Dict[str, Optional[datetime]] = {}
    monitors_ended: set = set()
    queued_notifications: List[Tuple[List[str], List[str], List[str]]] = []
    last_turn_pending_agent_count: Optional[int] = None
    for obj in tail.objects():
        if obj.get("type") == "system" and obj.get("subtype") == "turn_duration":
            count = obj.get("pendingBackgroundAgentCount")
            if isinstance(count, int):
                last_turn_pending_agent_count = count
        stamp = _parse_iso(obj.get("timestamp"))
        for tid in _launched_background_ids(obj):
            launched.setdefault(tid, stamp)
        for tid in _launched_bash_dispatch_ids(obj):
            bash_launched.setdefault(tid, stamp)
        for use_id in _launched_monitor_use_ids(obj):
            monitor_use_stamps.setdefault(use_id, stamp)
        ack = _monitor_ack(obj)
        if ack is not None and ack[0] in monitor_use_stamps:
            monitors.setdefault(ack[1], monitor_use_stamps[ack[0]] or stamp)
        monitor_event = _monitor_notification(obj)
        if monitor_event is not None and not monitor_event[1]:
            # A live event proves its monitor is running even when the launch
            # line scrolled out of the tail window.
            monitors.setdefault(monitor_event[0], stamp)
        ended_monitors = [monitor_event[0]] if monitor_event and monitor_event[1] else []
        if obj.get("type") == "queue-operation":
            operation = obj.get("operation")
            if operation == "enqueue":
                queued_notifications.append((
                    _notified_background_ids(obj),
                    _notified_bash_dispatch_ids(obj),
                    ended_monitors,
                ))
            elif operation in ("dequeue", "remove") and queued_notifications:
                task_ids, tool_use_ids, monitor_ids = queued_notifications.pop(0)
                completed.update(task_ids)
                bash_completed.update(tool_use_ids)
                monitors_ended.update(monitor_ids)
            continue
        completed.update(_notified_background_ids(obj))
        bash_completed.update(_notified_bash_dispatch_ids(obj))
        monitors_ended.update(ended_monitors)
    outstanding_stamps = [
        stamp for tid, stamp in launched.items() if tid not in completed
    ] + [
        stamp for tid, stamp in bash_launched.items() if tid not in bash_completed
    ] + [
        stamp for tid, stamp in monitors.items() if tid not in monitors_ended
    ]
    return outstanding_stamps, last_turn_pending_agent_count


def _pending_background_dispatch_launched_at(tail: _ExchangeTail) -> Optional[datetime]:
    """The earliest still-outstanding background dispatch's own launch
    timestamp, or ``None`` if nothing is pending (#608's ``stalled`` status
    needs *how long* a dispatch has been outstanding, not just whether one
    is).

    A ``Stop`` hook fires (stamping ``needs-you``) the moment Claude's own
    turn ends — even when that turn dispatched a background sub-agent or
    shell command it is still waiting to hear back from. The parent
    transcript then goes quiet until the eventual ``<task-notification>``
    lands, so #305's activity check alone never catches this window (#464).
    Unlike :func:`_last_activity` this runs unconditionally, not mtime-gated:
    the dispatch's launch line sits *before* the hook's stamp, not after it,
    so the mtime pre-filter that skips a quiet tail would otherwise hide it.

    Two independent launched/completed id schemes are unioned (#576): the
    original one keyed on internal ``toolUseResult`` fields
    (:func:`_launched_background_ids` / :func:`_notified_background_ids`,
    covers backgrounded ``Bash`` via ``backgroundTaskId`` and async
    ``Agent``/``Task`` dispatches via ``isAsync``+``agentId``), and a
    narrower, more robust one for backgrounded ``Bash``/``PowerShell`` calls
    specifically, keyed on the tool call's own ``tool_use`` id
    (:func:`_launched_bash_dispatch_ids` / :func:`_notified_bash_dispatch_ids`),
    which doesn't depend on the internal result-shape field that a live
    spot-check found had drifted. Either scheme flagging a dispatch as
    still-outstanding is enough to keep the status ``working`` — a launched
    id only counts as resolved within its own scheme's id-space, so there's
    no cross-scheme false match. (The sub-agent/``Task`` path is left to the
    original scheme alone — unlike backgrounded Bash, most ``Task`` dispatches
    are ordinary *synchronous* sub-agent calls, so a tool-call-id-keyed check
    would need to tell "still running async" apart from "finished, this is
    just the normal blocking reply" and risk misreading the common case.)

    A ``Monitor`` is a third scheme, keyed on its own ``taskId``: pending from
    its launch ack until a notification carrying ``<status>`` (or a timeout
    event) ends it. A bare monitor event never ends it, and counts as proof it
    is running when the launch line has left the tail window.

    Reads :data:`_EXCHANGE_TAIL_BYTES` rather than the smaller
    ``_ACTIVITY_TAIL_BYTES`` (#594): a launch line can sit well behind one
    large intervening tool result (e.g. a file ``Read``) by the time the turn
    ends, and the 8 KB window sized for the cheap #309 mtime pre-filter was
    empirically too small to still reach it. The read, torn-first-line drop
    and parse are :class:`_ExchangeTail`'s, shared with
    :func:`_tail_tool_use_pairs` (#881).

    Even :data:`_EXCHANGE_TAIL_BYTES` is just a window: a long turn can push
    the launch line past it (one real background ``Explore`` dispatch sat
    ~299 KB from EOF), and the id-keyed scan then finds nothing outstanding.
    Claude Code reports the count itself, independent of any window: the
    ``system``/``turn_duration`` line it appends when a turn ends carries
    ``pendingBackgroundAgentCount``. When the scan finds no outstanding stamp
    but that last count is nonzero, the count wins, reported as
    :data:`_UNKNOWN_LAUNCH_STAMP` (outstanding, age unknown) since it carries
    no per-dispatch launch time.

    Any failure degrades to "nothing pending" — callers keep the hook status.
    """
    outstanding_stamps, last_turn_pending_agent_count = _outstanding_dispatch_stamps(tail)
    if not outstanding_stamps:
        if last_turn_pending_agent_count:
            return _UNKNOWN_LAUNCH_STAMP
        return None
    resolved_stamps = [s for s in outstanding_stamps if s is not None]
    if not resolved_stamps:
        # Something is genuinely outstanding but its launch line carried no
        # parseable timestamp — real, just not age-able. Report the
        # earliest-possible bound (epoch) so a caller keying only on
        # "is anything pending" still sees it, while the stalled check
        # (which needs a real age) treats this as unknown rather than old.
        return _UNKNOWN_LAUNCH_STAMP
    return min(resolved_stamps)


# The tool_use names that block on a human decision, not just an async
# result — a caller must be able to tell "blocked on a human" apart from
# "finished, nothing wanted" without fetching the exchange.
_PENDING_DECISION_TOOL_NAMES = frozenset({"AskUserQuestion", "ExitPlanMode"})


def _tail_tool_use_pairs(tail: _ExchangeTail) -> Tuple[Dict[str, str], "set[str]", bool]:
    """Every assistant ``tool_use`` in the tail (id -> tool name), every
    ``tool_use_id`` a later ``tool_result`` block resolves, and whether the
    tail held at least one genuine, parseable conversation line at all
    (#608).

    Same tail-scan shape as :func:`_pending_background_dispatch_launched_at`
    — over the same :class:`_ExchangeTail`, so a card pays for one read
    between them (#881) — generalized from "any backgrounded dispatch" to
    "any tool call at all" —
    :func:`_refine_waiting_status` needs to know whether *anything* is still
    pending, not just a background one. A backgrounded ``Bash``/``PowerShell``
    call's own synchronous "launched" ack rides as a normal ``tool_result``
    block too (empirically confirmed against a live transcript: the block
    carries a sibling ``toolUseResult.backgroundTaskId``, but the block itself
    is the standard Anthropic ``tool_result`` shape) — so it reads as
    "resolved" here the moment it's launched, same as any other tool call.
    That is intentional: whether the *background work itself* is still
    outstanding is a separate question :func:`_transcript_overlay` already
    answers via :func:`_pending_background_dispatch_launched_at`, which this
    function does not duplicate.

    The third return value uses the same "real conversation event" test as
    :func:`_last_activity` (an ``assistant``/``user`` line carrying a
    ``message`` dict) — a missing file, an empty tail, or an unparseable one
    all leave it ``False``. This lets :func:`_pending_tool_use_names`, and
    through it :func:`_refine_waiting_status`, tell "checked and found
    nothing pending" apart from "couldn't check at all" — the latter must
    never be read as proof of a clean stop.
    """
    launched: Dict[str, str] = {}
    completed: "set[str]" = set()
    saw_message = False
    for obj in tail.objects():
        obj_type = obj.get("type")
        if obj_type not in ("assistant", "user"):
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        saw_message = True
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        if obj_type == "assistant":
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and isinstance(block.get("id"), str)
                ):
                    launched[block["id"]] = str(block.get("name") or "")
        else:
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tid = block.get("tool_use_id")
                    if isinstance(tid, str):
                        completed.add(tid)
    return launched, completed, saw_message


def _pending_tool_use_names(tail: _ExchangeTail) -> Optional["set[str]"]:
    """Tool names of every ``tool_use`` still unresolved at the tail's end
    (#608) — empty set when the tail was read and genuinely has nothing
    pending; ``None`` when there was no usable signal at all (no path,
    missing file, or an unparseable tail) — the caller must not conflate the
    two."""
    if not tail.path:
        return None
    launched, completed, saw_message = _tail_tool_use_pairs(tail)
    if not saw_message:
        return None
    return {name for tid, name in launched.items() if tid not in completed}


def pending_decision_call(transcript_path: Any) -> Optional[Dict[str, Any]]:
    """The newest :data:`_PENDING_DECISION_TOOL_NAMES` ``tool_use`` still
    unresolved at the tail's end, as ``{"id", "name", "input"}`` — the same
    "pending decision" :func:`_refine_waiting_status` sorts a card into
    ``awaiting-decision`` by, handed back whole so the Chat pane can answer
    it (#1149).

    ``None`` means *not established*, and covers both "nothing pending" and
    "couldn't read the tail" on purpose: the one caller is a send-time gate
    that types keystrokes into a live agent, where either answer must stop
    the send. Sub-agent (sidechain) traffic is skipped — a question the user
    is being asked is always the main thread's.
    """
    if not transcript_path:
        return None
    launched: List[Dict[str, Any]] = []
    completed: "set[str]" = set()
    for obj in _ExchangeTail(transcript_path).objects():
        if obj.get("isSidechain") or obj.get("type") not in ("assistant", "user"):
            continue
        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                launched.append(block)
            elif block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                completed.add(block["tool_use_id"])
    for block in reversed(launched):
        name = str(block.get("name") or "")
        if name in _PENDING_DECISION_TOOL_NAMES and block["id"] not in completed:
            raw = block.get("input")
            return {"id": block["id"], "name": name, "input": raw if isinstance(raw, dict) else {}}
    return None


def _refine_waiting_status(status: str, tail: _ExchangeTail) -> str:
    """Split the generic ``needs-you`` into a caller-actionable value (#608,
    sharpened by #813) without a caller ever needing to fetch the exchange to
    tell them apart:

    * ``awaiting-decision`` -- blocked on :data:`_PENDING_DECISION_TOOL_NAMES`
      (``AskUserQuestion``/``ExitPlanMode``) — a human must pick an option.
    * ``idle-finished`` -- the turn ended clean, nothing pending at all — the
      session doesn't actually need anyone, it's just holding a workspace.
    * ``tool-pending`` -- an ordinary, non-decision ``tool_use`` (``Bash``,
      ``Read``, ``Edit``, …) is still unresolved at the tail's end (#813).
      This is genuinely ambiguous — equally consistent with "still mid-turn,
      executing" and "permission-gated, waiting on a human to approve/deny" —
      so per the global rule that an unresolvable classifier must report its
      own state rather than fold into a positive one, it is neither
      ``awaiting-input`` (over-claims a human is needed) nor ``idle-finished``
      (under-claims the turn is done). Routed to Claude's turn, not Your
      turn: a false "needs you" on every long-running foreground command is
      the worse failure mode of the two. #815 fixed the busy-title fast path
      in :func:`_transcript_overlay` (a dead spinner-glyph regex), so a
      genuinely mid-turn session with a live, fresh PTY now already resolves
      to ``working`` before this function is ever reached — this branch is
      the narrower residual case: no live PTY, a wedged/quiet one, or a
      non-Claude agent this override doesn't cover.
    * ``awaiting-input`` -- everything else: a genuinely typed prompt is
      expected, or the tail gave no usable signal at all (no transcript, a
      missing file, an unparseable tail — see :func:`_pending_tool_use_names`).
      This is the old undifferentiated ``needs-you`` meaning, kept as the
      safe fallback for whatever this function can't positively classify —
      ``idle-finished`` is a claim this function must have actually checked
      for, never a default for "couldn't check" (the same asymmetry the
      ``stalled`` threshold applies to an unparseable launch timestamp).

    ``stalled`` is decided earlier, in :func:`_transcript_overlay` — it needs
    the pending *background* dispatch's age, which that function already
    computes; by the time this runs, ``status`` is never ``needs-you`` for a
    stalled dispatch (it's already ``stalled``). Any status other than
    ``needs-you`` (``working``, ``idle``, ``unknown``) passes through
    unchanged — the split is scoped to ``needs-you`` alone.
    """
    if status != "needs-you":
        return status
    pending_names = _pending_tool_use_names(tail)
    if pending_names is None:
        return "awaiting-input"
    if pending_names & _PENDING_DECISION_TOOL_NAMES:
        return "awaiting-decision"
    if pending_names:
        return "tool-pending"
    return "idle-finished"


def _external_row_liveness(
    row: Optional[Dict[str, Any]],
    now: datetime,
    *,
    claimed_transcripts: AbstractSet[str] = frozenset(),
    live_launcher_session_ids: AbstractSet[str] = frozenset(),
) -> Tuple[bool, str]:
    """Whether an unmatched hook row has independent process-liveness proof.

    A hook row can survive a hard kill or a cloud/bridge lifecycle gap for the
    writer's whole 24-hour retention window. Its status says what the agent was
    doing at the last event; it does not say the process is still alive. Two
    stronger, deterministic checks run before the transcript-freshness
    fallback (#613 — a freshness heuristic is corroboration, not proof):

    * ``launcher_session_id`` present but absent from the *current* live
      session-host list is definitive: the session-host is the authority on
      which PTYs it owns, so a row whose own PTY has been reaped is proven
      dead regardless of how recently its transcript happened to be touched.
    * A transcript path already claimed by a live, *matched* card's own row
      (``claimed_transcripts``) means this unmatched row is a superseded
      leftover of that same session — re-keyed when a worker moved from one
      issue to the next, still pointing at the transcript the live session
      keeps writing — not independent evidence of a second process.

    Only once neither applies does a transcript written in the last 5
    minutes remain the (inherently imperfect) fallback evidence for a
    genuinely external row the launcher has no other way to verify — a
    process that writes once and exits within the window is indistinguishable
    from one still running; #613 narrows this gap, it does not close it.

    The reason string is deliberately condition-specific so the caller can
    leave one useful info-level breadcrumb for the next recurrence (#455).
    """
    if row is None:
        return False, "missing state row"
    launcher_sid = row.get("launcher_session_id")
    if launcher_sid and str(launcher_sid) not in live_launcher_session_ids:
        return False, "launcher session no longer live"
    transcript = row.get("transcript_path")
    if transcript and transcript in claimed_transcripts:
        return False, "transcript claimed by a live matched session"
    if not transcript:
        return False, "missing transcript path"
    mtime = _transcript_mtime(transcript)
    if mtime is None:
        return False, "transcript file unavailable"
    if now - mtime > _EXTERNAL_ACTIVITY_AFTER:
        return False, "transcript quiet past 5 minutes"
    return True, "recent transcript activity"
