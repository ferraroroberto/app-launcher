"""The PTY side of a session's pending messages: recording what each
``/input`` write left pending, reading the list against the screen, and
discarding a message the agent has not taken yet.

:class:`PendingInputProtocol` extends
:class:`~src.session_hosting.deferred_submit.DeferredSubmit`, and
``PtySession`` inherits it. Like its bases it supplies behaviour only; the list
itself (:class:`~src.session_hosting.pending_input.PendingInputs`) and every
other piece of state live on the ``PtySession`` dataclass.

A discard never presses Enter, and it presses nothing at all unless the
screen says the keys can only reach the composer:

- **Queued in the agent:** only while the agent's turn is visibly running
  (a main-turn spinner in the last second, which a dialog stops), with the
  composer known empty and no dialog in recent output. Up then pulls every
  queued message back into the composer and Ctrl-U clears it. That takes
  the whole queue, so every queued entry goes.
- **Still in the composer:** the deferred watcher is told to clear rather
  than submit, so the Ctrl-U presses go in at the moment its own checks
  would have released Enter, after those same checks pass.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from src.agents import supports_queue_edit, supports_type_ahead
from src.session_host_input import (
    INPUT_DEFERRED,
    INPUT_DISCARD_PENDING,
    INPUT_DISCARDED,
    INPUT_DROPPED,
    InputOutcome,
)
from src.session_hosting import deferred_submit
from src.session_hosting.deferred_submit import DeferredSubmit
from src.session_hosting.pending_input import (
    PENDING_DISCARDING,
    PENDING_IN_COMPOSER,
    PENDING_QUEUED,
    PendingEntry,
)

logger = logging.getLogger(__name__)

# Up moves every queued message back into the composer and Ctrl-U clears
# it; neither submits, and with nothing queued both do nothing (a throwaway
# Claude Code v2.1.316 probe, 2026-11-01). The gaps are the shortest that
# probe cleared two queued messages with: Up needs its repaint before the
# first Ctrl-U.
_QUEUE_EDIT_KEY = "\x1b[A"
_CLEAR_KEY = "\x15"
_QUEUE_EDIT_SETTLE_S = 0.3
_CLEAR_KEY_GAP_S = 0.08
# The presses run under ``_write_lock``, which every keyboard writer waits
# on, so they are capped at about five seconds' worth. A composer needing
# more is left reported unknown rather than empty.
_CLEAR_MAX_PRESSES = 60
# How much recent output to look through for a dialog: a permission prompt
# paints once, then sits quiet over the queue.
_DIALOG_LOOKBACK_CHARS = 4000

# Discard verdicts. Distinct refusals for distinct conditions, so the Chat
# view can say which one stopped it.
DISCARD_DONE = "discarded"
DISCARD_CLEARING = "discarding"
DISCARD_NOT_PENDING = "not_pending"
DISCARD_NOT_REMOVABLE = "not_removable"
DISCARD_NOT_CONFIRMED = "not_confirmed"
DISCARD_NOT_MID_TURN = "not_mid_turn"
DISCARD_COMPOSER_TEXT = "composer_has_text"
DISCARD_DIALOG = "dialog_open"
DISCARD_EXITED = "exited"
DISCARD_DROPPED = "dropped"

_DISCARD_DETAIL = {
    DISCARD_DONE: "Removed from the agent's queue",
    DISCARD_CLEARING: "Clearing it from the agent's input box",
    DISCARD_NOT_PENDING: "Already taken by the agent",
    DISCARD_NOT_REMOVABLE: "Queued in the agent, can't be removed from here",
    DISCARD_NOT_CONFIRMED: "Not confirmed queued yet, try again in a moment",
    DISCARD_NOT_MID_TURN: "The agent is between turns and taking it now",
    DISCARD_COMPOSER_TEXT: "The agent's input box has other text in it, remove it in Terminal",
    DISCARD_DIALOG: "The agent is showing a question, answer it first",
    DISCARD_EXITED: "The session has ended",
    DISCARD_DROPPED: "The keys did not all reach the terminal, check it in Terminal",
}
_DISCARD_STATUS = {
    DISCARD_DONE: 200,
    DISCARD_CLEARING: 202,
    DISCARD_NOT_PENDING: 404,
    DISCARD_DROPPED: 502,
}


def _discard_reply(reason: str, ids: Optional[List[str]] = None) -> Tuple[int, Dict[str, Any]]:
    return _DISCARD_STATUS.get(reason, 409), {
        "reason": reason,
        "detail": _DISCARD_DETAIL[reason],
        "ids": ids or [],
    }


class PendingInputProtocol(DeferredSubmit):
    """Pending-message tracking and discard for ``PtySession``. The screen
    evidence is the deferred watcher's own: its spinner recency, quiet
    window, dialog markers and queued markers (``deferred_submit``)."""

    # ------------------------------------------------------------ screen facts
    def _turn_running_now(self) -> bool:
        """Is a type-ahead agent's main turn running right now, so an Enter
        queues and the queue-edit keys reach the composer? A main-turn
        spinner repaints several times a second, and stops when a dialog
        takes the screen."""
        live_s = deferred_submit._TYPEAHEAD_LIVE_MS / 1000
        return (
            supports_type_ahead(self.agent)
            and not self._bg_waiting
            and time.time() - self._main_turn_seen_at <= live_s
        )

    def _dialog_recent(self) -> bool:
        recent = self._normalized_since(self._output_total - _DIALOG_LOOKBACK_CHARS)
        return any(marker in recent for marker in deferred_submit._DEFER_DIALOG_MARKERS)

    def _idle_at_prompt(self, now: float) -> bool:
        """Nothing is queued in an agent idle at its prompt: it submits its
        queue the moment the turn yields. Idle is the watcher's evidence:
        idle with background agents running, or its quiet window with no
        dialog sitting over the queue."""
        if self._bg_waiting:
            return True
        last = self._last_output_at
        quiet_s = deferred_submit._DEFER_QUIET_MS / 1000
        return bool(last) and now - last >= quiet_s and not self._dialog_recent()

    def _queued_markers_since(self, mark: int) -> bool:
        painted = self._normalized_since(mark)
        return any(marker in painted for marker in deferred_submit._QUEUED_MARKERS)

    def _composer_known_empty(self) -> bool:
        return self._composer is not None and self._composer.state()[0] is False

    def _discard_pending(self) -> bool:
        return self._pending.has_state(PENDING_DISCARDING)

    # ---------------------------------------------------------------- tracking
    def _track_pending(
        self, data: str, outcome: InputOutcome, mark: int, clean_before: bool
    ) -> None:
        """Record what one ``submit_input`` write left pending. Runs under
        ``_write_lock``, right after the write.

        A deferred payload joins the composer's entries. A write whose CR
        went in takes the composer with it, queued if the agent's turn was
        running and its own text queued with them. A write that sent no CR
        superseded the watcher, so nothing in the composer will be submitted
        by the host any more.
        """
        if outcome.reason == INPUT_DISCARD_PENDING:
            return
        now = time.time()
        if outcome.reason == INPUT_DEFERRED:
            self._pending.add(data, now, PENDING_IN_COMPOSER, clean_before=clean_before)
            return
        if not outcome.submitted:
            self._pending.drop_composer()
            return
        if not self._turn_running_now():
            self._pending.submitted(None)
            return
        confirm_by = now + deferred_submit._TYPEAHEAD_CONFIRM_MS / 1000
        self._pending.submitted((mark, confirm_by))
        if data:
            self._pending.add(
                data, now, PENDING_QUEUED, confirm_mark=mark, confirm_by=confirm_by
            )

    def _write_watcher_cr_locked(self, mode: str, cr_mark: int) -> bool:
        """The deferred watcher's Enter, under ``_write_lock``. The composer's
        entries leave it the moment the Enter is written, not when the
        watcher's confirmation ends: a discard in between must find them
        taken, never clear an input box they already left. Mid-turn they are
        queued, awaiting the agent's queued markers, as for an immediate send."""
        if not self._write_locked("\r"):
            return False
        if mode == deferred_submit._DEFER_MODE_TYPEAHEAD:
            confirm_by = time.time() + deferred_submit._TYPEAHEAD_CONFIRM_MS / 1000
            self._pending.submitted((cr_mark, confirm_by))
        else:
            self._pending.submitted(None)
        return True

    def _entry_removable(self, entry: PendingEntry) -> bool:
        if not supports_queue_edit(self.agent) or self._exited:
            return False
        if entry.state == PENDING_IN_COMPOSER:
            return entry.clean_before
        return entry.state == PENDING_QUEUED and entry.confirmed

    def pending_input_api(self) -> List[Dict[str, Any]]:
        """The session's pending messages for ``to_api``, settled against
        the screen first. Every session-list read calls this, so an empty
        list costs no screen scan."""
        if self._exited:
            self._pending.clear()
            return []
        if not self._pending.entries():
            return []
        now = time.time()
        self._pending.prune(now, self._idle_at_prompt(now), self._queued_markers_since)
        return self._pending.to_api(self._entry_removable)

    # ----------------------------------------------------------------- discard
    def discard_pending_input(self, entry_id: str) -> Tuple[int, Dict[str, Any]]:
        """Discard one pending message: ``(http_status, body)``.

        200 when the keys went in and the queue is gone, 202 when the
        watcher will clear the composer, 404 when the agent already took
        it, 409 with the reason when the screen does not allow it now, 502
        when a key write failed.
        """
        entry = self._pending.get(entry_id)
        if entry is None:
            return _discard_reply(DISCARD_NOT_PENDING)
        if not supports_queue_edit(self.agent):
            return _discard_reply(DISCARD_NOT_REMOVABLE)
        if entry.state == PENDING_DISCARDING:
            return _discard_reply(DISCARD_CLEARING, [entry.id])
        if entry.state == PENDING_IN_COMPOSER:
            return self._discard_from_composer()
        return self._discard_from_queue(entry_id)

    def _discard_from_composer(self) -> Tuple[int, Dict[str, Any]]:
        with self._write_lock:
            held = [e for e in self._pending.composer_held() if e.state == PENDING_IN_COMPOSER]
            if not held:
                return _discard_reply(DISCARD_NOT_PENDING)
            if self._exited:
                return _discard_reply(DISCARD_EXITED)
            if not all(e.clean_before for e in held):
                # Ctrl-U would take somebody's half-typed text with it.
                return _discard_reply(DISCARD_COMPOSER_TEXT)
            ids = self._pending.mark_discarding()
        logger.info(
            f"🗑️ PTY {self.session_id[:8]} discard: {len(ids)} message(s) in the "
            f"composer, cleared by the watcher at its next safe moment"
        )
        return _discard_reply(DISCARD_CLEARING, ids)

    def _discard_from_queue(self, entry_id: str) -> Tuple[int, Dict[str, Any]]:
        with self._write_lock:
            entry = self._pending.get(entry_id)
            refusal = self._queue_discard_refusal(entry)
            if refusal is not None:
                logger.info(
                    f"🗑️ PTY {self.session_id[:8]} discard refused: {refusal}"
                )
                return _discard_reply(refusal)
            presses = self._pending.clear_presses((PENDING_QUEUED,))
            ok = self._write_locked(_QUEUE_EDIT_KEY)
            if ok:
                time.sleep(_QUEUE_EDIT_SETTLE_S)
                ok = self._clear_composer_locked(presses)
            ids = self._pending.drop_queued()
        logger.info(
            f"🗑️ PTY {self.session_id[:8]} discard: {len(ids)} queued message(s) "
            f"pulled back with Up and cleared with {presses} Ctrl-U"
            f"{'' if ok else ' (a key write failed)'}"
        )
        return _discard_reply(DISCARD_DONE if ok else DISCARD_DROPPED, ids)

    def _queue_discard_refusal(self, entry: Optional[PendingEntry]) -> Optional[str]:
        """Why the queue-edit keys must not go in now, or ``None``. Under
        ``_write_lock``, so nothing types between this and the keys."""
        if entry is None or entry.state != PENDING_QUEUED:
            return DISCARD_NOT_PENDING
        if self._exited:
            return DISCARD_EXITED
        if not entry.confirmed:
            return DISCARD_NOT_CONFIRMED
        if not self._turn_running_now():
            return DISCARD_NOT_MID_TURN
        if not self._composer_known_empty():
            # Up would recall into, and Ctrl-U delete, somebody's draft.
            return DISCARD_COMPOSER_TEXT
        if self._dialog_recent():
            return DISCARD_DIALOG
        return None

    def _clear_composer_locked(self, presses: int) -> bool:
        """Press Ctrl-U ``presses`` times (at most ``_CLEAR_MAX_PRESSES``)
        under ``_write_lock``. The composer is empty afterwards, which the
        tracker cannot work out from an Up it does not model, so it is told;
        past the cap it is told only that the state is unknown."""
        for i in range(min(presses, _CLEAR_MAX_PRESSES)):
            if i:
                time.sleep(_CLEAR_KEY_GAP_S)
            if not self._write_locked(_CLEAR_KEY):
                return False
        if self._composer is not None:
            if presses > _CLEAR_MAX_PRESSES:
                self._composer.mark_unknown()
            else:
                self._composer.mark_empty()
        return True

    def _clear_discarded_locked(self, waited_ms: int) -> InputOutcome:
        """The deferred watcher's discard: Ctrl-U in place of Enter, at the
        moment and after the checks the Enter would have needed."""
        presses = self._pending.clear_presses((PENDING_DISCARDING,))
        ok = self._clear_composer_locked(presses)
        logger.info(
            f"🗑️ PTY {self.session_id[:8]} discarded payload cleared from the "
            f"composer with {presses} Ctrl-U{'' if ok else ' (a key write failed)'}"
        )
        return InputOutcome(
            reason=INPUT_DISCARDED if ok else INPUT_DROPPED,
            ingested=True,
            deferred=True,
            submit_confirmed=False,
            waited_ms=waited_ms,
        )
