"""The deferred-submit watcher: pressing Enter for a payload that is still
waiting in a busy agent's composer.

:class:`DeferredSubmit` extends :class:`~src.session_host_input.InputProtocol`
with the background half of ``submit_input``: when a bulk payload is ingested
but the agent never goes quiet, the submit is handed to a watcher thread that
presses Enter once the agent can take it, or reports why it did not.
``PtySession`` inherits this class, so it gets both halves.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import List, Optional

from src.agents import supports_type_ahead
from src.session_host_input import (
    BULK_POLL_S,
    INPUT_DEFER_FAILED,
    INPUT_DEFER_TIMEOUT,
    INPUT_DEFER_UNCLEAR,
    INPUT_DEFER_VANISHED,
    INPUT_DEFERRED,
    INPUT_DROPPED,
    INPUT_OK,
    INPUT_UNVERIFIED,
    InputOutcome,
    InputProtocol,
    attach_converting,
    chip_visible,
)

logger = logging.getLogger(__name__)

# Deferred submit ("if the
# terminal can take input, so can the API"). A *working* agent repaints its
# spinner every few tens of ms, so InputProtocol's settle wait can never see a quiet
# window for as long as it works: it runs out to _BULK_CAP_MS with the payload
# ingested but still sitting unsent in the composer. Firing the CR there,
# mid-repaint, is the stall shape to avoid: Claude Code absorbs a CR as a
# literal newline when it lands there.
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
# the earlier behaviour.
#
# Sized against the thing actually being waited on rather than against
# a guess at "a while". A lane running scripts/verify-before-ship.ps1 sits
# inside *one* tool call for the gate's whole ~19 min (CLAUDE.md's re-baselined
# full-tier runtime), repainting its spinner throughout, so a timeout shorter
# than that can never reach the end of the turn: a steer sent to a lane
# mid-gate would strand with `reason=defer_timeout`, recoverable only by a
# human POSTing a bare CR. 30 min covers a gate run plus the agent's own
# wrap-up before it goes quiet.
#
# Lengthening the window is only safe because the pre-fire re-check identifies
# *this* payload rather than merely some payload — see chip_visible and
# DeferredSubmit._payload_still_visible. Every other guard the watcher applies
# (supersession, the quiet window, the dialog scan) is evaluated at fire time,
# so none of them cares how long the wait was. The one hazard that genuinely
# scaled with the window was _PASTE_CHIP_MARKER matching on the marker alone,
# which let a chip somebody else put in the composer stand in for ours — and a
# human typing into the session deliberately does not supersede the watcher
# (see PtySession._defer_seq). It no longer does.
#
# What remains is a liveness bound, not a safety one: a watcher that never
# gives up is worse than one that does, so the give-up path stays reachable
# and keeps reporting INPUT_DEFER_TIMEOUT honestly.
_DEFER_CAP_MS = 1_800_000
_DEFER_POLL_S = 0.25
# How far back the pre-fire re-check looks for the payload. Sampling
# _output_total on every poll and scanning from the oldest retained sample is
# what turns "the payload was there once" into "it is there now".
#
# It is only positive evidence, never negative. The premise that the
# composer is repainted on essentially every frame is false for Claude Code's
# differential renderer, which repaints only the cells that changed: an
# unchanged composer under a ticking status row is never painted again, so a
# steer still sitting on the input line read as gone, and the watcher reported
# ``defer_vanished`` after ~113 s and pressed nothing. What proves the payload
# is still there is that no keystroke has touched the composer since the paste
# (``PtySession._composer_touches``); this scan only matters once one has.
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

# How long to wait after a background-wait submit for the main turn to start,
# which is what confirms the CR was taken as Submit. Sized from a live
# session in this state: Claude Code went silent for ~5 s after taking the
# paste and again after the CR, and painted the turn's first spinner frame
# 4.7 s after it. The wait runs on the watcher thread, never on a caller.
_BG_SUBMIT_CONFIRM_MS = 15000

# Type-ahead. The deferral assumed a CR sent while the agent works is
# absorbed as a literal newline, and so waited for the turn to go quiet. That
# was never probed, and for Claude Code it is not what happens: a probe on
# v2.1.296 (2026-10-12) pasted a 1,034-byte, six-paragraph steer into a
# throwaway session mid-turn, sent the CR 3 s later — the moment submit_input
# hands the submit to its watcher — and Claude Code moved the message out of
# the composer at once, painted "Press up to edit queued messages" in its
# place and "ctrl+x ctrl+s to send now" under the queued text, and answered it
# when the turn yielded. Waiting for quiet instead strands a steer for as long
# as the turn runs (a gate run, background agents, a status row that keeps
# ticking), and the owner pressed Enter by hand on two such steers.
#
# So for an agent whose ``type_ahead`` was probed (``src.agents``), a running
# main turn is the cue to press Enter now rather than the thing to wait out.
# "Running" means a main-turn spinner glyph (``_MAIN_SPINNER_RE``) painted
# within _TYPEAHEAD_LIVE_MS: the spinner repaints several times a second for
# as long as a turn runs, and stops when a dialog takes over the screen.
_TYPEAHEAD_LIVE_MS = 1000
# The queued-state markers, normalized (see _normalize_echo). Either one is
# the confirmation; Claude Code paints both. No marker within
# _TYPEAHEAD_CONFIRM_MS means the CR went in and nothing showed it was taken,
# reported as unconfirmed, never as ``ok``. The probe saw them within 0.5 s.
_QUEUED_MARKERS = ("queuedmessages", "tosendnow")
_TYPEAHEAD_CONFIRM_MS = 10000

# What released the deferred watcher's Enter: a quiet window, the
# idle-with-background-agents screen, or a running turn on a
# type-ahead agent. The last two confirm their CR afterwards.
_DEFER_MODE_QUIET = "quiet"
_DEFER_MODE_BG = "bg_waiting"
_DEFER_MODE_TYPEAHEAD = "type_ahead"


class DeferredSubmit(InputProtocol):
    """``InputProtocol`` plus the deferred-submit watcher and the verdict
    recorder both halves share. Like its base, it supplies behaviour only;
    the state lives on the ``PtySession`` dataclass."""

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
        # Breadcrumbs for the next occurrence: the whole defect was
        # invisible in the logs, so both an outright non-delivery and an
        # unconfirmed submit — the shape all three 2026-08-14 stalls took —
        # get an INFO line naming which condition it was.
        sid = self.session_id[:8]
        if outcome.reason == INPUT_DEFERRED:
            logger.info(
                f"⏳ PTY {sid} input ingested but the agent is still busy — "
                f"submit deferred to a watcher ({nbytes} chars, waited "
                f"{outcome.waited_ms}ms, window {_DEFER_CAP_MS}ms)"
            )
        elif outcome.deferred and outcome.reason == INPUT_OK:
            how = "queued by the agent mid-turn" if outcome.queued else "landed"
            logger.info(
                f"✅ PTY {sid} deferred submit {how} after "
                f"{outcome.waited_ms}ms ({nbytes} chars)"
            )
        elif outcome.deferred and outcome.submitted:
            # The watcher's Enter went in, but nothing confirmed it was taken.
            # Delivered-but-unconfirmed fell through every
            # branch here before, so this verdict left no line at all.
            logger.info(
                f"❔ PTY {sid} deferred submit sent, not confirmed "
                f"({outcome.reason}, {nbytes} chars, waited "
                f"{outcome.waited_ms}ms)"
            )
        elif not outcome.delivered:
            # Names the submit's fate too: a paste stranded unsent in
            # the composer (ingested, not_submitted) reads differently from
            # one that never reached the terminal at all.
            logger.info(
                f"⚠️ PTY {sid} input not delivered "
                f"({outcome.reason}, ingested={outcome.ingested}, "
                f"submit={outcome.submit_state}, {nbytes} chars, "
                f"waited {outcome.waited_ms}ms)"
            )

    @staticmethod
    def _payload_still_visible(
        normalized: str, needles: List[str], chip_id: Optional[str]
    ) -> bool:
        """Is *our* payload the thing the composer is showing right now?

        The deferred watcher's pre-fire check (#1075), and deliberately
        stricter than :meth:`_payload_visible`. At ingest time any chip is
        fair evidence: the window opens at our own write, it closes within
        ``_INGEST_CAP_MS``, and the question is only "did anything land".
        The watcher asks a different question minutes later — "is the thing
        in the composer still mine" — and over a window long enough to
        outlast a gate run somebody else's paste can easily be the chip
        sitting there. A human typing into the session does not supersede the
        watcher (that is decided — see ``PtySession._defer_seq``), so this
        identity check is the only thing between a long wait and a CR pressed
        on their half-composed message.

        ``chip_id`` is ``None`` when no chip existed at ingest, in which case
        the payload's own echo is the only evidence that counts. An unpinned
        chip is not evidence, and the watcher declining costs nothing worse
        than a steer that stays stranded and honestly reported.
        """
        if chip_id is not None and chip_visible(normalized, chip_id):
            return True
        return any(needle in normalized for needle in needles)

    def _arm_deferred_submit(
        self,
        seq: int,
        mark: int,
        needles: List[str],
        chip_id: Optional[str],
        touches: Optional[int] = None,
        *,
        nbytes: int,
    ) -> None:
        """Hand a still-unsubmitted payload to a background watcher thread.

        Daemon so a session-host shutdown is never held up by a watcher
        sitting out its window.
        """
        threading.Thread(
            target=self._run_deferred_submit,
            args=(seq, mark, needles, chip_id, touches),
            kwargs={"nbytes": nbytes},
            name=f"defer-submit-{self.session_id[:8]}",
            daemon=True,
        ).start()

    def _run_deferred_submit(
        self,
        seq: int,
        mark: int,
        needles: List[str],
        chip_id: Optional[str],
        touches: Optional[int] = None,
        *,
        nbytes: int,
    ) -> None:
        """Thread body: run the watcher, then record whatever it concluded.

        Every path ends on a recorded verdict or a log line saying why there
        is none (#1548): ``last_input`` left at ``pending`` by a watcher that
        died is indistinguishable from one still waiting.
        """
        sid = self.session_id[:8]
        started_at = time.time()
        try:
            outcome = self._await_deferred_submit(seq, mark, needles, chip_id, touches)
        except Exception as exc:  # noqa: BLE001 — a watcher must never crash the host
            # Only the pre-CR part can get here: the CR write itself never
            # raises (``_write_locked`` returns False instead), and the
            # confirmation that follows it catches its own failures. So no
            # CR was written, and the verdict says so.
            logger.info(f"❌ PTY {sid} deferred submit watcher failed: {exc!r}")
            if self._defer_seq != seq:
                return
            outcome = InputOutcome(
                reason=INPUT_DEFER_FAILED,
                ingested=True,
                deferred=True,
                submit_confirmed=False,
                waited_ms=int((time.time() - started_at) * 1000),
            )
        if outcome is None:
            logger.info(
                f"↪️ PTY {sid} deferred submit superseded by a newer write or a "
                f"stop — the verdict on record is that write's"
            )
            return
        self._record_input(outcome, nbytes, True)

    def _await_deferred_submit(
        self,
        seq: int,
        mark: int,
        needles: List[str],
        chip_id: Optional[str] = None,
        touches: Optional[int] = None,
    ) -> Optional["InputOutcome"]:
        """Press Enter once the agent can take it — or don't.

        This is the API's version of what a human does with a stranded
        ``[Pasted text #N]`` chip: check the message is still sitting there
        unsent, and press Enter once. It never resends the text (see
        ``_DEFER_QUIET_MS``'s note).

        When to press, the first of:

        - **The agent's turn is running and it takes type-ahead** (#1548): a
          main-turn spinner painted within ``_TYPEAHEAD_LIVE_MS`` on an agent
          whose ``type_ahead`` was probed. Enter goes in now and the agent
          queues the message; the CR is confirmed afterwards by the queued
          markers (:meth:`_confirm_typeahead_submit`).
        - **A real quiet window** (``_DEFER_QUIET_MS``), inside
          ``_DEFER_CAP_MS``. A working agent repaints far more often than
          that, so quiet means its turn is genuinely over.
        - **Idle at its prompt with background agents running** (#1319,
          ``_bg_waiting``), a state whose agent rows repaint every second
          forever. Its CR is confirmed afterwards too: the main turn has to
          start (:meth:`_confirm_bg_submit`).

        An image still being converted holds all three. Then two things have
        to hold before the CR is written, and either failing means *nothing*
        is written — the steer stays stranded and honestly reported:

        1. **The payload — *ours* — is still in the composer.** Proven by no
           keystroke having touched the composer since the paste (``touches``
           against ``_composer_touches``, #1548), or, once one has, by the
           payload showing in the terminal's recent output: scanning only
           from the oldest sample in the rolling ``frames`` window
           (~``_DEFER_FRAME_LOOKBACK_MS``) makes that "it is there now", and
           ``chip_id`` (#1075) makes it "it is *mine*" — see
           :meth:`_payload_still_visible`. ``touches`` is ``None`` when the
           count was not taken; only the screen counts then.
        2. **No dialog in that same window** (``_DEFER_DIALOG_MARKERS``) —
           a bare CR into a permission or AskUserQuestion modal picks an
           option instead of submitting.

        Returns ``None`` when a newer write superseded this watcher; the
        caller then leaves ``last_input`` alone, since it now describes that
        newer write.
        """
        started_at = time.time()
        # Seeded with the pre-write mark so an agent that settles almost
        # immediately still matches against its own original echo; the seed
        # ages out of the deque within _DEFER_FRAME_LOOKBACK_MS, after which
        # only fresh repaints count.
        frames: "deque[int]" = deque([mark], maxlen=_DEFER_FRAME_SAMPLES)
        mode = self._await_submit_moment(seq, mark, started_at, frames)
        if not isinstance(mode, str):
            return mode

        # Re-check under ``_write_lock`` so nothing can write between the
        # check and the CR, and re-check ``_defer_seq`` inside it too — a
        # superseding write bumps the counter while holding the same lock.
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
            refusal = self._refuse_before_fire(
                frames[0], needles, chip_id, touches, mode, waited_ms
            )
            if refusal is not None:
                return refusal
            cr_mark = self._output_total
            if not self._write_locked("\r"):
                return InputOutcome(
                    reason=INPUT_DROPPED,
                    ingested=True,
                    deferred=True,
                    waited_ms=waited_ms,
                )
        return self._confirm_deferred_cr(seq, cr_mark, waited_ms, mode)

    def _await_submit_moment(
        self, seq: int, mark: int, started_at: float, frames: "deque[int]"
    ) -> "str | InputOutcome | None":
        """Poll until one of :meth:`_await_deferred_submit`'s three release
        conditions holds and return its ``_DEFER_MODE_*``, sampling
        ``_output_total`` into ``frames`` on every poll. An ``InputOutcome``
        when the watcher ends without pressing (the session exited, or
        ``_DEFER_CAP_MS`` ran out); ``None`` when a newer write superseded it.
        """
        deadline = started_at + _DEFER_CAP_MS / 1000
        quiet_s = _DEFER_QUIET_MS / 1000
        live_s = _TYPEAHEAD_LIVE_MS / 1000
        type_ahead = supports_type_ahead(self.agent)
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
            if (
                type_ahead
                and not self._bg_waiting
                and now - self._main_turn_seen_at <= live_s
                and not attach_converting(self._normalized_since(mark))
            ):
                return _DEFER_MODE_TYPEAHEAD
            last_out = self._last_output_at
            if last_out and (now - last_out) >= quiet_s:
                return _DEFER_MODE_QUIET
            if self._bg_waiting and not attach_converting(self._normalized_since(mark)):
                return _DEFER_MODE_BG
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

    def _refuse_before_fire(
        self,
        since: int,
        needles: List[str],
        chip_id: Optional[str],
        touches: Optional[int],
        mode: str,
        waited_ms: int,
    ) -> Optional["InputOutcome"]:
        """The two pre-fire checks of :meth:`_await_deferred_submit`, run
        under ``_write_lock``: the verdict that withholds the CR, or ``None``
        (after a breadcrumb for a screen-read release) when it may go in."""
        sid = self.session_id[:8]
        normalized = self._normalized_since(since)
        untouched = touches is not None and self._composer_touches == touches
        if not (
            untouched or self._payload_still_visible(normalized, needles, chip_id)
        ):
            logger.info(
                f"🔎 PTY {sid} deferred submit: the composer was touched "
                f"since the paste and the payload is not on screen "
                f"(touches {touches}→{self._composer_touches}, chip "
                f"{chip_id}, mode {mode})"
            )
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
        composer = "untouched" if untouched else "touched, payload on screen"
        if mode == _DEFER_MODE_BG:
            # Breadcrumb for the next occurrence: the state is
            # recognised from screen markers that drift across Claude
            # Code versions, so say when it was the reason for a CR.
            logger.info(
                f"🧵 PTY {sid} idle at its prompt with background agents "
                f"running — submitting without a quiet window (waited "
                f"{waited_ms}ms, composer {composer})"
            )
        elif mode == _DEFER_MODE_TYPEAHEAD:
            logger.info(
                f"⌨️ PTY {sid} agent mid-turn — pressing Enter for it to "
                f"queue (waited {waited_ms}ms, composer {composer})"
            )
        return None

    def _confirm_deferred_cr(
        self, seq: int, cr_mark: int, waited_ms: int, mode: str
    ) -> Optional["InputOutcome"]:
        """The verdict on a CR the watcher wrote: confirmed afterwards for a
        screen-read release, confirmed by the quiet window itself otherwise.
        Runs outside ``_write_lock``."""
        try:
            if mode == _DEFER_MODE_BG:
                return self._confirm_bg_submit(seq, cr_mark, waited_ms)
            if mode == _DEFER_MODE_TYPEAHEAD:
                return self._confirm_typeahead_submit(seq, cr_mark, waited_ms)
        except Exception as exc:  # noqa: BLE001 — the CR is in; say so
            logger.info(
                f"❔ PTY {self.session_id[:8]} deferred submit confirmation "
                f"failed: {exc!r}"
            )
            return InputOutcome(
                reason=INPUT_UNVERIFIED,
                ingested=True,
                submitted=True,
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

    def _confirm_typeahead_submit(
        self, seq: int, cr_mark: int, waited_ms: int
    ) -> Optional["InputOutcome"]:
        """Did the agent queue the message a mid-turn Enter sent? (#1548)

        Confirmed by a queued-state marker (``_QUEUED_MARKERS``) painted after
        the CR, within ``_TYPEAHEAD_CONFIRM_MS``. Without one the outcome is
        unverified with the submit unconfirmed, never ``ok``: a CR the agent
        absorbed as a newline would look exactly like that, and pressing
        again is the operator's call, not the watcher's. Runs outside
        ``_write_lock``. ``None`` when a newer write superseded the watcher.
        """
        confirm_by = time.time() + _TYPEAHEAD_CONFIRM_MS / 1000
        while True:
            if self._defer_seq != seq:
                return None
            normalized = self._normalized_since(cr_mark)
            if any(marker in normalized for marker in _QUEUED_MARKERS):
                return InputOutcome(
                    reason=INPUT_OK,
                    ingested=True,
                    submitted=True,
                    submit_confirmed=True,
                    deferred=True,
                    waited_ms=waited_ms,
                    queued=True,
                )
            if self._exited or time.time() >= confirm_by:
                logger.info(
                    f"⚠️ PTY {self.session_id[:8]} mid-turn Enter sent, but no "
                    f"queued message showed within {_TYPEAHEAD_CONFIRM_MS}ms"
                )
                return InputOutcome(
                    reason=INPUT_UNVERIFIED,
                    ingested=True,
                    submitted=True,
                    deferred=True,
                    waited_ms=waited_ms,
                )
            time.sleep(BULK_POLL_S)

    def _confirm_bg_submit(
        self, seq: int, cr_mark: int, waited_ms: int
    ) -> Optional["InputOutcome"]:
        """Did the CR a background-wait submit wrote start a main turn? (#1319)

        The quiet-window path earns its ``submit_confirmed=True`` from the
        agent having gone silent. This path fires into a screen that is still
        painting, on the strength of reading that screen, so it checks the
        result instead: Claude Code paints a main-turn spinner as soon as a
        submit starts a turn. No spinner within ``_BG_SUBMIT_CONFIRM_MS``
        means the CR went in and nothing showed it was taken, which is
        reported as exactly that (unverified, submit unconfirmed) rather
        than ``ok``. Runs outside ``_write_lock``, so a keyboard writer is
        never held up by the wait. ``waited_ms`` stays the wait before the CR,
        as on the quiet-window path.

        ``None`` when a newer write superseded the watcher during the wait:
        ``last_input`` describes that write by then and is left alone, as
        everywhere else in the watcher.
        """
        confirm_by = time.time() + _BG_SUBMIT_CONFIRM_MS / 1000
        while self._main_turn_at <= cr_mark:
            if self._defer_seq != seq:
                return None
            if self._exited or time.time() >= confirm_by:
                logger.info(
                    f"⚠️ PTY {self.session_id[:8]} background-wait submit sent, "
                    f"but no main turn started within {_BG_SUBMIT_CONFIRM_MS}ms"
                )
                return InputOutcome(
                    reason=INPUT_UNVERIFIED,
                    ingested=True,
                    submitted=True,
                    deferred=True,
                    waited_ms=waited_ms,
                )
            time.sleep(BULK_POLL_S)
        return InputOutcome(
            reason=INPUT_OK,
            ingested=True,
            submitted=True,
            submit_confirmed=True,
            deferred=True,
            waited_ms=waited_ms,
        )
