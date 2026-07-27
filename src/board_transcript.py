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
  it hasn't heard back from yet (:func:`_has_pending_background_dispatch`,
  #464, hardened by #576 and #601 — see that function's docstring for why the
  original ``toolUseResult``-keyed check alone isn't reliable);
* the last completed user→assistant exchange, for the drill-down drawer
  (:func:`last_exchange`, #301).

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

from src.board_state import _now, _parse_iso

logger = logging.getLogger(__name__)

# Transcript-activity overlay (#305): a transcript appended this much later
# than the row's stamp means Claude resumed without any hook firing. The
# margin absorbs the Stop hook and the final transcript write landing a
# couple of seconds apart in either order.
_RESUME_EPSILON = timedelta(seconds=10)

# External-row liveness (#322, tightened by #455, narrowed by #613): a hook
# state is semantic evidence, not proof that its process still exists. Only
# recent transcript activity lets an unmatched row render as an external
# session. Missing cloud/bridge transcripts and quiet waiting rows otherwise
# linger for 24 hours. Narrowed from 15 to 5 minutes by #613 — the observed
# Codex ghost sat at 14.3 minutes, just inside the old window; this is still
# an inherently imperfect fallback (a process that writes once and exits
# within the window is indistinguishable from one still running), not a fix
# for that class — the two deterministic checks in
# :func:`_external_row_liveness` (reaped launcher session, claimed
# transcript) are the real fix and run first.
_EXTERNAL_ACTIVITY_AFTER = timedelta(minutes=5)

_ACTIVITY_TAIL_BYTES = 8 * 1024

# Only the transcript's tail is read for either an exchange or a pending
# background dispatch — a long session's JSONL runs to many MB, but the last
# exchange (or the launch line of a dispatch still in flight) always sits
# within the final few hundred KB, even behind one large intervening tool
# result (e.g. a file Read). Shared by :func:`last_exchange` and
# :func:`_has_pending_background_dispatch` (#594 widened the latter from the
# much smaller ``_ACTIVITY_TAIL_BYTES`` after a live dispatch's launch line —
# 11.8 KB back — was pushed out of an 8 KB window by one such Read).
_EXCHANGE_TAIL_BYTES = 256 * 1024

# Pending-background-dispatch detection (#464): the id a completed dispatch
# is later referenced by, e.g. "<task-id>btvos2agp</task-id>".
_TASK_ID_RE = re.compile(r"<task-id>([^<]+)</task-id>")

# #608's ``stalled`` status: how long a pending background dispatch must sit
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
    try:
        with Path(str(path)).open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - n_bytes))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
            return lines, size > n_bytes
    except OSError:
        return [], False


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


def _transcript_overlay(
    row: Optional[Dict[str, Any]],
    status: str,
    anchor: Optional[datetime],
    *,
    now: Optional[datetime] = None,
) -> tuple:
    """Override a waiting status with ``working`` (or, for a long-stuck
    dispatch, ``stalled``) when the transcript says so.

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
    anomaly, so it renders ``stalled`` instead of ``working`` — but only when
    the launch line's own timestamp actually resolved
    (:data:`_UNKNOWN_LAUNCH_STAMP` means "genuinely outstanding, age
    unknown", not "very old"; guessing stalled from that sentinel would be
    wrong far too often, per the explicit caution against a low-confidence
    stalled call). ``idle`` never gets a ``stalled`` verdict — the four-way
    split is scoped to ``needs-you``, and :func:`_refine_waiting_status` does
    the rest of that split once this function is done overriding.

    Any failure keeps the hook status. Returns the (possibly overridden)
    ``(status, age-anchor)``.
    """
    if row is None or status not in ("needs-you", "idle"):
        return status, anchor
    now = now or _now()
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
    pending_since = _pending_background_dispatch_launched_at(transcript)
    if pending_since is not None:
        if (
            status == "needs-you"
            and pending_since != _UNKNOWN_LAUNCH_STAMP
            and now - pending_since > _STALLED_DISPATCH_AFTER
        ):
            return "stalled", anchor
        return "working", anchor
    return status, anchor


def _launched_background_ids(obj: Dict[str, Any]) -> List[str]:
    """Background-dispatch ids a transcript line's tool result evidences.

    The synchronous ack for a backgrounded dispatch rides ``toolUseResult``
    (a sibling of ``message``, not inside it, per live transcripts): a
    backgrounded ``Bash`` command carries ``backgroundTaskId``; an async
    sub-agent dispatch (the ``Agent``/``Task`` tool) carries ``isAsync: true``
    plus ``agentId``. Both ids later reappear inside a completion's
    ``<task-id>`` (see :func:`_notified_background_ids`).
    """
    result = obj.get("toolUseResult")
    if not isinstance(result, dict):
        return []
    ids: List[str] = []
    background_id = result.get("backgroundTaskId")
    if isinstance(background_id, str) and background_id:
        ids.append(background_id)
    if result.get("isAsync"):
        agent_id = result.get("agentId")
        if isinstance(agent_id, str) and agent_id:
            ids.append(agent_id)
    return ids


def _notified_background_ids(obj: Dict[str, Any]) -> List[str]:
    """Background-dispatch ids a ``<task-notification>`` line marks complete.

    Claude Code injects the completion notice as a ``queue-operation`` line's
    plain ``content`` string, or an ``attachment`` line's
    ``attachment.prompt`` — never as an ordinary ``assistant``/``user``
    message, so it is invisible to :func:`_last_activity` (empirically
    confirmed against live transcripts, app-launcher#464). Either shape
    carries the same ``<task-id>ID</task-id>``.
    """
    text = obj.get("content")
    if not isinstance(text, str) or "<task-notification>" not in text:
        attachment = obj.get("attachment")
        text = attachment.get("prompt") if isinstance(attachment, dict) else None
    if not isinstance(text, str) or "<task-notification>" not in text:
        return []
    return _TASK_ID_RE.findall(text)


_BACKGROUNDABLE_TOOL_NAMES = frozenset({"Bash", "PowerShell"})


def _is_background_dispatch_tool_use(block: Any) -> bool:
    """Whether an assistant ``tool_use`` content block is a ``Bash`` or
    ``PowerShell`` call dispatched with ``run_in_background: true``.

    Read directly off the ``tool_use`` block itself — the Anthropic
    message-content-block shape (every tool call carries ``type``, ``name``,
    ``id``, ``input``) is part of the stable message format, unlike the
    internal ``toolUseResult.backgroundTaskId`` key
    :func:`_launched_background_ids` depends on, which a live-transcript
    spot-check for #576 found had silently stopped appearing: a fresh
    ``run_in_background`` Bash dispatch and its eventual
    ``<task-notification>`` both showed up in the tail, but no
    ``backgroundTaskId`` anywhere — the old check sees nothing launched and
    never overrides the status. ``PowerShell`` shares the same
    ``run_in_background`` input shape and is this repo's own agents' actual
    backgrounding tool on Windows (#594) — restricting the check to ``Bash``
    left it blind to exactly that case.
    """
    return (
        isinstance(block, dict)
        and block.get("type") == "tool_use"
        and block.get("name") in _BACKGROUNDABLE_TOOL_NAMES
        and isinstance(block.get("input"), dict)
        and block["input"].get("run_in_background") is True
    )


def _launched_bash_dispatch_ids(obj: Dict[str, Any]) -> List[str]:
    """``tool_use`` ids of backgrounded ``Bash``/``PowerShell`` dispatches an
    assistant line launches (#576, widened to ``PowerShell`` by #594) —
    correlated against the tool call's own ``id``, not the internal
    ``backgroundTaskId`` :func:`_launched_background_ids` reads out of the
    result.
    """
    if obj.get("type") != "assistant":
        return []
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return []
    return [
        block["id"]
        for block in content
        if _is_background_dispatch_tool_use(block) and isinstance(block.get("id"), str)
    ]


# A completion notification's own correlation tag (#576) — carried alongside
# ``<task-id>`` in the real ``<task-notification>`` payload (confirmed
# against a live transcript), so it resolves a dispatch even when nothing
# ever surfaced the internal ``backgroundTaskId`` the legacy check keys on.
# Deliberately *not* matched against an ordinary ``tool_result`` for the same
# ``tool_use_id``: a backgrounded Bash call's synchronous reply is just the
# "launched" ack, not real completion — treating it as done would defeat the
# whole point of this check the instant the dispatch fires.
_TOOL_USE_ID_RE = re.compile(r"<tool-use-id>([^<]+)</tool-use-id>")


def _notified_bash_dispatch_ids(obj: Dict[str, Any]) -> List[str]:
    """``tool_use`` ids a ``<task-notification>`` line's ``<tool-use-id>``
    tag proves resolved (#576)."""
    text = obj.get("content")
    if not isinstance(text, str) or "<task-notification>" not in text:
        attachment = obj.get("attachment")
        text = attachment.get("prompt") if isinstance(attachment, dict) else None
    if not isinstance(text, str) or "<task-notification>" not in text:
        return []
    return _TOOL_USE_ID_RE.findall(text)


def _pending_background_dispatch_launched_at(transcript_path: Any) -> Optional[datetime]:
    """The earliest still-outstanding background dispatch's own launch
    timestamp, or ``None`` if nothing is pending (#608's ``stalled`` status
    needs *how long* a dispatch has been outstanding, not just whether one
    is — see :func:`_has_pending_background_dispatch` for the full detection
    story this shares).

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

    Reads :data:`_EXCHANGE_TAIL_BYTES` rather than the smaller
    ``_ACTIVITY_TAIL_BYTES`` (#594): a launch line can sit well behind one
    large intervening tool result (e.g. a file ``Read``) by the time the turn
    ends, and the 8 KB window sized for the cheap #309 mtime pre-filter was
    empirically too small to still reach it. A truncated read's first line is
    dropped — it is likely torn — matching :func:`last_exchange`'s handling
    of the same tail-read shape.

    A ``<task-notification>``'s ``<task-id>``/``<tool-use-id>`` tags first
    appear on a ``queue-operation`` line whose ``operation`` is ``"enqueue"``
    — that only means the background result is *ready*, not that Claude has
    received it yet. A live-transcript spot-check for #601 found a session
    where the enqueue line was the last thing written to the transcript, with
    no later ``dequeue``/``remove`` — the notification was still sitting in
    the queue, unconsumed, while the row's stale ``needs-you`` Stop stamp
    stood. So an enqueued notification is only counted as delivered once a
    *later* ``dequeue``/``remove`` operation pops it off the (FIFO) queue —
    tracked positionally, since a ``dequeue`` line carries no content of its
    own to correlate by id. Other queue traffic (e.g. a prompt typed into an
    already-running session, which rides the same enqueue/dequeue mechanism)
    occupies a queue slot too, so it is tracked untagged rather than skipped —
    skipping it would misalign a later dequeue onto the wrong notification.
    The ``attachment``-shaped notification (:func:`_notified_background_ids`'s
    other fallback) is not queue-gated: it isn't a ``queue-operation`` line at
    all, so it already represents a delivered conversation event.

    Any failure degrades to "nothing pending" — callers keep the hook status.
    """
    lines, truncated = _tail_lines(transcript_path, _EXCHANGE_TAIL_BYTES)
    if truncated and lines:
        lines = lines[1:]
    launched: Dict[str, Optional[datetime]] = {}
    completed: set = set()
    bash_launched: Dict[str, Optional[datetime]] = {}
    bash_completed: set = set()
    queued_notifications: List[Tuple[List[str], List[str]]] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        stamp = _parse_iso(obj.get("timestamp"))
        for tid in _launched_background_ids(obj):
            launched.setdefault(tid, stamp)
        for tid in _launched_bash_dispatch_ids(obj):
            bash_launched.setdefault(tid, stamp)
        if obj.get("type") == "queue-operation":
            operation = obj.get("operation")
            if operation == "enqueue":
                queued_notifications.append(
                    (_notified_background_ids(obj), _notified_bash_dispatch_ids(obj))
                )
            elif operation in ("dequeue", "remove") and queued_notifications:
                task_ids, tool_use_ids = queued_notifications.pop(0)
                completed.update(task_ids)
                bash_completed.update(tool_use_ids)
            continue
        completed.update(_notified_background_ids(obj))
        bash_completed.update(_notified_bash_dispatch_ids(obj))
    outstanding_stamps = [
        stamp for tid, stamp in launched.items() if tid not in completed
    ] + [
        stamp for tid, stamp in bash_launched.items() if tid not in bash_completed
    ]
    if not outstanding_stamps:
        return None
    resolved_stamps = [s for s in outstanding_stamps if s is not None]
    if not resolved_stamps:
        # Something is genuinely outstanding but its launch line carried no
        # parseable timestamp — real, just not age-able. Report the
        # earliest-possible bound (epoch) so a caller keying only on
        # "is anything pending" still sees it, while #608's stalled check
        # (which needs a real age) treats this as unknown rather than old.
        return _UNKNOWN_LAUNCH_STAMP
    return min(resolved_stamps)


def _has_pending_background_dispatch(transcript_path: Any) -> bool:
    """Whether the tail shows a background dispatch with no completion yet
    — see :func:`_pending_background_dispatch_launched_at` for the full
    detection story; this is the plain boolean callers that don't need the
    age used before #613."""
    return _pending_background_dispatch_launched_at(transcript_path) is not None


# #608: the tool_use names that block on a human decision, not just an async
# result — a caller must be able to tell "blocked on a human" apart from
# "finished, nothing wanted" without fetching the exchange.
_PENDING_DECISION_TOOL_NAMES = frozenset({"AskUserQuestion", "ExitPlanMode"})


def _tail_tool_use_pairs(transcript_path: Any) -> Tuple[Dict[str, str], "set[str]", bool]:
    """Every assistant ``tool_use`` in the tail (id -> tool name), every
    ``tool_use_id`` a later ``tool_result`` block resolves, and whether the
    tail held at least one genuine, parseable conversation line at all
    (#608).

    Same tail-scan shape as :func:`_pending_background_dispatch_launched_at`,
    generalized from "any backgrounded dispatch" to "any tool call at all" —
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
    lines, truncated = _tail_lines(transcript_path, _EXCHANGE_TAIL_BYTES)
    if truncated and lines:
        lines = lines[1:]
    launched: Dict[str, str] = {}
    completed: "set[str]" = set()
    saw_message = False
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
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


def _pending_tool_use_names(transcript_path: Any) -> Optional["set[str]"]:
    """Tool names of every ``tool_use`` still unresolved at the tail's end
    (#608) — empty set when the tail was read and genuinely has nothing
    pending; ``None`` when there was no usable signal at all (no path,
    missing file, or an unparseable tail) — the caller must not conflate the
    two."""
    if not transcript_path:
        return None
    launched, completed, saw_message = _tail_tool_use_pairs(transcript_path)
    if not saw_message:
        return None
    return {name for tid, name in launched.items() if tid not in completed}


def _refine_waiting_status(status: str, transcript_path: Any) -> str:
    """Split the generic ``needs-you`` into a caller-actionable value (#608)
    without a caller ever needing to fetch the exchange to tell them apart:

    * ``awaiting-decision`` -- blocked on :data:`_PENDING_DECISION_TOOL_NAMES`
      (``AskUserQuestion``/``ExitPlanMode``) — a human must pick an option.
    * ``idle-finished`` -- the turn ended clean, nothing pending at all — the
      session doesn't actually need anyone, it's just holding a workspace.
    * ``awaiting-input`` -- everything else: a genuinely typed prompt is
      expected, some other/unrecognized tool_use is pending, or the tail
      gave no usable signal at all (no transcript, a missing file, an
      unparseable tail — see :func:`_pending_tool_use_names`). This is the
      old undifferentiated ``needs-you`` meaning, kept as the safe fallback
      for whatever this function can't positively classify — ``idle-finished``
      is a claim this function must have actually checked for, never a
      default for "couldn't check" (the same asymmetry the ``stalled``
      threshold applies to an unparseable launch timestamp).

    ``stalled`` is decided earlier, in :func:`_transcript_overlay` — it needs
    the pending *background* dispatch's age, which that function already
    computes; by the time this runs, ``status`` is never ``needs-you`` for a
    stalled dispatch (it's already ``stalled``). Any status other than
    ``needs-you`` (``working``, ``idle``, ``unknown``) passes through
    unchanged — the split is scoped to ``needs-you`` alone.
    """
    if status != "needs-you":
        return status
    pending_names = _pending_tool_use_names(transcript_path)
    if pending_names is None:
        return "awaiting-input"
    if pending_names & _PENDING_DECISION_TOOL_NAMES:
        return "awaiting-decision"
    if pending_names:
        return "awaiting-input"
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

# User lines whose string content is harness plumbing, not a typed prompt:
# slash-command wrappers, local-command output, background-task events.
_SKIP_USER_PREFIXES = (
    "<command-", "<local-command-", "<task-notification", "<system-reminder",
)

# Phone-drawer display caps — the ⚡ open-terminal button is the escape
# hatch for anything longer.
_ASSISTANT_TEXT_CAP = 6000
_USER_TEXT_CAP = 1500


def _assistant_text(content: Any) -> str:
    """Join the ``text`` blocks of an assistant message's content list."""
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text") or "").strip()
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n\n".join(p for p in parts if p)


def last_exchange(transcript_path: Any) -> Dict[str, Any]:
    """The last completed user→assistant exchange from a transcript JSONL.

    Reads the final :data:`_EXCHANGE_TAIL_BYTES`, walks the lines in reverse:
    the newest assistant line carrying a ``text`` block is the reply (earlier
    lines of the *same* ``message.id`` are prepended — transcripts write one
    line per content block); the nearest preceding user line whose content is
    a plain string is the prompt (list-shaped user content is tool results;
    harness wrappers like ``<command-…>`` are skipped). Missing file, no
    assistant text in the tail → ``{"available": False}`` — never an error.
    """
    unavailable: Dict[str, Any] = {"available": False, "user": None, "assistant": None}
    if not transcript_path:
        return unavailable
    lines, truncated = _tail_lines(transcript_path, _EXCHANGE_TAIL_BYTES)
    if truncated and lines:
        lines = lines[1:]  # first line is almost certainly a partial record

    assistant: Optional[Dict[str, Any]] = None
    assistant_msg_id: Any = None
    user: Optional[Dict[str, Any]] = None

    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}

        if assistant is None:
            if obj.get("type") != "assistant":
                continue
            text = _assistant_text(msg.get("content"))
            if not text:
                continue  # thinking / tool_use-only line
            assistant = {"text": text, "timestamp": obj.get("timestamp")}
            assistant_msg_id = msg.get("id")
            continue

        if (
            obj.get("type") == "assistant"
            and assistant_msg_id
            and msg.get("id") == assistant_msg_id
        ):
            text = _assistant_text(msg.get("content"))
            if text:
                assistant["text"] = text + "\n\n" + assistant["text"]
                assistant["timestamp"] = obj.get("timestamp") or assistant["timestamp"]
            continue

        if obj.get("type") == "user":
            content = msg.get("content")
            if not isinstance(content, str):
                continue  # tool results ride as content lists
            stripped = content.strip()
            if not stripped or any(
                stripped.startswith(p) for p in _SKIP_USER_PREFIXES
            ):
                continue
            user = {
                "text": stripped[:_USER_TEXT_CAP],
                "timestamp": obj.get("timestamp"),
            }
            break

    if assistant is None:
        return unavailable
    assistant["text"] = assistant["text"][-_ASSISTANT_TEXT_CAP:]
    return {"available": True, "user": user, "assistant": assistant}
