"""Type a message into another process's console — the detached-session send
path (issue #967).

A detached (``kind: remote``) session is a console window the launcher only
tracks by PID: no PTY, no stdin handle, no output stream. The one channel
into it is the console's own input buffer, which every process sharing that
console reads from. This module is that channel: run as a **separate,
console-less** process (``python -m src.console_input <pid>``, spawned with
``CREATE_NO_WINDOW`` by :meth:`src.session_host.RemoteSession.submit_input`)
it frees the hidden console it was given, ``AttachConsole``s to the target,
opens ``CONIN$`` and writes ``KEY_EVENT_RECORD``s with ``WriteConsoleInputW``
— the message's UTF-16 units first, then, after a short settle, one
``VK_RETURN`` that submits it. The text arrives on **stdin**, never on the
command line: argv is visible to every process on the box and could not
carry a long message anyway.

Why a subprocess and not an in-process call: ``AttachConsole`` fails with
``ERROR_ACCESS_DENIED`` from a process that already owns a console, and the
session-host does — freeing and re-attaching inside the host would yank the
console out from under every other thread. A fresh helper pays a ~100 ms
Python start-up per message and touches nothing else. It is deliberately
**not imported** by the session-host (string-referenced only), so it stays
off the ``## session-host`` import closure in ``CLAUDE.md``: a fix here goes
live on the next send, without the ``:8446`` restart that kills every live
PTY.

Empirical facts this encodes (probe on the issue, 2026-09-14, Windows
Terminal host — the box's default — with Claude Code, Codex, Pi, Copilot and
Antigravity. Grok was re-probed the same way on 2026-09-19 once it was
signed in on the box — issue #1069, 4/4, same settle and same soft-newline
behaviour — so every Coding-tab agent is now proven; classic conhost is
still not probed):

- ``FreeConsole()`` **before** ``AttachConsole`` even under
  ``CREATE_NO_WINDOW`` — a NO_WINDOW child still owns a (hidden) console.
- One key-down + key-up record per UTF-16 unit, ``UnicodeChar`` carrying the
  unit, ``wVirtualKeyCode``/``wVirtualScanCode`` from ``VkKeyScanW`` /
  ``MapVirtualKeyW`` and ``SHIFT_PRESSED`` when the layout needs Shift, is
  read correctly by both Node's libuv (Claude Code — VT input mode on) and
  crossterm (Codex — VT input mode off): both take ``UnicodeChar`` as the
  character and only look at the VK for special keys.
- A newline inside the message goes out as ``VK_RETURN`` **with**
  ``SHIFT_PRESSED``, which Claude Code's composer takes as a soft line break
  (verified: a two-line message arrived as two lines and one submit). The
  plain ``VK_RETURN`` is reserved for the single submitting Enter.
- A ~0.5 s settle between the text and the Enter was enough for a 114-unit
  message and 0.8 s for a 1192-unit one; :func:`settle_seconds` scales
  between those. Nothing here can *observe* the agent consuming the records
  — the verdict the caller reports is ``unconfirmed`` by design.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from ctypes import wintypes as wt
from typing import List, Optional, Sequence

KEY_EVENT = 0x0001
VK_RETURN = 0x0D
SCAN_RETURN = 0x1C
SHIFT_PRESSED = 0x0010

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x1
_FILE_SHARE_WRITE = 0x2
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = wt.HANDLE(-1).value

# Settle between the last text record and the submitting Enter (see the
# module docstring for the measurements): 0.5 s floor, +1 ms per UTF-16
# unit, 3 s cap. The Enter sits behind the text in the same FIFO input
# buffer regardless — the settle only gives a composer that classifies a
# burst as a paste time to finish ingesting before the Enter arrives.
SETTLE_FLOOR_S = 0.5
SETTLE_PER_UNIT_S = 0.001
SETTLE_CAP_S = 3.0


class ConsoleInputError(RuntimeError):
    """A Win32 console call failed; the message carries the call and the
    formatted ``GetLastError`` text."""


class KEY_EVENT_RECORD(ctypes.Structure):
    # ``UnicodeChar`` is declared as a WORD, not WCHAR, so a lone surrogate
    # half (one unit of an astral character) can be assigned without
    # ctypes rejecting it as an invalid single character.
    _fields_ = [
        ("bKeyDown", wt.BOOL),
        ("wRepeatCount", wt.WORD),
        ("wVirtualKeyCode", wt.WORD),
        ("wVirtualScanCode", wt.WORD),
        ("UnicodeChar", wt.WORD),
        ("dwControlKeyState", wt.DWORD),
    ]


class _EVENT(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("_pad", ctypes.c_byte * 16)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wt.WORD), ("Event", _EVENT)]


def _key_pair(vk: int, scan: int, unit: int, control_state: int) -> List[INPUT_RECORD]:
    pair = []
    for down in (1, 0):
        rec = INPUT_RECORD()
        rec.EventType = KEY_EVENT
        rec.Event.KeyEvent.bKeyDown = down
        rec.Event.KeyEvent.wRepeatCount = 1
        rec.Event.KeyEvent.wVirtualKeyCode = vk
        rec.Event.KeyEvent.wVirtualScanCode = scan
        rec.Event.KeyEvent.UnicodeChar = unit
        rec.Event.KeyEvent.dwControlKeyState = control_state
        pair.append(rec)
    return pair


_USER32 = None


def _user32():
    """``user32`` loaded once, with ``VkKeyScanW``'s return declared as the
    ``SHORT`` it is — ctypes' default ``c_int`` leaves the upper bits of a
    16-bit return undefined on x64, so the ``-1`` "no key" sentinel could
    miss and a surrogate half would get a junk VK (review nit on #967)."""
    global _USER32
    if _USER32 is None:
        lib = ctypes.WinDLL("user32", use_last_error=True)
        lib.VkKeyScanW.argtypes = [wt.WCHAR]
        lib.VkKeyScanW.restype = ctypes.c_short
        lib.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]
        lib.MapVirtualKeyW.restype = wt.UINT
        _USER32 = lib
    return _USER32


def _vk_for(unit: int) -> "tuple[int, int, int]":
    """``(virtual key, scan code, control state)`` for one UTF-16 unit.

    Looked up through ``user32`` on Windows; ``(0, 0, 0)`` for a unit the
    active layout has no key for (a surrogate half, an astral character) —
    the readers take ``UnicodeChar`` as the character either way, the VK
    only matters for special keys. Also ``(0, 0, 0)`` off Windows, so the
    record builders stay importable and testable there.
    """
    if sys.platform != "win32":
        return 0, 0, 0
    vk_scan = _user32().VkKeyScanW(wt.WCHAR(chr(unit)))
    if vk_scan == -1:
        return 0, 0, 0
    vk = vk_scan & 0xFF
    # Only the Shift bit is honoured: a Ctrl/Alt (AltGr) layout state would
    # read as a control chord to a TUI, while the UnicodeChar already
    # carries the character.
    shift = SHIFT_PRESSED if (vk_scan >> 8) & 1 else 0
    scan = _user32().MapVirtualKeyW(vk, 0)
    return vk, scan, shift


def utf16_units(text: str) -> List[int]:
    """The UTF-16 code units of ``text`` — what ``KEY_EVENT_RECORD`` carries
    one of each (an astral character is two records, not one)."""
    raw = text.encode("utf-16-le", "surrogatepass")
    return [int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)]


def key_records(text: str) -> List[INPUT_RECORD]:
    """Key-down/key-up record pairs typing ``text``.

    ``\\r`` units are dropped (CRLF → LF), ``\\n`` becomes a Shift+Enter
    pair (a soft line break in the agents' composers, never a submit), and
    every other unit is a plain key pair carrying the unit as its
    ``UnicodeChar``.
    """
    records: List[INPUT_RECORD] = []
    for unit in utf16_units(text):
        if unit == 0x0D:
            continue
        if unit == 0x0A:
            records.extend(_key_pair(VK_RETURN, SCAN_RETURN, 0x0D, SHIFT_PRESSED))
            continue
        vk, scan, state = _vk_for(unit)
        records.extend(_key_pair(vk, scan, unit, state))
    return records


def enter_records() -> List[INPUT_RECORD]:
    """The one submitting Enter: a plain ``VK_RETURN`` down/up pair."""
    return _key_pair(VK_RETURN, SCAN_RETURN, 0x0D, 0)


def settle_seconds(unit_count: int) -> float:
    return min(SETTLE_CAP_S, SETTLE_FLOOR_S + SETTLE_PER_UNIT_S * unit_count)


class Win32Console:
    """The three Win32 calls the helper needs, behind one small seam so the
    CLI can be exercised with a fake console in tests."""

    def __init__(self) -> None:
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32.CreateFileW.restype = wt.HANDLE
        self._k32.WriteConsoleInputW.argtypes = [
            wt.HANDLE, ctypes.POINTER(INPUT_RECORD), wt.DWORD, ctypes.POINTER(wt.DWORD),
        ]
        self._conin: Optional[int] = None

    def _fail(self, call: str) -> ConsoleInputError:
        code = ctypes.get_last_error()
        return ConsoleInputError(f"{call} failed: [{code}] {ctypes.FormatError(code).strip()}")

    def attach(self, pid: int) -> None:
        self._k32.FreeConsole()
        if not self._k32.AttachConsole(wt.DWORD(pid)):
            raise self._fail(f"AttachConsole({pid})")
        handle = self._k32.CreateFileW(
            "CONIN$", _GENERIC_READ | _GENERIC_WRITE, _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            None, _OPEN_EXISTING, 0, None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            raise self._fail("CreateFile(CONIN$)")
        self._conin = handle

    def write(self, records: Sequence[INPUT_RECORD]) -> int:
        if self._conin is None:
            raise ConsoleInputError("write before attach")
        if not records:
            return 0
        array = (INPUT_RECORD * len(records))(*records)
        written = wt.DWORD(0)
        if not self._k32.WriteConsoleInputW(self._conin, array, len(records), ctypes.byref(written)):
            raise self._fail("WriteConsoleInputW")
        if written.value != len(records):
            raise ConsoleInputError(
                f"WriteConsoleInputW wrote {written.value} of {len(records)} records"
            )
        return written.value


def type_into_console(
    pid: int,
    text: str,
    submit: bool,
    console: Optional[Win32Console] = None,
    sleep=time.sleep,
) -> dict:
    """Attach to ``pid``'s console, type ``text``, and if ``submit`` press
    Enter after the settle. Returns the JSON-shaped result the CLI prints;
    raises :class:`ConsoleInputError` when a console call fails."""
    console = console or Win32Console()
    console.attach(pid)
    units = utf16_units(text)
    if text:
        console.write(key_records(text))
    if submit:
        if text:
            sleep(settle_seconds(len(units)))
        console.write(enter_records())
    return {"ok": True, "units": len(units), "submitted": bool(submit)}


def main(argv: Optional[Sequence[str]] = None, text: Optional[str] = None,
         console: Optional[Win32Console] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.console_input",
        description="Type stdin into the console owned by PID and submit it (issue #967).",
    )
    parser.add_argument("pid", type=int, help="PID of a process attached to the target console")
    parser.add_argument("--no-submit", action="store_true", help="type the text only, no Enter")
    args = parser.parse_args(argv)
    if text is None:
        # The host sends UTF-8; read the bytes so the console code page
        # never gets a say in what the message contains.
        text = sys.stdin.buffer.read().decode("utf-8")
    try:
        out = type_into_console(args.pid, text, submit=not args.no_submit, console=console)
    except ConsoleInputError as exc:
        out = {"ok": False, "error": str(exc)}
    except OSError as exc:  # a DLL that would not load, or a handle gone stale mid-call
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    # ensure_ascii (the default) keeps the one stdout line safe under any
    # console code page — the host parses it back as JSON either way.
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
