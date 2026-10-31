"""Does an agent's composer hold typed-but-unsent text? (issue #1643)

An automated typer — the fleet chief's doorbell (#1645), a steer through
``/input`` — must not append its line to a half-typed human message and
submit both. The session-host cannot read the composer itself, so
:class:`ComposerTracker` infers it from the bytes written *into* the PTY:
every keyboard write from the terminal WebSocket and every server-initiated
write passes through ``PtySession._write_locked``, which feeds them here.

The state is three-valued and never optimistic. ``True``: text was typed and
nothing has removed it. ``False``: the composer is known to be empty. ``None``:
something the tracker does not model happened (an arrow key, Esc, Tab, a
control key other than the few below) — reported as unknown, never as
``False``. Only a key whose effect was observed on the agent's own screen may
move the state to ``False``.

Observed through a throwaway ConPTY probe on 2026-10-17 (Claude Code, Codex
CLI and Pi, each spawned alone, nothing attached to a live session):

- Typed text lands in the composer; Backspace (``\\x7f``) back to nothing
  empties it, and so does Ctrl-U (``\\x15``) on a one-line draft and Ctrl-C
  (``\\x03``) on any draft. One bare Esc leaves the draft in place.
- Enter on an empty composer does nothing. Enter on a draft submits it, and
  mid-turn it still empties the composer: Claude queues it (#1548), Codex
  sends it as a steer, Pi shows it as "Steering:".
- A backslash right before Enter inserts a newline instead of submitting in
  Claude Code and Pi (the backslash is consumed); Codex submits the line,
  backslash included. That is ``Agent.backslash_newline``.

Two keys are deliberately not read as a clear although they can empty the
composer: Ctrl-C with no draft (it interrupts a turn, which can put the
prompt back) and the submit after a Ctrl-S stash (Claude Code restores the
stashed draft then). Both report ``None``.

Agents whose composer was not probed get no tracker at all
(:func:`composer_tracker_for` returns ``None``) and report ``None``.

One input the tracker cannot see: the agent opening a dialog of its own over
a draft (a permission prompt, Codex's update notice). Keys typed then reach
the dialog, not the composer. Typing reads as ``True`` (safe); a dialog
answered with Enter or Ctrl-C reads as a clear even though the draft may
come back when the dialog closes. A typer must still check the agent is idle
at its prompt before it types.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Optional, Tuple

from src.agents import SESSION_HOST_AGENTS
from src.session_host_input import TERMINAL_REPLY_RE

logger = logging.getLogger(__name__)

_PASTE_START = "\x1b[200~"
_PASTE_END = "\x1b[201~"
# One escape sequence the browser terminal sends for a key: CSI (arrows,
# Home/End, Shift-Tab, modified keys), SS3 (application-mode arrows), or ESC
# plus one character (an Alt-key). A bare trailing ESC matches the last arm
# with nothing after it.
_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|O.|.)?", re.DOTALL)
# A run of printable characters, taken in one step.
_PRINTABLE_RE = re.compile(r"[^\x00-\x1f\x7f]+")

# How much of the draft to remember. Past it the head is dropped and becomes
# unseen text, exactly like text typed before the tracker lost track.
_TYPED_MAX_CHARS = 4096

_ENTER = "\r"
_BACKSPACE = ("\x7f", "\x08")
_CTRL_C = "\x03"
_CTRL_S = "\x13"
_CTRL_U = "\x15"


class ComposerTracker:
    """Composer state inferred from PTY input bytes for one session.

    ``_base`` is what the composer held before ``_typed``: ``False`` (known
    empty) or ``None`` (unknown). ``_typed`` is the text inserted since then,
    in order, so a Backspace can take back exactly what was typed and Enter
    can see the character before the cursor. The published pair is one tuple
    so a reader on another thread never sees a state with the wrong
    timestamp.
    """

    def __init__(self, backslash_newline: bool) -> None:
        self._backslash_newline = backslash_newline
        self._lock = threading.Lock()
        self._base: Optional[bool] = False
        self._typed = ""
        # Ctrl-S stashes a draft in Claude Code, which puts it back in the
        # composer after the next submit — so that submit is not a clear.
        self._stashed = False
        self._published: Tuple[Optional[bool], Optional[float]] = (False, None)

    def state(self) -> Tuple[Optional[bool], Optional[float]]:
        """``(composer_dirty, composer_dirty_since)``.

        ``composer_dirty_since`` is the epoch second the composer last left
        the known-empty state — for ``True`` and ``None`` alike — and
        ``None`` while it is known empty.
        """
        return self._published

    def feed(self, data: str) -> None:
        """Account for one write that reached the PTY.

        Runs on the keyboard path, so a fault here must never reach the
        write: it is logged and the state becomes unknown.
        """
        try:
            self._feed(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"⚠️ composer tracking failed, state unknown: {exc!r}")
            self.mark_unknown()

    def _feed(self, data: str) -> None:
        data = TERMINAL_REPLY_RE.sub("", data)
        with self._lock:
            i = 0
            while i < len(data):
                if data.startswith(_PASTE_START, i):
                    end = data.find(_PASTE_END, i + len(_PASTE_START))
                    stop = len(data) if end == -1 else end
                    self._insert(data[i + len(_PASTE_START):stop])
                    i = len(data) if end == -1 else end + len(_PASTE_END)
                    continue
                char = data[i]
                if char == "\x1b":
                    self._unknown()
                    i = _ESCAPE_RE.match(data, i).end()
                    continue
                printable = _PRINTABLE_RE.match(data, i)
                if printable:
                    self._insert(printable.group())
                    i = printable.end()
                    continue
                if char == _ENTER:
                    self._enter()
                elif char in _BACKSPACE:
                    self._backspace()
                elif char == _CTRL_C:
                    self._ctrl_c()
                elif char == _CTRL_S:
                    self._stashed = True
                    self._unknown()
                elif char == _CTRL_U:
                    self._kill_line()
                else:
                    self._unknown()
                i += 1
            self._publish()

    def mark_unknown(self) -> None:
        """A write whose bytes may or may not have reached the PTY."""
        with self._lock:
            self._unknown()
            self._publish()

    # ------------------------------------------------------------ transitions
    def _insert(self, text: str) -> None:
        self._typed += text
        if len(self._typed) > _TYPED_MAX_CHARS:
            self._base = None
            self._typed = self._typed[-_TYPED_MAX_CHARS:]

    def _clear(self) -> None:
        self._base = False
        self._typed = ""

    def _ctrl_c(self) -> None:
        # On a draft it was seen to clear. With no draft it interrupts a turn
        # or arms the agent's exit instead, and an interrupted turn may put
        # its prompt back in the composer.
        if self._typed:
            self._clear()
        else:
            self._unknown()

    def _unknown(self) -> None:
        self._base = None
        self._typed = ""

    def _backspace(self) -> None:
        # Past the start of what was typed, the deleted character is one the
        # tracker never saw: the base stays as it was (empty stays empty).
        self._typed = self._typed[:-1]

    def _kill_line(self) -> None:
        # Ctrl-U deletes back to the start of the cursor's line only.
        newline = self._typed.rfind("\n")
        if newline != -1:
            self._typed = self._typed[: newline + 1]
        elif self._base is False:
            self._typed = ""
        else:
            self._unknown()

    def _enter(self) -> None:
        typed = self._typed
        if not typed:
            # Enter on a known-empty composer does nothing; on an unknown one,
            # the character before the cursor is unknown too.
            return
        if self._backslash_newline and typed.endswith("\\"):
            self._typed = typed[:-1] + "\n"
            return
        if not typed.strip():
            self._unknown()
            return
        token = re.split(r"\s", typed)[-1]
        if token.startswith("@") or (typed.startswith("/") and not re.search(r"\s", typed)):
            # A completion popup (a file mention, a slash command) may take
            # the Enter instead of the composer.
            self._unknown()
            return
        if self._base is None and not re.search(r"\s", typed):
            # The current word may have started in the unseen text before
            # ``_typed``, so whether a popup is open is unknown.
            self._unknown()
            return
        if self._stashed:
            self._stashed = False
            self._unknown()
            return
        self._clear()

    def _publish(self) -> None:
        dirty: Optional[bool] = True if self._typed else self._base
        if dirty is False:
            self._published = (False, None)
        elif self._published[0] is False:
            self._published = (dirty, time.time())
        else:
            self._published = (dirty, self._published[1])


def composer_tracker_for(agent_id: str) -> Optional[ComposerTracker]:
    """A tracker for ``agent_id``, or ``None`` when its composer was never
    probed (``Agent.composer_tracked``) — that session reports unknown."""
    agent = SESSION_HOST_AGENTS.get(agent_id)
    if agent is None or not agent.composer_tracked:
        return None
    return ComposerTracker(backslash_newline=agent.backslash_newline)
