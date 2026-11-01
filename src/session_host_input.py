"""Server-initiated input-delivery protocol — issue #798.

Split out of ``src/session_host.py`` (a `/codebase-audit` maintainability
finding): bracketed-paste framing, bulk settle-then-submit and ingest/echo
verification (issues #611/#760) for the HTTP ``/input`` path, plus the
outcome vocabulary every input path reports in. Unlike
``session_host_scan.py``'s pure functions, this protocol is inherently
stateful against a live ``PtySession`` — the settle/defer state
(``_defer_seq``, ``_defer_args``, ``_output_total``, ``_last_output_at``) is
already explicit ``PtySession`` dataclass fields, so it stays there. What
moves is the *behaviour*: :class:`InputProtocol` is a mixin packaging every
method that reasons about that state. The deferred-submit watcher (#763)
extends it in :mod:`src.session_hosting.deferred_submit`, and ``PtySession``
inherits that, keeping only ``write()``/``_write_locked()`` — the raw PTY
chunk-and-pace write path (#64).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Settle-then-submit for bulk input — ported from
# app/webapp/static/terminal-compose.js's sendSubmit/bulkSettle,
# the WS/compose-bar path's fix for the same race the HTTP /input endpoint
# lacked: a bulk (dictation-sized) paste's bracketed-paste ingest can outrun
# a fixed delay under machine load, so a CR arriving too soon lands mid-ingest
# and becomes a literal newline into the still-settling composer instead of
# Submit, leaving the message stranded as an unsent "[Pasted text #N]" chip.
# These four values are copied verbatim from terminal-compose.js so the two
# paths cannot drift and quietly diverge again — see that file's calibration comment
# for the calibration data (echo-then-quiet was the only protocol that ran
# clean 20/20 against a live ConPTY probe under synthetic load).
_BULK_SUBMIT_THRESHOLD_CHARS = 500
_BULK_FLOOR_MS = 350
_BULK_QUIET_MS = 350
_BULK_CAP_MS = 3000
# Poll interval while waiting for the bulk-settle condition — matches the
# 50ms setInterval terminal-compose.js polls at.
BULK_POLL_S = 0.05

# Attachment settle — the /input half of terminal-compose.js's
# fix. Claude Code turns a pasted image path into an attachment
# asynchronously: it paints "Pasting…" at once and the "[Image #N]" chip only
# when done (~200 ms for 1.5 MB, ~400 ms for 11 MB on an idle box), and a CR
# that arrives in between is absorbed, leaving the prompt unsent in its
# composer. So a payload carrying an image path never takes the instant short
# path: it goes through the echo-then-settle loop below however short it is,
# and the stream does not count as settled while a "Pasting…" has no chip
# after it. The cap is terminal-compose.js's _ATTACH_CAP_MS verbatim, and it
# also bounds the ingest wait for such a payload, since its only evidence may
# be the chip painted at the end of the conversion; it stays inside
# session_client's _INPUT_TIMEOUT. An agent that never converts paths echoes
# the path as text and settles on the plain echo-then-quiet rule.
_ATTACH_CAP_MS = 10000
# A line that is nothing but an absolute path to an image file — how the
# composer appends an attachment (its own paragraph). The extensions
# are the ones Claude Code converts into an attachment.
_IMAGE_PATH_RE = re.compile(
    r"^\s*[\"']?(?:[a-z]:[\\/]|[\\/])[^\n\"']*\.(?:png|jpe?g|gif|webp)[\"']?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# What Claude Code paints while converting, normalized (see _normalize_echo):
# the hint first, then the chip in place of the path.
_ATTACH_START_MARKER = "pasting…"
_IMAGE_CHIP_MARKER = "[image#"

# Ingest verification for a server-initiated bulk write. The
# settle protocol above answers "has the terminal gone quiet", which is not
# the same question as "did my payload actually land": three workers went
# deaf to API input on 2026-08-14 while `_pty.write` kept returning without
# raising, so the endpoint answered {"ok": true} for messages that were
# never submitted (transcript evidence: the composer holding an unsent
# "[Pasted text #N]" chip while the agent worked on). A busy agent never
# goes quiet — its spinner repaints every few tens of ms — so the settle
# wait always ran out to _BULK_CAP_MS and fired the CR blind, mid-repaint,
# where Claude Code absorbs it as a newline instead of Submit.
#
# So before submitting we look for the payload in the PTY's *own output*:
# either the text echoed back, or the "[Pasted text #N]" chip Claude Code
# collapses a bulk paste into. No evidence → the payload demonstrably did
# not reach the terminal, so the CR is NOT written (a blind CR into an
# unknown TUI state is worse than no CR: with a modal dialog open — the
# other state observed in the 2026-08-14 transcripts — it selects a menu
# option) and the caller is told so, loudly.
#
# _INGEST_CAP_MS extends only the no-evidence-yet wait (under load the echo
# itself is deferred — see terminal-compose.js's calibration note), and stays well
# inside session_client's own input timeout so the caller sees this verdict
# rather than a client-side read timeout.
_INGEST_CAP_MS = 5000
# How much of the ring's newly-appended tail to normalize and scan.
_ECHO_SCAN_CHARS = 65536
# Normalized payload fragments used as echo needles: long enough not to
# collide with ordinary terminal furniture, short enough to survive the
# composer wrapping and decorating a long paste — and sampled across the
# payload so one mangled fragment doesn't hide the whole match.
_ECHO_FRAGMENT_CHARS = 24
_ECHO_SAMPLES = 4
# Claude Code's collapsed-paste chip, normalized (see _normalize_echo) —
# positive ingest evidence for a payload that is never echoed verbatim.
_PASTE_CHIP_MARKER = "[pastedtext#"
# The chip's *number*, which is what makes a chip ours rather than any chip.
# Claude Code renders "[Pasted text #<n> +53 lines]", which normalizes
# to "[pastedtext#<n>+53lines]" — _normalize_echo strips whitespace and
# box-drawing but keeps brackets, "#" and digits, so the number is free to
# read. Pastes are numbered monotonically within a session. An image chip
# is pinned the same way: "[Image #<n>]" normalizes to "[image#<n>]".
_CHIP_ID_RE = re.compile(r"(\[(?:pastedtext|image)#)(\d+)")

# Idle at the prompt while background agents run. When the main
# agent ends its turn with background sub-agents still working, Claude Code
# paints "✻ Waiting for N background agents to finish" once, then keeps a
# live row per agent below the prompt whose timer and token counter tick
# about once a second each, and animates the window title. The main agent is
# idle and a CR submits normally (a human typing into that terminal gets an
# answer), but the stream never has a _DEFER_QUIET_MS gap, so the deferred
# watcher waited until the agents finished or _DEFER_CAP_MS ran out.
#
# So that state is recognised from the screen, not from timing. Two markers,
# matched on _normalize_echo'd output (escapes and whitespace gone):
#
# - The wait line. Only its "Waiting for <N>" prefix is matched reliably:
#   Claude Code's differential renderer skips cells that already hold the
#   right character, and real sessions paint "backgrou\x1b[26Gd", "backg
#   \x1b[23Gou", "to\x1b[38Gfini\x1b[43Gh" and so on. The tail is loose
#   ("backg", or some letters then "agent") so a model's prose saying
#   "waiting for 2 minutes" is not taken for it.
# - A main-turn spinner: one of the spinner glyphs, repainted several times a
#   second for as long as a main turn runs, and painted first when a turn
#   starts ("✢ Topsy-turvying… (0s)"). "·" and "*" are spinner frames too but
#   are left out, being ordinary text; the other glyphs arrive within a
#   second anyway. The wait line's own glyph is the one exception.
#
# Whichever of the two came last decides: waiting means idle-with-agents,
# a spinner means a main turn. Checked against a live Opus session's
# transcript (27 transitions over ~50k frames, none spurious, idle stretches
# of 2k+ timer-only frames with no flip) and a throwaway probe session. The
# failure direction is safe: a missed wait line, or a spinner glyph that
# isn't one, leaves the watcher on the quiet-window rule it had before.
_BG_WAIT_RE = re.compile(r"waitingfor\d+(?:backg|[a-z]{0,10}agent)")
_MAIN_SPINNER_RE = re.compile(r"[✢✳✶✻✽](?!waitingfor)")
# Cheap pre-filter on the raw chunk: the reader thread sees every byte of
# every session, and most chunks (timer ticks, echoes, titles) carry neither.
_TURN_MARKER_HINTS = ("Waiting", "✢", "✳", "✶", "✻", "✽")
# An OSC (window title) cut off at the end of a read: _normalize_echo only
# strips terminated ones, and Claude Code's idle title starts with "✳".
_OSC_TAIL_RE = re.compile(r"\x1b\][^\x07\x1b]*$")

# What a keyboard write must carry to count as touching the composer
# (``PtySession._composer_touches``): any control character, which is
# every key that can submit, clear, delete, recall history or paste. Before
# the check, the replies a terminal emulator sends on its own are removed —
# focus in/out, device attributes, cursor position, mode reports, OSC/DCS
# strings — since an attached xterm sends those without anybody typing. An
# unrecognised sequence counts as a touch: the watcher then falls back to
# reading the screen, which is the earlier rule, never something riskier.
TERMINAL_REPLY_RE = re.compile(
    r"\x1b\[(?:[IO]|[?>][\d;]*[cu]|\?[\d;]*\$y|\d+;\d+R)"
    r"|\x1b[\]P][^\x07\x1b]*(?:\x07|\x1b\\)"
)
_CONTROL_KEY_RE = re.compile(r"[\x00-\x1f\x7f]")


def touches_composer(data: str) -> bool:
    """Can this keyboard write change what the composer holds? (#1548)

    Plain typed text cannot remove a payload already there, so it does not
    count — consistent with a human typing never superseding the watcher.
    """
    return _CONTROL_KEY_RE.search(TERMINAL_REPLY_RE.sub("", data)) is not None


# Outcome reasons for a server-initiated write. Distinct
# conditions get distinct reasons — "couldn't establish delivery" is never
# folded into the success state.
INPUT_OK = "ok"                        # landed; submit (if asked) confirmed
INPUT_UNVERIFIED = "unverified"        # short payload: submitted un-verified
INPUT_NOT_INGESTED = "not_ingested"    # written, never echoed → not delivered
INPUT_DROPPED = "dropped"              # a write never reached the PTY
INPUT_NOOP = "noop"                    # nothing was asked for
# Deferred-submit reasons. INPUT_DEFERRED is what the /input
# call itself returns once the watcher is armed; the others are the
# watcher's own terminal verdicts, recorded onto ``last_input`` when it
# finishes. It replaces the former "settle_cap" reason, which reported a CR
# that had already been fired blind — there is no longer such a CR to report.
INPUT_DEFERRED = "deferred"            # ingested; submit handed to the watcher
INPUT_DEFER_TIMEOUT = "defer_timeout"  # never went quiet within _DEFER_CAP_MS
INPUT_DEFER_VANISHED = "defer_vanished"  # composer touched, payload not on screen
INPUT_DEFER_UNCLEAR = "defer_unclear"  # payload there, but a dialog too
# The watcher itself failed before writing any CR. Recorded so
# a broken watcher ends on a verdict instead of leaving ``last_input`` at
# ``pending`` for good.
INPUT_DEFER_FAILED = "defer_failed"
# The owner discarded the payload while it waited in the composer: the
# watcher cleared it with Ctrl-U instead of pressing Enter.
INPUT_DISCARDED = "discarded"
# Refused before anything was written: a discarded payload is still waiting
# to be cleared, and a new write would land behind it and carry it along.
INPUT_DISCARD_PENDING = "discard_pending"
# Detached (console) targets only — RemoteSession.submit_input:
# the console-input helper could not attach/type (a distinct 502 from
# ``not_ingested``: nothing was written, the console may be gone).
INPUT_CONSOLE_FAILED = "console_failed"
# The ``delivered`` value a detached target reports. A console
# has no output stream to verify a write against, so the answer is neither
# True nor False — a third state, never folded into the passing one.
DELIVERED_UNCONFIRMED = "unconfirmed"

# What happened to the submit, as one field a caller can read without
# re-deriving it from ``reason`` + ``submitted`` + ``submit_confirmed``.
# "Submitted but unconfirmed" and "never submitted" are
# distinct states, and neither is folded into "confirmed".
SUBMIT_CONFIRMED = "confirmed"          # CR sent after a verified settle
SUBMIT_UNCONFIRMED = "unconfirmed"      # CR sent, nothing verified it landed
SUBMIT_PENDING = "pending"              # CR is with the deferred watcher
SUBMIT_NOT_SUBMITTED = "not_submitted"  # asked for, and no CR was ever sent
SUBMIT_NOT_REQUESTED = "not_requested"  # the call did not ask for a submit


@dataclass(frozen=True)
class InputOutcome:
    """What actually happened to one server-initiated write (issue #760).

    ``reason`` is the single source of truth; the booleans are the detail a
    caller renders. ``ingested``/``submit_confirmed`` are tri-state on
    purpose — ``None`` means *not established*, which is never the same as
    ``False`` and never folded into the success state.
    """

    reason: str
    ingested: Optional[bool] = None
    submitted: bool = False
    submit_confirmed: Optional[bool] = None
    waited_ms: int = 0
    # Whether the deferred-submit watcher is involved: True on
    # the ``deferred`` verdict the /input call returns once the watcher is
    # armed, and on every verdict the watcher itself later records. Lets a
    # reader tell "the submit is still coming" apart from a finished call.
    deferred: bool = False
    # Whether the call asked for a submit at all. Defaults to
    # True so an outcome built without saying reads as a submit that did not
    # happen — never as a plain paste that needed none. ``submit_input``
    # stamps the real value from its own ``submit`` argument.
    submit_requested: bool = True
    # The submit was taken into the agent's own queue: Enter
    # went in mid-turn and the agent showed the message queued for when the
    # turn yields. Only ever True next to a confirmed submit.
    queued: bool = False

    @property
    def submit_state(self) -> str:
        """The submit's fate as one of the ``SUBMIT_*`` values (issue #929)."""
        if self.submitted:
            return SUBMIT_CONFIRMED if self.submit_confirmed is True else SUBMIT_UNCONFIRMED
        if not self.submit_requested:
            return SUBMIT_NOT_REQUESTED
        if self.reason == INPUT_DEFERRED:
            return SUBMIT_PENDING
        return SUBMIT_NOT_SUBMITTED

    @property
    def delivered(self) -> bool:
        """Whether everything this call asked for actually reached the agent.

        ``INPUT_NOT_INGESTED`` counts as *not* delivered: the write returned
        without raising, but the terminal never showed the payload, which is
        exactly the silent drop #760 was filed over.

        A submit that was asked for and not sent is not delivered either
        (issue #929) — including ``deferred`` (the CR is still with the
        watcher) and every watcher give-up (``defer_timeout`` /
        ``defer_vanished`` / ``defer_unclear``). The payload reached the
        composer, but a paste sitting unsubmitted there has not reached the
        agent, and ``delivered`` must never be more optimistic than
        ``submitted``. Before #929 all of those read ``delivered: true`` next
        to ``submitted: false``, and three stranded briefs were reported as
        landed. ``submit_state`` says which of those it was.
        """
        if self.reason in (INPUT_DROPPED, INPUT_NOT_INGESTED):
            return False
        return self.submit_state in (
            SUBMIT_CONFIRMED, SUBMIT_UNCONFIRMED, SUBMIT_NOT_REQUESTED
        )

    def to_api(self) -> Dict[str, Any]:
        return {
            "delivered": self.delivered,
            "reason": self.reason,
            "ingested": self.ingested,
            "submitted": self.submitted,
            "submit_confirmed": self.submit_confirmed,
            "submit_state": self.submit_state,
            "waited_ms": self.waited_ms,
            "deferred": self.deferred,
            "queued": self.queued,
        }


# Any escape sequence a TUI paints its composer with — CSI (cursor moves,
# SGR), OSC (titles), and the two-char ESC forms. Stripped before matching a
# payload against the terminal's own output, since a wrapped, re-decorated
# echo of "CHIEF - do X" is interleaved with dozens of these.
_ECHO_ESCAPE_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]"
)
# Whitespace and box-drawing runs: the two kinds of noise a TUI sprays
# *through* an echoed payload. A bordered composer paints "│ text │" on
# every wrapped line, so the border characters land inside the text as
# reliably as the wrap itself does. Both sides of the comparison get the
# same treatment, so a payload that genuinely contains box-drawing still
# matches itself.
_ECHO_NOISE_RE = re.compile(r"[\s─-╿]+")


def _normalize_echo(text: str) -> str:
    """Escape-free, whitespace-free, lowercased form of terminal text.

    Dropping *all* whitespace and border decoration is what makes an echo
    match survive the composer: the same payload comes back hard-wrapped at
    the terminal width, re-indented inside a box-drawn frame, and split
    across repaints, so any newline- or column-sensitive comparison misses
    it. What survives that treatment is the sequence of content characters.
    """
    return _ECHO_NOISE_RE.sub("", _ECHO_ESCAPE_RE.sub("", text)).lower()


def _echo_needles(data: str) -> List[str]:
    """Normalized fragments of ``data`` to look for in the PTY's output.

    Several fragments sampled across the payload rather than one, because
    any single one can be broken up by decoration this normalizer doesn't
    know about (an agent that numbers wrapped lines, a truncation ellipsis
    in the middle of a collapsed paste). A match on *any* of them is
    evidence the payload reached the terminal.

    Empty list when the payload normalizes to less than one fragment's
    worth of content — the caller treats that as "nothing to match on"
    rather than as a failed match.
    """
    normalized = _normalize_echo(data)
    if len(normalized) < _ECHO_FRAGMENT_CHARS:
        return []
    step = max(1, (len(normalized) - _ECHO_FRAGMENT_CHARS) // _ECHO_SAMPLES)
    return [
        normalized[i : i + _ECHO_FRAGMENT_CHARS]
        for i in range(0, len(normalized) - _ECHO_FRAGMENT_CHARS + 1, step)
    ][:_ECHO_SAMPLES]


def _carries_image_path(data: str) -> bool:
    """Does ``data`` hold an attached image path (issue #1212)?"""
    return _IMAGE_PATH_RE.search(data) is not None


def attach_converting(normalized: str) -> bool:
    """Is an image conversion still in flight in ``normalized`` output?

    True while the newest "Pasting…" has no image chip painted after it —
    terminal-compose.js's ``attachStartAt``/``attachChipAt`` comparison
    (#1211), read off the ring instead of per-frame timestamps.
    """
    start = normalized.rfind(_ATTACH_START_MARKER)
    return start != -1 and normalized.find(_IMAGE_CHIP_MARKER, start) == -1


def _chip_id_in(normalized: str, image: bool = False) -> Optional[str]:
    """The newest chip in ``normalized`` terminal output (#1075), as its
    normalized token — ``"[pastedtext#7"``, or ``"[image#4"`` when ``image``.

    Highest rather than last-in-paint-order: a composer holding several chips
    repaints them in ascending order, and Claude Code numbers pastes
    monotonically, so within a window that begins at our *own* write the
    largest number is the chip our write created. ``image`` (#1212) pins the
    chip an image payload's path was converted into; a text payload keeps
    pinning its collapsed-paste chip.

    ``None`` when the output shows no such chip — the ordinary case for a
    bulk payload small enough that the composer echoes it verbatim instead of
    collapsing it, or for an agent that echoes an image path as text. The
    caller then falls back to the payload's own echo, which is the only
    evidence there is.
    """
    marker = _IMAGE_CHIP_MARKER if image else _PASTE_CHIP_MARKER
    ids = [num for prefix, num in _CHIP_ID_RE.findall(normalized) if prefix == marker]
    return marker + max(ids, key=int) if ids else None


def _scan_turn_state(chunk: str) -> Optional[bool]:
    """What a chunk of raw PTY output says about the main agent (#1319).

    ``True`` when the last status marker in ``chunk`` is the "Waiting for N
    background agents" line (idle at the prompt, agents running), ``False``
    when it is a main-turn spinner, ``None`` when it carries neither, which
    leaves the previous verdict standing. See ``_BG_WAIT_RE``.
    """
    if not any(hint in chunk for hint in _TURN_MARKER_HINTS):
        return None
    normalized = _normalize_echo(_OSC_TAIL_RE.sub("", chunk))
    wait_at = max((m.start() for m in _BG_WAIT_RE.finditer(normalized)), default=-1)
    spin_at = max((m.start() for m in _MAIN_SPINNER_RE.finditer(normalized)), default=-1)
    if wait_at == spin_at:  # both -1: a hint matched, no marker did
        return None
    return wait_at > spin_at


def chip_visible(normalized: str, chip_id: str) -> bool:
    """Is *this* chip (a :func:`_chip_id_in` token) showing in ``normalized``
    terminal output?

    The trailing boundary is the whole point: without it ``#1`` also matches
    ``#12``, which hands back exactly the any-chip-will-do answer this exists
    to avoid.
    """
    pattern = re.escape(chip_id) + r"(?!\d)"
    return re.search(pattern, normalized) is not None


class InputProtocol:
    """Mixin: ``PtySession``'s server-initiated input-delivery protocol.

    Every method here reasons about ``PtySession`` state (``_ring``,
    ``_ring_lock``, ``_output_total``, ``_last_output_at``, ``_write_lock``,
    ``_defer_seq``, ``_defer_args``, ``last_input``, …) declared on the
    dataclass itself — this mixin supplies only the behaviour, never the
    fields, so ``PtySession`` picks it up unchanged for every existing caller
    (``session.submit_input(...)`` etc.). ``_record_input`` and
    ``_arm_deferred_submit``, which ``submit_input`` calls, come from
    :class:`~src.session_hosting.deferred_submit.DeferredSubmit`.
    """

    def _track_turn_state(self, chunk: str) -> None:
        """Reader-thread hook: fold one output chunk into the turn state.

        Called with every chunk after it is counted into ``_output_total``,
        so ``_main_turn_at`` is a position a CR's own mark can be compared
        against (#1319). State rather than a scan of recent output, because
        the wait line is painted once and then scrolls out of any window
        while the agent rows tick on for as long as the agents run.
        """
        state = _scan_turn_state(chunk)
        if state is None:
            return
        self._bg_waiting = state
        if not state:
            self._main_turn_at = self._output_total
            self._main_turn_seen_at = time.time()

    def _normalized_since(self, mark: int) -> str:
        """Normalized form of everything the PTY has painted since ``mark``.

        ``mark`` is an ``_output_total`` reading, so the window is defined by
        the monotonic output counter rather than by ring offsets — it stays
        correct across a ring that has wrapped. Empty string when nothing has
        been painted since.
        """
        with self._ring_lock:
            appended = self._output_total - mark
            if appended <= 0:
                return ""
            window = self._ring[-min(appended, len(self._ring), _ECHO_SCAN_CHARS):]
        return _normalize_echo(window)

    @staticmethod
    def _payload_visible(
        normalized: str, needles: List[str], image: bool = False
    ) -> bool:
        """Does ``normalized`` terminal output show this payload?

        Evidence is either the payload itself coming back (normalized, so
        wrapping and box-drawing don't hide it) or Claude Code's
        collapsed-paste chip, which replaces the echo for a bulk paste. For a
        payload carrying an image path (``image``, #1212) the "[Image #N]"
        chip counts too: Claude Code replaces the path with it, so the path
        itself is never echoed.
        """
        if _PASTE_CHIP_MARKER in normalized:
            return True
        if image and _IMAGE_CHIP_MARKER in normalized:
            return True
        return any(needle in normalized for needle in needles)

    def submit_input(self, data: str, submit: bool) -> "InputOutcome":
        """Write ``data`` and, if ``submit``, follow it with a submitting CR.

        Ported from ``app/webapp/static/terminal-compose.js``'s
        ``framePaste``/``sendSubmit``/``bulkSettle`` (issues #166/#450/#499)
        for the HTTP ``/input`` path (issue #611), which previously wrote the
        (unconditionally bracketed) text and the CR back-to-back with no
        settle logic — Claude Code's composer classifies a bulk write as a
        paste, and a CR arriving mid-ingest is absorbed as a literal newline
        instead of Submit, stranding the message as an unsent
        ``[Pasted text #N]`` chip while the caller was told ``ok: true``.

        - Bracketed-paste framing (``\\x1b[200~ … \\x1b[201~``) is applied
          only when ``self._bracketed_paste_mode`` is currently on (tracked
          from the PTY's own DECSET 2004 output — see
          ``_scan_bracketed_paste_mode``), matching ``framePaste``'s gate on
          xterm's client-side ``term.modes.bracketedPasteMode`` exactly: a
          literal ``\\x1b[200~`` sent to an agent that never asked for it is
          garbage, not a paste.
        - The CR is always its own separate ``write()`` call, never
          concatenated onto the framed text (#166).
        - ``data`` blank + ``submit`` → a bare submit against whatever is
          already sitting in the composer, with no text write at all — the
          escape hatch for a message already stranded by this exact race
          (previously both ``{"data": "", "submit": true}`` and any other
          blank-data call 400'd, leaving no recovery but a human tapping the
          phone's own Send control).
        - Payloads at/above ``_BULK_SUBMIT_THRESHOLD_CHARS`` hold the CR
          until the session's output stream shows the paste was echoed and
          has gone quiet (floor/quiet/cap — #499); shorter payloads submit
          immediately, matching ``sendSubmit``'s "short sends stay instant".
        - A payload carrying an image path takes that same settle path
          however short it is, and also waits out an image conversion still
          in flight, up to ``_ATTACH_CAP_MS`` (issue #1212) — ``sendSubmit``'s
          ``attachSettle`` (#1211).
        - A bulk payload is additionally checked for *ingest evidence*
          before the CR is written at all (issue #760): the terminal must
          have painted the payload back (or its ``[Pasted text #N]`` chip)
          since the write. No evidence within ``_INGEST_CAP_MS`` → the CR is
          withheld and the outcome says ``not_ingested``, because a blind CR
          into a terminal that never showed the text can do real damage (it
          answers whatever modal dialog is open).
        - A bulk payload that *was* ingested but whose stream never goes
          quiet — a busy agent, the 2026-08-14 shape — no longer gets a blind
          CR at the settle cap either (issue #763). The submit is handed to a
          bounded background watcher instead, the call returns ``deferred``,
          and ``_write_lock`` is released immediately so a keyboard writer is
          never blocked behind the wait. For an agent that queues type-ahead
          the watcher presses Enter at once while the turn runs (issue
          #1548). See :meth:`_await_deferred_submit`.

        Returns an :class:`InputOutcome` describing what actually happened —
        superseding the old bare ``bool`` (#607), which could only say "the
        write didn't raise" and so reported ``True`` for every silently
        stranded steer in #760's three occurrences.

        The whole sequence runs under ``_write_lock`` (issue #721): the CR is
        a separate PTY write from the text it submits, so without the lock a
        concurrent writer — the WebSocket pump serving a PC mirror on the
        same session — can land its own bytes *between* them and turn a
        submit into a literal newline in somebody else's message. The
        ingest wait extends the hold to at most ``_INGEST_CAP_MS``, and only
        on the failing path, so a keyboard writer is never blocked for long.
        """
        with self._write_lock:
            clean_before = self._composer_known_empty()
            mark = self._output_total
            # Refused before the write and its supersede: the watcher that
            # clears a discarded payload must keep running.
            outcome = replace(
                InputOutcome(reason=INPUT_DISCARD_PENDING)
                if self._discard_pending()
                else self._submit_input_locked(data, submit),
                submit_requested=submit,
            )
            defer_args = self._defer_args
            self._defer_args = None
            self._track_pending(data, outcome, mark, clean_before)
        self._record_input(outcome, len(data), submit)
        # Armed only after the ``deferred`` verdict is already on the session,
        # so the watcher can never overwrite ``last_input`` with its own
        # outcome before the call that created it has recorded one.
        if defer_args is not None:
            self._arm_deferred_submit(*defer_args, nbytes=len(data))
        return outcome

    def _await_bulk_settle(
        self, mark: int, sent_at: float, needles: List[str], image: bool
    ) -> Tuple[bool, bool, bool]:
        """Poll until a bulk payload written at ``mark`` has echoed and gone
        quiet, or a cap runs out. Returns ``(ingested, settled, exited)``."""
        floor_at = sent_at + _BULK_FLOOR_MS / 1000
        settle_deadline = sent_at + (_ATTACH_CAP_MS if image else _BULK_CAP_MS) / 1000
        ingest_deadline = sent_at + (_ATTACH_CAP_MS if image else _INGEST_CAP_MS) / 1000
        quiet_s = _BULK_QUIET_MS / 1000
        ingested = False
        converting = False
        settled = False
        # Only rescan when the terminal has actually painted something new —
        # this loop polls 20×/s on a process hosting every live PTY, and in
        # the case that matters most (nothing coming back at all) there is
        # nothing new to normalize on any of those passes. Only output painted
        # *after* ``mark`` counts, so an identical payload sent earlier in the
        # session can't be mistaken for this one's echo.
        scanned_at = mark
        while True:
            if self._exited:
                return ingested, settled, True
            if (not ingested or image) and self._output_total > scanned_at:
                scanned_at = self._output_total
                normalized = self._normalized_since(mark)
                ingested = ingested or self._payload_visible(normalized, needles, image)
                converting = image and attach_converting(normalized)
            now = time.time()
            settled = (
                now >= floor_at
                and self._last_output_at > sent_at
                and (now - self._last_output_at) >= quiet_s
                and not converting
            )
            if ingested and (settled or now >= settle_deadline):
                return ingested, settled, False
            # Not ingested yet: keep looking past the settle cap — under load
            # the echo itself is deferred (terminal-compose.js's calibration note) —
            # but never past the ingest cap.
            if now >= ingest_deadline:
                return ingested, settled, False
            time.sleep(BULK_POLL_S)

    def _submit_input_locked(self, data: str, submit: bool) -> "InputOutcome":
        """:meth:`submit_input`'s body, assuming ``_write_lock`` is held."""
        # Any new server-initiated write supersedes a watcher still waiting to
        # press Enter for an earlier payload: the composer no longer
        # holds only that payload, so the watcher's pre-fire re-check would be
        # reasoning about a terminal state that has already moved on.
        self._defer_seq += 1
        if not data:
            if not submit:
                return InputOutcome(reason=INPUT_NOOP)
            if not self._write_locked("\r"):
                return InputOutcome(reason=INPUT_DROPPED)
            # A bare submit carries no payload to look for, so there is
            # nothing to verify — it releases whatever the composer already
            # holds. Honest name for that: unverified, not "ok".
            return InputOutcome(reason=INPUT_UNVERIFIED, submitted=True)
        framed = "\x1b[200~" + data + "\x1b[201~" if self._bracketed_paste_mode else data
        mark = self._output_total
        touches = self._composer_touches
        sent_at = time.time()
        if not self._write_locked(framed):
            return InputOutcome(reason=INPUT_DROPPED)
        image = _carries_image_path(data)
        if len(data) < _BULK_SUBMIT_THRESHOLD_CHARS and not image:
            # Short sends stay instant (sendSubmit's own rule) — and so stay
            # unverified: there is no settle window to observe an echo in.
            if not submit:
                return InputOutcome(reason=INPUT_UNVERIFIED)
            if not self._write_locked("\r"):
                return InputOutcome(reason=INPUT_DROPPED)
            return InputOutcome(reason=INPUT_UNVERIFIED, submitted=True)
        needles = _echo_needles(data)
        ingested, settled, exited = self._await_bulk_settle(
            mark, sent_at, needles, image
        )
        waited_ms = int((time.time() - sent_at) * 1000)
        if exited:
            return InputOutcome(
                reason=INPUT_DROPPED, ingested=ingested, waited_ms=waited_ms
            )
        if not ingested:
            # The write returned without raising, yet the terminal never
            # painted the payload: treat it as undelivered and withhold the
            # CR rather than firing it into an unknown TUI state.
            return InputOutcome(
                reason=INPUT_NOT_INGESTED, ingested=False, waited_ms=waited_ms
            )
        if not submit:
            return InputOutcome(reason=INPUT_OK, ingested=True, waited_ms=waited_ms)
        if not settled:
            # Ingested, but the stream never went quiet: the agent is working
            # and repainting. The CR used to go out here anyway, into a
            # terminal mid-repaint — exactly the window Claude Code absorbs a
            # CR as a literal newline in, which is how three steers ended up
            # stranded on 2026-08-14. Now nothing is written: the submit is
            # handed to a watcher that presses Enter when the agent actually
            # settles, the way a human at the keyboard would.
            #
            # Pin the chip *here* rather than at fire time: this
            # window opens at our own write, so the newest chip in it is
            # unambiguously the one our paste created. Minutes later, when
            # the watcher looks again, a chip in the composer could be
            # anyone's.
            self._defer_args = (
                self._defer_seq,
                mark,
                needles,
                _chip_id_in(self._normalized_since(mark), image),
                touches,
            )
            return InputOutcome(
                reason=INPUT_DEFERRED,
                ingested=True,
                deferred=True,
                waited_ms=waited_ms,
            )
        if not self._write_locked("\r"):
            return InputOutcome(
                reason=INPUT_DROPPED, ingested=True, waited_ms=waited_ms
            )
        return InputOutcome(
            reason=INPUT_OK,
            ingested=True,
            submitted=True,
            submit_confirmed=True,
            waited_ms=waited_ms,
        )
