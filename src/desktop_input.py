"""Screen control (#1521): click, drag, scroll and type on the PC from the phone.

The Screen view's socket (:mod:`app.webapp.routers.desktop_screen`) hands
each input message to an :class:`InputSession`, which turns it into Windows
``SendInput`` events. Same process as the capture (the tray-owned webapp in
the user's interactive session), so no admin rights and no extra tool.

* **One mapping.** The phone sends ``x``/``y`` normalised 0-1 within the
  monitor it is showing. :func:`map_point` turns that into a physical pixel
  on that monitor's rect, then into the 0-65535 range ``SendInput`` takes
  for an absolute move over the whole virtual desktop
  (``MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK``). Rects are physical
  pixels because :func:`desktop_capture.ensure_dpi_awareness` made the
  process per-monitor-v2 aware, so a scaled monitor maps like any other.
* **One seam.** Every event goes out through an :class:`Injector`.
  :class:`SendInputInjector` is the only code in the repo that calls
  ``SendInput``; tests, and the pre-ship gate's disposable webapp
  (``LAUNCHER_SCREEN_FAKE_INPUT=1``), use :class:`RecordingInjector`, which
  injects nothing.
* **Nothing stays held.** A modifier combo goes out as one batch (modifiers
  down, key down/up, modifiers up). A drag's button is held between
  messages, so the session tracks what Windows actually accepted as down and
  :meth:`InputSession.release_all` lifts it when the socket closes; a dropped
  phone never leaves Ctrl or a mouse button stuck on the PC.
* **Nothing typed is logged.** The session counts accepted messages for the
  audit line; it never logs their content.
* **The clipboard rides the same socket (#1719).** A ``clipboard_set``
  message puts its text on the PC's clipboard through
  :mod:`src.desktop_clipboard`, its own seam; it has its own size cap and its
  own reply (``{"type": "clipboard", ...}``, which says only whether it
  worked), and the session counts transfers, never what they carried.

A non-elevated process cannot inject into an elevated window (UIPI):
``SendInput`` reports success and nothing happens. The lock screen (the
secure desktop) refuses input the same way. Both are known limits.
"""

from __future__ import annotations

import ctypes
import json
import logging
import math
import os
import sys
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Set, Tuple, Union

from src import desktop_capture, desktop_clipboard
from src.desktop_capture import Monitor

logger = logging.getLogger(__name__)

# Set by the pre-ship gate's disposable webapp: input is recorded, never sent.
FAKE_INPUT_ENV = "LAUNCHER_SCREEN_FAKE_INPUT"

# Bounds on one message: a text frame types at most this many characters, a
# scroll moves at most this many wheel units per axis (120 = one notch).
MAX_TEXT_CHARS = 1000
MAX_SCROLL = 1200
# A frame longer than this is not an input message (and is not parsed), except
# a clipboard_set frame, which has its own, larger cap: the text may be
# up to desktop_clipboard.MAX_CLIPBOARD_BYTES, and JSON escaping can make a
# character up to six characters long.
MAX_MESSAGE_CHARS = 8192
MAX_CLIPBOARD_FRAME_CHARS = 6 * desktop_clipboard.MAX_CLIPBOARD_BYTES + 256
# How a clipboard_set frame starts (the phone's JSON.stringify of
# {type, text}); only a frame that starts this way may be over MAX_MESSAGE_CHARS.
CLIPBOARD_FRAME_PREFIX = '{"type":"clipboard_set"'

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

# button -> (down flag, up flag)
BUTTONS: Dict[str, Tuple[int, int]] = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

VK_BACK, VK_TAB, VK_RETURN = 0x08, 0x09, 0x0D
VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_ESCAPE, VK_SPACE = 0x1B, 0x20
VK_PRIOR, VK_NEXT, VK_END, VK_HOME = 0x21, 0x22, 0x23, 0x24
VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN = 0x25, 0x26, 0x27, 0x28
VK_DELETE, VK_LWIN = 0x2E, 0x5B

# Named key -> (virtual key, extended). The navigation cluster and the Win
# key are extended keys; without the flag an app can read an arrow as the
# numeric keypad's.
NAMED_KEYS: Dict[str, Tuple[int, bool]] = {
    "esc": (VK_ESCAPE, False),
    "tab": (VK_TAB, False),
    "enter": (VK_RETURN, False),
    "backspace": (VK_BACK, False),
    "delete": (VK_DELETE, True),
    "space": (VK_SPACE, False),
    "up": (VK_UP, True),
    "down": (VK_DOWN, True),
    "left": (VK_LEFT, True),
    "right": (VK_RIGHT, True),
    "home": (VK_HOME, True),
    "end": (VK_END, True),
    "pageup": (VK_PRIOR, True),
    "pagedown": (VK_NEXT, True),
    # F1-F12 are the contiguous virtual keys 0x70-0x7B, not extended.
    **{f"f{n}": (0x6F + n, False) for n in range(1, 13)},
}

# Modifier -> (virtual key, extended), in the order they go down.
MODIFIERS: Dict[str, Tuple[int, bool]] = {
    "ctrl": (VK_CONTROL, False),
    "alt": (VK_MENU, False),
    "shift": (VK_SHIFT, False),
    "win": (VK_LWIN, True),
}


class InputRejected(ValueError):
    """An input message that is malformed or out of range; nothing is sent."""


@dataclass(frozen=True)
class MouseEvent:
    """One ``MOUSEINPUT``: ``data`` is the signed wheel delta for a wheel."""

    flags: int
    dx: int = 0
    dy: int = 0
    data: int = 0


@dataclass(frozen=True)
class KeyEvent:
    """One ``KEYBDINPUT``: a virtual key, or a UTF-16 unit when ``KEYEVENTF_UNICODE``."""

    vk: int = 0
    scan: int = 0
    flags: int = 0

    @property
    def up(self) -> bool:
        return bool(self.flags & KEYEVENTF_KEYUP)

    def released(self) -> "KeyEvent":
        return KeyEvent(self.vk, self.scan, self.flags | KEYEVENTF_KEYUP)


InputEvent = Union[MouseEvent, KeyEvent]


# ------------------------------------------------------------- the mapping


def virtual_desktop(monitors: Sequence[Monitor]) -> Tuple[int, int, int, int]:
    """``(left, top, width, height)`` of the virtual desktop: the bounding box
    of every monitor, which is what Windows defines it as."""
    if not monitors:
        raise InputRejected("no monitors")
    left = min(m.left for m in monitors)
    top = min(m.top for m in monitors)
    right = max(m.left + m.width for m in monitors)
    bottom = max(m.top + m.height for m in monitors)
    return left, top, right - left, bottom - top


def map_point(
    monitor: Monitor, desktop: Tuple[int, int, int, int], x: float, y: float
) -> Tuple[int, int]:
    """Normalised ``(x, y)`` in ``monitor`` -> ``SendInput``'s absolute 0-65535.

    ``x``/``y`` in 0-1 pick a physical pixel on the monitor (1.0 is its last
    pixel, never the next monitor's first); that pixel is then scaled over
    the virtual desktop ``desktop`` (from :func:`virtual_desktop`) the way
    the tower probe proved lands on the intended pixel.
    """
    px = monitor.left + min(monitor.width - 1, int(x * monitor.width))
    py = monitor.top + min(monitor.height - 1, int(y * monitor.height))
    vx, vy, vw, vh = desktop
    return (
        round((px - vx) * 65535 / max(vw - 1, 1)),
        round((py - vy) * 65535 / max(vh - 1, 1)),
    )


# ---------------------------------------------------------- event builders


def _fraction(msg: Mapping[str, Any], name: str) -> float:
    value = msg.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputRejected(f"{name} must be a number")
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise InputRejected(f"{name} must be within 0-1")
    return float(value)


def _wheel(msg: Mapping[str, Any], name: str) -> int:
    value = msg.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InputRejected(f"{name} must be a number")
    return max(-MAX_SCROLL, min(MAX_SCROLL, int(value)))


def _button(msg: Mapping[str, Any]) -> str:
    button = msg.get("button", "left")
    if button not in BUTTONS:
        raise InputRejected("unknown button")
    return button


def key_events(vk: int, extended: bool) -> Tuple[KeyEvent, KeyEvent]:
    """A virtual key's down and up."""
    flags = KEYEVENTF_EXTENDEDKEY if extended else 0
    down = KeyEvent(vk=vk, flags=flags)
    return down, down.released()


def text_events(text: str) -> List[KeyEvent]:
    """Type ``text`` as Unicode key packets; a newline is Enter, a tab Tab.

    Each UTF-16 unit goes down and up on its own, so a character outside the
    BMP (an emoji) arrives as its surrogate pair. Other control characters
    are dropped.
    """
    events: List[KeyEvent] = []
    for ch in text:
        if ch == "\n":
            events.extend(key_events(VK_RETURN, False))
        elif ch == "\t":
            events.extend(key_events(VK_TAB, False))
        elif ord(ch) < 0x20 or ch == "\x7f":
            continue
        else:
            encoded = ch.encode("utf-16-le")
            for i in range(0, len(encoded), 2):
                unit = int.from_bytes(encoded[i:i + 2], "little")
                down = KeyEvent(scan=unit, flags=KEYEVENTF_UNICODE)
                events.extend((down, down.released()))
    return events


def resolve_char(ch: str) -> Optional[Tuple[int, bool]]:
    """``(virtual key, needs Shift)`` for a printable ASCII character on the
    PC's current keyboard layout, or None when no key types it by itself.

    On Windows this asks the layout of the foreground window's thread (the one
    the keystroke will reach), so ``?`` is Shift + ``/`` on a US layout and
    its own key elsewhere. Anywhere else, and under ``FAKE_INPUT_ENV`` (the
    gate and the tests, which must not depend on the box's layout), it is the
    US layout.
    """
    if sys.platform == "win32" and os.environ.get(FAKE_INPUT_ENV, "") != "1":
        return _windows_layout_vk(ch)
    return _US_LAYOUT.get(ch)


def _windows_layout_vk(ch: str) -> Optional[Tuple[int, bool]]:
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
    user32.GetKeyboardLayout.restype = ctypes.c_void_p
    user32.VkKeyScanExW.argtypes = [ctypes.c_wchar, ctypes.c_void_p]
    user32.VkKeyScanExW.restype = ctypes.c_short
    thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    scan = user32.VkKeyScanExW(ch, user32.GetKeyboardLayout(thread))
    if scan == -1:
        return None
    vk, state = scan & 0xFF, (scan >> 8) & 0xFF
    # Bit 0 is Shift. Ctrl / Alt / AltGr in the state mean the character is
    # not a key of its own here, and a combo built on it would be wrong.
    if vk == 0xFF or state & ~1:
        return None
    return vk, bool(state & 1)


def _us_layout() -> Dict[str, Tuple[int, bool]]:
    table: Dict[str, Tuple[int, bool]] = {}
    for ch, vk in zip("`-=[]\\;',./", (0xC0, 0xBD, 0xBB, 0xDB, 0xDD, 0xDC, 0xBA, 0xDE, 0xBC, 0xBE, 0xBF)):
        table[ch] = (vk, False)
    for ch, vk in zip('~_+{}|:"<>?', (0xC0, 0xBD, 0xBB, 0xDB, 0xDD, 0xDC, 0xBA, 0xDE, 0xBC, 0xBE, 0xBF)):
        table[ch] = (vk, True)
    for ch, digit in zip("!@#$%^&*()", "1234567890"):
        table[ch] = (ord(digit), True)
    return table


_US_LAYOUT = _us_layout()


def combo_events(key: str, mods: Sequence[str]) -> List[KeyEvent]:
    """A named key or a single printable ASCII character, with modifiers held
    around it.

    Letters and digits are their own virtual keys. Any other character is
    resolved on the PC's keyboard layout (:func:`resolve_char`); one that
    needs Shift there gets it added, and one that does not resolve is
    rejected. Modifiers go out as virtual keys, never Unicode, so an app sees
    Ctrl+C as a shortcut. The whole combo is one batch: nothing is left held.
    """
    unknown = [m for m in mods if m not in MODIFIERS]
    if unknown:
        raise InputRejected("unknown modifier")
    wanted = set(mods)
    if key in NAMED_KEYS:
        vk, extended = NAMED_KEYS[key]
    elif len(key) == 1 and key.isascii() and key.isalnum():
        vk, extended = ord(key.upper()), False
    elif len(key) == 1 and key.isascii() and key.isprintable() and key != " ":
        resolved = resolve_char(key)
        if resolved is None:
            raise InputRejected("unknown key")
        vk, needs_shift = resolved
        extended = False
        if needs_shift:
            wanted.add("shift")
    else:
        raise InputRejected("unknown key")
    held = [MODIFIERS[m] for m in MODIFIERS if m in wanted]
    downs = [key_events(mvk, mext)[0] for mvk, mext in held]
    key_down, key_up = key_events(vk, extended)
    return downs + [key_down, key_up] + [d.released() for d in reversed(downs)]


# ---------------------------------------------------------------- injectors


class Injector(Protocol):
    def send(self, events: Sequence[InputEvent]) -> int:
        """Inject ``events`` in order; return how many Windows accepted."""


class RecordingInjector:
    """Records what would be injected and injects nothing (tests, the gate)."""

    def __init__(self) -> None:
        self.batches: List[List[InputEvent]] = []
        self._lock = threading.Lock()

    def send(self, events: Sequence[InputEvent]) -> int:
        with self._lock:
            self.batches.append(list(events))
        return len(events)

    @property
    def events(self) -> List[InputEvent]:
        with self._lock:
            return [e for batch in self.batches for e in batch]


class SendInputInjector:
    """The real thing: ``user32.SendInput`` through ctypes. Windows only."""

    def __init__(self) -> None:
        self._types: Optional[Dict[str, Any]] = None

    def _structs(self) -> Dict[str, Any]:
        if self._types is not None:
            return self._types
        from ctypes import wintypes

        ulong_ptr = ctypes.c_size_t

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ulong_ptr)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ulong_ptr)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                        ("wParamH", wintypes.WORD)]

        class _U(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        self._types = {"MOUSEINPUT": MOUSEINPUT, "KEYBDINPUT": KEYBDINPUT,
                       "U": _U, "INPUT": INPUT}
        return self._types

    def send(self, events: Sequence[InputEvent]) -> int:
        if sys.platform != "win32" or not events:
            return 0
        desktop_capture.ensure_dpi_awareness()
        t = self._structs()
        inputs = []
        for e in events:
            if isinstance(e, MouseEvent):
                mi = t["MOUSEINPUT"](e.dx, e.dy, e.data & 0xFFFFFFFF, e.flags, 0, 0)
                inputs.append(t["INPUT"](type=0, u=t["U"](mi=mi)))
            else:
                ki = t["KEYBDINPUT"](e.vk, e.scan, e.flags, 0, 0)
                inputs.append(t["INPUT"](type=1, u=t["U"](ki=ki)))
        array = (t["INPUT"] * len(inputs))(*inputs)
        return int(ctypes.windll.user32.SendInput(len(inputs), array, ctypes.sizeof(t["INPUT"])))


_injector_lock = threading.Lock()
_injector: Optional[Injector] = None


def get_injector() -> Injector:
    """The process's injector: real, or a recorder under ``FAKE_INPUT_ENV``."""
    global _injector
    with _injector_lock:
        if _injector is None:
            if os.environ.get(FAKE_INPUT_ENV, "") == "1":
                logger.info("ℹ️ Screen: %s=1, input is recorded and never sent", FAKE_INPUT_ENV)
                _injector = RecordingInjector()
            else:
                _injector = SendInputInjector()
        return _injector


# ------------------------------------------------------------------ session


class InputSession:
    """One Screen socket's input: validates messages, sends them, and knows
    what is held down so :meth:`release_all` can lift it."""

    def __init__(
        self,
        injector: Injector,
        monitor: Monitor,
        monitors: Sequence[Monitor],
        clipboard: Optional[desktop_clipboard.Clipboard] = None,
    ) -> None:
        self._injector = injector
        # None: the process's clipboard, asked for on the first transfer, so a
        # session that never pastes never touches the clipboard seam.
        self._clipboard = clipboard
        self._replies: List[Dict[str, Any]] = []
        self._monitor = monitor
        self._desktop = virtual_desktop(monitors)
        self._lock = threading.Lock()
        self._held_buttons: Set[str] = set()
        self._held_keys: Set[KeyEvent] = set()
        self._warned_short = False
        self.count = 0
        self.clipboard_to_pc = 0

    @property
    def held(self) -> Tuple[Set[str], Set[KeyEvent]]:
        with self._lock:
            return set(self._held_buttons), set(self._held_keys)

    def handle_text(self, text: str) -> bool:
        """A text frame from the socket; True when it was an accepted input."""
        if len(text) > MAX_MESSAGE_CHARS and not (
            text.startswith(CLIPBOARD_FRAME_PREFIX) and len(text) <= MAX_CLIPBOARD_FRAME_CHARS
        ):
            logger.debug("Screen: input frame over %d chars ignored", MAX_MESSAGE_CHARS)
            return False
        try:
            msg = json.loads(text)
        except ValueError:
            logger.debug("Screen: non-JSON text frame ignored")
            return False
        if not isinstance(msg, dict):
            return False
        try:
            return self.handle(msg)
        except InputRejected as exc:
            # The reason only, never the message: it can carry typed text.
            logger.debug("Screen: input message rejected: %s", exc)
            return False

    def handle(self, msg: Mapping[str, Any]) -> bool:
        """Send one input message; raises :class:`InputRejected` when invalid."""
        if msg.get("type") == "clipboard_set":
            return self._clipboard_set(msg)
        with self._lock:
            events = self._events_for(msg)
            if not events:
                return False
            self._send(events)
            self.count += 1
            return True

    def take_replies(self) -> List[Dict[str, Any]]:
        """The messages for the phone that handling input has queued, oldest
        first (a clipboard transfer's outcome); the socket sends them."""
        with self._lock:
            replies, self._replies = self._replies, []
            return replies

    def release_all(self) -> int:
        """Lift every key and button still down; return how many were held."""
        with self._lock:
            events: List[InputEvent] = [k.released() for k in self._held_keys]
            events += [MouseEvent(BUTTONS[b][1]) for b in self._held_buttons]
            if events:
                accepted = self._injector.send(events)
                if accepted < len(events):
                    logger.warning(
                        "⚠️ Screen: releasing held input on disconnect, SendInput "
                        "accepted %d/%d", accepted, len(events),
                    )
                else:
                    logger.info("ℹ️ Screen: released %d held input(s) on disconnect", len(events))
            self._held_keys.clear()
            self._held_buttons.clear()
            return len(events)

    def _clipboard_set(self, msg: Mapping[str, Any]) -> bool:
        """Put the message's text on the PC's clipboard and queue the reply.

        Not under the input lock: a locked clipboard is retried for a moment,
        and a click must not wait behind it. The reply carries a reason class
        and nothing of the text, and neither does any log line here.
        """
        text = msg.get("text")
        if not isinstance(text, str) or not text:
            return self._clipboard_reply(False, "empty")
        if len(text.encode("utf-8", "surrogatepass")) > desktop_clipboard.MAX_CLIPBOARD_BYTES:
            return self._clipboard_reply(False, "too_large")
        try:
            if self._clipboard is None:
                self._clipboard = desktop_clipboard.get_clipboard()
            self._clipboard.set_text(text)
        except desktop_clipboard.ClipboardBusy:
            logger.info("ℹ️ Screen: the PC clipboard stayed locked, nothing was copied")
            return self._clipboard_reply(False, "busy")
        except desktop_clipboard.ClipboardError as exc:
            logger.warning("⚠️ Screen: the PC clipboard could not be written: %s", exc)
            return self._clipboard_reply(False, "error")
        with self._lock:
            self.clipboard_to_pc += 1
        return self._clipboard_reply(True, None)

    def _clipboard_reply(self, ok: bool, reason: Optional[str]) -> bool:
        reply: Dict[str, Any] = {"type": "clipboard", "direction": "to_pc", "ok": ok}
        if reason:
            reply["reason"] = reason
        with self._lock:
            self._replies.append(reply)
        return ok

    # -- internals (called under the lock)

    def _move(self, msg: Mapping[str, Any]) -> MouseEvent:
        ax, ay = map_point(self._monitor, self._desktop, _fraction(msg, "x"), _fraction(msg, "y"))
        return MouseEvent(
            MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, ax, ay
        )

    def _events_for(self, msg: Mapping[str, Any]) -> List[InputEvent]:
        kind = msg.get("type")
        if kind == "click":
            down, up = BUTTONS[_button(msg)]
            return [self._move(msg), MouseEvent(down), MouseEvent(up)]
        if kind == "down":
            button = _button(msg)
            move = self._move(msg)
            if button in self._held_buttons:
                return [move]
            return [move, MouseEvent(BUTTONS[button][0])]
        if kind == "move":
            return [self._move(msg)]
        if kind == "up":
            button = _button(msg)
            move = self._move(msg)
            if button not in self._held_buttons:
                return [move]
            return [move, MouseEvent(BUTTONS[button][1])]
        if kind == "scroll":
            move = self._move(msg)
            dx, dy = _wheel(msg, "dx"), _wheel(msg, "dy")
            events: List[InputEvent] = []
            if dy:
                events.append(MouseEvent(MOUSEEVENTF_WHEEL, data=dy))
            if dx:
                events.append(MouseEvent(MOUSEEVENTF_HWHEEL, data=dx))
            return [move] + events if events else []
        if kind == "text":
            text = msg.get("text")
            if not isinstance(text, str) or not text:
                raise InputRejected("text must be a non-empty string")
            if len(text) > MAX_TEXT_CHARS:
                raise InputRejected("text too long")
            return text_events(text)
        if kind == "key":
            key = msg.get("key")
            mods = msg.get("mods", [])
            if not isinstance(key, str) or not isinstance(mods, list) or not all(
                isinstance(m, str) for m in mods
            ):
                raise InputRejected("key must be a string, mods a list of strings")
            return combo_events(key, mods)
        raise InputRejected("unknown input type")

    def _send(self, events: Sequence[InputEvent]) -> None:
        accepted = max(0, min(len(events), self._injector.send(events)))
        for event in events[:accepted]:
            self._track(event)
        if accepted < len(events) and not self._warned_short:
            self._warned_short = True
            logger.info(
                "ℹ️ Screen: SendInput accepted %d/%d events (an elevated window "
                "or the secure desktop refuses input)", accepted, len(events),
            )

    def _track(self, event: InputEvent) -> None:
        if isinstance(event, MouseEvent):
            for button, (down, up) in BUTTONS.items():
                if event.flags & down:
                    self._held_buttons.add(button)
                if event.flags & up:
                    self._held_buttons.discard(button)
            return
        if event.up:
            self._held_keys.discard(KeyEvent(event.vk, event.scan, event.flags & ~KEYEVENTF_KEYUP))
        else:
            self._held_keys.add(event)
