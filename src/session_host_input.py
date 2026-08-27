"""Server-initiated input-delivery protocol — issue #798.

Split out of ``src/session_host.py`` (a `/codebase-audit` maintainability
finding): bracketed-paste framing, bulk settle-then-submit, ingest/echo
verification, and the deferred-submit watcher thread (issues #611/#760/#763)
for the HTTP ``/input`` path. Unlike ``session_host_scan.py``'s pure
functions, this protocol is inherently stateful against a live
``PtySession`` — the settle/defer state (``_defer_seq``, ``_defer_args``,
``_output_total``, ``_last_output_at``) is already explicit ``PtySession``
dataclass fields, so it stays there. What moves is the *behaviour*:
:class:`InputProtocol` is a mixin ``PtySession`` inherits from, packaging
every method that reasons about that state. ``PtySession`` itself keeps only
``write()``/``_write_locked()`` — the raw PTY chunk-and-pace write path
(#64) — and gets ``submit_input`` and everything downstream of it through
this mixin.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Settle-then-submit for bulk input (issue #611) — ported from
# app/webapp/static/terminal-compose.js's sendSubmit/bulkSettle (#166/#499),
# the WS/compose-bar path's fix for the same race the HTTP /input endpoint
# lacked: a bulk (dictation-sized) paste's bracketed-paste ingest can outrun
# a fixed delay under machine load, so a CR arriving too soon lands mid-ingest
# and becomes a literal newline into the still-settling composer instead of
# Submit, leaving the message stranded as an unsent "[Pasted text #N]" chip.
# These four values are copied verbatim from terminal-compose.js so the two
# paths cannot drift and quietly diverge again — see that file's #499 comment
# for the calibration data (echo-then-quiet was the only protocol that ran
# clean 20/20 against a live ConPTY probe under synthetic load).
_BULK_SUBMIT_THRESHOLD_CHARS = 500
_BULK_FLOOR_MS = 350
_BULK_QUIET_MS = 350
_BULK_CAP_MS = 3000
# Poll interval while waiting for the bulk-settle condition — matches the
# 50ms setInterval terminal-compose.js polls at.
_BULK_POLL_S = 0.05

# Ingest verification for a server-initiated bulk write (issue #760). The
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
# itself is deferred — see terminal-compose.js's #499 note), and stays well
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

# Deferred submit (issue #763, closing #760's third acceptance point: "if the
# terminal can take input, so can the API"). A *working* agent repaints its
# spinner every few tens of ms, so the settle wait above can never see a quiet
# window for as long as it works: it runs out to _BULK_CAP_MS with the payload
# ingested but still sitting unsent in the composer. Firing the CR there —
# what this module did before #763 — is the 2026-08-14 stall shape exactly:
# mid-repaint, Claude Code absorbs a CR as a literal newline.
#
# A human at the keyboard does not hit this. They see the stranded
# "[Pasted text #N]" chip, wait for the agent to settle, and press Enter. The
# watcher below is that retry-by-observation and nothing more: it re-presses
# Enter, and it never resends the *text* (a blind resend of something like
# "/issue-finish" could double-execute — the same reason chief_ops.py's
# `say --verify` deliberately has no auto-retry).
#
# _DEFER_QUIET_MS is deliberately far longer than _BULK_QUIET_MS: 350 ms only
# has to outlast one paste's ingest, whereas this has to outlast the repaint
# gaps of a working agent and actually mean "the turn is over".
_DEFER_QUIET_MS = 1500
# Bounded: past this the steer stays stranded and honestly reported, which is
# the pre-#763 behaviour. An unbounded watcher would keep a CR primed against
# a terminal whose state drifted arbitrarily far from the one it verified.
_DEFER_CAP_MS = 120_000
_DEFER_POLL_S = 0.25
# How far back the pre-fire re-check looks for the payload. The composer is
# repainted on essentially every frame, so a payload still sitting unsent
# keeps reappearing, whereas one that was submitted (or scrolled away behind
# a dialog) stops being repainted and only survives in older scrollback.
# Sampling _output_total on every poll and scanning from the oldest retained
# sample is what turns "the payload was there once" into "it is there now".
_DEFER_FRAME_LOOKBACK_MS = 6000
_DEFER_FRAME_SAMPLES = max(2, int(_DEFER_FRAME_LOOKBACK_MS / 1000 / _DEFER_POLL_S))
# Defence in depth for the one case the positive check above cannot rule out
# by itself: a permission / AskUserQuestion dialog painted *over* a composer
# that still shows the chip, where a bare CR picks a menu option instead of
# submitting (the second terminal state seen in the 2026-08-14 transcripts).
# Normalized (see _normalize_echo) markers for the dialog shapes Claude Code
# paints. This is a heuristic and is allowed to be over-eager: a false
# positive only means the watcher declines to press Enter, leaving the steer
# stranded and honestly reported — never a CR into a dialog.
_DEFER_DIALOG_MARKERS = ("doyouwanttoproceed", "❯1.", "❯2.")

# Outcome reasons for a server-initiated write (issue #760). Distinct
# conditions get distinct reasons — "couldn't establish delivery" is never
# folded into the success state.
INPUT_OK = "ok"                        # landed; submit (if asked) confirmed
INPUT_UNVERIFIED = "unverified"        # short payload: submitted un-verified
INPUT_NOT_INGESTED = "not_ingested"    # written, never echoed → not delivered
INPUT_DROPPED = "dropped"              # a write never reached the PTY
INPUT_NOOP = "noop"                    # nothing was asked for
# Deferred-submit reasons (issue #763). INPUT_DEFERRED is what the /input
# call itself returns once the watcher is armed; the other three are the
# watcher's own terminal verdicts, recorded onto ``last_input`` when it
# finishes. It replaces the former "settle_cap" reason, which reported a CR
# that had already been fired blind — there is no longer such a CR to report.
INPUT_DEFERRED = "deferred"            # ingested; submit handed to the watcher
INPUT_DEFER_TIMEOUT = "defer_timeout"  # never went quiet within _DEFER_CAP_MS
INPUT_DEFER_VANISHED = "defer_vanished"  # quiet, but the payload is gone
INPUT_DEFER_UNCLEAR = "defer_unclear"  # quiet, payload there, but a dialog too


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
    # Whether the deferred-submit watcher (issue #763) is involved: True on
    # the ``deferred`` verdict the /input call returns once the watcher is
    # armed, and on every verdict the watcher itself later records. Lets a
    # reader tell "the submit is still coming" apart from a finished call.
    deferred: bool = False

    @property
    def delivered(self) -> bool:
        """Whether everything this call attempted actually reached the PTY.

        ``INPUT_NOT_INGESTED`` counts as *not* delivered: the write returned
        without raising, but the terminal never showed the payload, which is
        exactly the silent drop #760 was filed over. The deferred-submit
        verdicts (#763) all stay *delivered*: the payload demonstrably
        reached the composer — it is only the submitting CR that is pending,
        withheld, or refused, and ``submit_confirmed`` says which.
        """
        return self.reason not in (INPUT_DROPPED, INPUT_NOT_INGESTED)

    def to_api(self) -> Dict[str, Any]:
        return {
            "delivered": self.delivered,
            "reason": self.reason,
            "ingested": self.ingested,
            "submitted": self.submitted,
            "submit_confirmed": self.submit_confirmed,
            "waited_ms": self.waited_ms,
            "deferred": self.deferred,
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


class InputProtocol:
    """Mixin: ``PtySession``'s server-initiated input-delivery protocol.

    Every method here reasons about ``PtySession`` state (``_ring``,
    ``_ring_lock``, ``_output_total``, ``_last_output_at``, ``_write_lock``,
    ``_defer_seq``, ``_defer_args``, ``last_input``, …) declared on the
    dataclass itself — this mixin supplies only the behaviour, never the
    fields, so ``PtySession(InputProtocol)`` picks it up unchanged for every
    existing caller (``session.submit_input(...)`` etc.).
    """

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
    def _payload_visible(normalized: str, needles: List[str]) -> bool:
        """Does ``normalized`` terminal output show this payload?

        Evidence is either the payload itself coming back (normalized, so
        wrapping and box-drawing don't hide it) or Claude Code's
        collapsed-paste chip, which replaces the echo for a bulk paste.
        """
        if _PASTE_CHIP_MARKER in normalized:
            return True
        return any(needle in normalized for needle in needles)

    def _echo_seen_since(self, mark: int, needles: List[str]) -> bool:
        """Has the PTY painted evidence of this payload since ``mark``?

        Only output the terminal produced *after* ``mark`` counts — an
        identical payload sent earlier in the session can't be mistaken for
        this one's echo.
        """
        return self._payload_visible(self._normalized_since(mark), needles)

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
          never blocked behind the wait. See :meth:`_await_deferred_submit`.

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
            outcome = self._submit_input_locked(data, submit)
            defer_args = self._defer_args
            self._defer_args = None
        self._record_input(outcome, len(data), submit)
        # Armed only after the ``deferred`` verdict is already on the session,
        # so the watcher can never overwrite ``last_input`` with its own
        # outcome before the call that created it has recorded one.
        if defer_args is not None:
            self._arm_deferred_submit(*defer_args, nbytes=len(data))
        return outcome

    def _record_input(
        self, outcome: "InputOutcome", nbytes: int, submit: bool
    ) -> None:
        """Record one server-initiated write's verdict and log a breadcrumb.

        Shared by :meth:`submit_input` and the deferred-submit watcher (#763),
        so a deferred outcome lands on ``last_input`` in exactly the same
        shape as an immediate one and ``to_api()`` needs no special case.
        """
        self.last_input = {
            **outcome.to_api(),
            "at": time.time(),
            "bytes": nbytes,
            "submit": submit,
        }
        # Breadcrumbs for the next occurrence (#760): the whole defect was
        # invisible in the logs, so both an outright non-delivery and an
        # unconfirmed submit — the shape all three 2026-08-14 stalls took —
        # get an INFO line naming which condition it was.
        sid = self.session_id[:8]
        if not outcome.delivered:
            logger.info(
                f"⚠️ PTY {sid} input not delivered "
                f"({outcome.reason}, {nbytes} chars, "
                f"waited {outcome.waited_ms}ms)"
            )
        elif outcome.reason == INPUT_DEFERRED:
            logger.info(
                f"⏳ PTY {sid} input ingested but the agent is still busy — "
                f"submit deferred to a watcher ({nbytes} chars, waited "
                f"{outcome.waited_ms}ms, window {_DEFER_CAP_MS}ms)"
            )
        elif outcome.deferred and outcome.reason == INPUT_OK:
            logger.info(
                f"✅ PTY {sid} deferred submit landed after "
                f"{outcome.waited_ms}ms ({nbytes} chars)"
            )
        elif outcome.submit_confirmed is False:
            logger.info(
                f"ℹ️ PTY {sid} input ingested but not submitted "
                f"({outcome.reason}, {nbytes} chars, waited "
                f"{outcome.waited_ms}ms)"
            )

    def _submit_input_locked(self, data: str, submit: bool) -> "InputOutcome":
        """:meth:`submit_input`'s body, assuming ``_write_lock`` is held."""
        # Any new server-initiated write supersedes a watcher still waiting to
        # press Enter for an earlier payload (#763): the composer no longer
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
        sent_at = time.time()
        if not self._write_locked(framed):
            return InputOutcome(reason=INPUT_DROPPED)
        if len(data) < _BULK_SUBMIT_THRESHOLD_CHARS:
            # Short sends stay instant (sendSubmit's own rule) — and so stay
            # unverified: there is no settle window to observe an echo in.
            if not submit:
                return InputOutcome(reason=INPUT_UNVERIFIED)
            if not self._write_locked("\r"):
                return InputOutcome(reason=INPUT_DROPPED)
            return InputOutcome(reason=INPUT_UNVERIFIED, submitted=True)
        needles = _echo_needles(data)
        floor_at = sent_at + _BULK_FLOOR_MS / 1000
        settle_deadline = sent_at + _BULK_CAP_MS / 1000
        ingest_deadline = sent_at + _INGEST_CAP_MS / 1000
        quiet_s = _BULK_QUIET_MS / 1000
        ingested = False
        settled = False
        exited = False
        # Only rescan when the terminal has actually painted something new —
        # this loop polls 20×/s on a process hosting every live PTY, and in
        # the case that matters most (nothing coming back at all) there is
        # nothing new to normalize on any of those passes.
        scanned_at = mark
        while True:
            if self._exited:
                exited = True
                break
            if not ingested and self._output_total > scanned_at:
                scanned_at = self._output_total
                ingested = self._echo_seen_since(mark, needles)
            now = time.time()
            settled = (
                now >= floor_at
                and self._last_output_at > sent_at
                and (now - self._last_output_at) >= quiet_s
            )
            if ingested and (settled or now >= settle_deadline):
                break
            # Not ingested yet: keep looking past the settle cap — under load
            # the echo itself is deferred (terminal-compose.js's #499 note) —
            # but never past the ingest cap.
            if now >= ingest_deadline:
                break
            time.sleep(_BULK_POLL_S)
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
            # and repainting. Before #763 the CR went out here anyway, into a
            # terminal mid-repaint — exactly the window Claude Code absorbs a
            # CR as a literal newline in, which is how three steers ended up
            # stranded on 2026-08-14. Now nothing is written: the submit is
            # handed to a watcher that presses Enter when the agent actually
            # settles, the way a human at the keyboard would.
            self._defer_args = (self._defer_seq, mark, needles)
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

    # ------------------------------------------------- deferred submit (#763)
    def _arm_deferred_submit(
        self, seq: int, mark: int, needles: List[str], nbytes: int
    ) -> None:
        """Hand a still-unsubmitted payload to a background watcher thread.

        Daemon so a session-host shutdown is never held up by a watcher
        sitting out its window.
        """
        threading.Thread(
            target=self._run_deferred_submit,
            args=(seq, mark, needles, nbytes),
            name=f"defer-submit-{self.session_id[:8]}",
            daemon=True,
        ).start()

    def _run_deferred_submit(
        self, seq: int, mark: int, needles: List[str], nbytes: int
    ) -> None:
        """Thread body: run the watcher, then record whatever it concluded."""
        try:
            outcome = self._await_deferred_submit(seq, mark, needles)
        except Exception as exc:  # noqa: BLE001 — a watcher must never crash the host
            logger.debug(
                f"PTY {self.session_id[:8]} deferred submit watcher failed: {exc}"
            )
            return
        if outcome is None:
            return  # superseded — ``last_input`` belongs to the newer write
        self._record_input(outcome, nbytes, True)

    def _await_deferred_submit(
        self, seq: int, mark: int, needles: List[str]
    ) -> Optional["InputOutcome"]:
        """Wait for a genuine quiet window, then press Enter — or don't.

        This is the API's version of what a human does with a stranded
        ``[Pasted text #N]`` chip: wait until the agent stops working, check
        the message is still sitting there unsent, and press Enter once. It
        never resends the text (see ``_DEFER_QUIET_MS``'s note).

        Three things have to hold before the CR is written, and any of them
        failing means *nothing* is written — the steer stays stranded and
        honestly reported, which is exactly the pre-#763 state, never worse:

        1. **A real quiet window** (``_DEFER_QUIET_MS``), inside
           ``_DEFER_CAP_MS``. A working agent repaints far more often than
           that, so quiet means its turn is genuinely over.
        2. **The payload is still visible in the terminal's recent output.**
           The composer is repainted on essentially every frame, so a payload
           still sitting unsent keeps reappearing; one that was submitted, or
           scrolled behind something else, stops being repainted. Scanning
           only from the oldest sample in the rolling ``frames`` window
           (~``_DEFER_FRAME_LOOKBACK_MS``) is what makes this "it is there
           now" rather than "it was there once".
        3. **No dialog in that same window** (``_DEFER_DIALOG_MARKERS``) —
           a bare CR into a permission or AskUserQuestion modal picks an
           option instead of submitting.

        Returns ``None`` when a newer write superseded this watcher; the
        caller then leaves ``last_input`` alone, since it now describes that
        newer write.
        """
        started_at = time.time()
        deadline = started_at + _DEFER_CAP_MS / 1000
        quiet_s = _DEFER_QUIET_MS / 1000
        # Seeded with the pre-write mark so an agent that settles almost
        # immediately still matches against its own original echo; the seed
        # ages out of the deque within _DEFER_FRAME_LOOKBACK_MS, after which
        # only fresh repaints count.
        frames: "deque[int]" = deque([mark], maxlen=_DEFER_FRAME_SAMPLES)
        while True:
            if self._defer_seq != seq:
                return None
            now = time.time()
            if self._exited:
                return InputOutcome(
                    reason=INPUT_DROPPED,
                    ingested=True,
                    deferred=True,
                    waited_ms=int((now - started_at) * 1000),
                )
            last_out = self._last_output_at
            if last_out and (now - last_out) >= quiet_s:
                break
            if now >= deadline:
                return InputOutcome(
                    reason=INPUT_DEFER_TIMEOUT,
                    ingested=True,
                    deferred=True,
                    submit_confirmed=False,
                    waited_ms=int((now - started_at) * 1000),
                )
            frames.append(self._output_total)
            time.sleep(_DEFER_POLL_S)

        # Quiet. Re-check under ``_write_lock`` so nothing can write between
        # the check and the CR, and re-check ``_defer_seq`` inside it too —
        # a superseding write bumps the counter while holding the same lock.
        with self._write_lock:
            if self._defer_seq != seq:
                return None
            waited_ms = int((time.time() - started_at) * 1000)
            if self._exited:
                return InputOutcome(
                    reason=INPUT_DROPPED,
                    ingested=True,
                    deferred=True,
                    waited_ms=waited_ms,
                )
            normalized = self._normalized_since(frames[0])
            if not self._payload_visible(normalized, needles):
                return InputOutcome(
                    reason=INPUT_DEFER_VANISHED,
                    ingested=True,
                    deferred=True,
                    submit_confirmed=False,
                    waited_ms=waited_ms,
                )
            if any(marker in normalized for marker in _DEFER_DIALOG_MARKERS):
                return InputOutcome(
                    reason=INPUT_DEFER_UNCLEAR,
                    ingested=True,
                    deferred=True,
                    submit_confirmed=False,
                    waited_ms=waited_ms,
                )
            if not self._write_locked("\r"):
                return InputOutcome(
                    reason=INPUT_DROPPED,
                    ingested=True,
                    deferred=True,
                    waited_ms=waited_ms,
                )
        return InputOutcome(
            reason=INPUT_OK,
            ingested=True,
            submitted=True,
            submit_confirmed=True,
            deferred=True,
            waited_ms=waited_ms,
        )
