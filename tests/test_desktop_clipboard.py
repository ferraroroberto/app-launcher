"""Screen clipboard (#1719): the phone's text onto the PC's clipboard.

Nothing here touches the real clipboard: every session runs against a
:class:`src.desktop.clipboard.RecordingClipboard` (and ``tests/conftest.py``'s
autouse ``fake_clipboard`` replaces the process clipboard for the whole suite);
the real :class:`Win32Clipboard` is driven against a fake ``win32clipboard``.

The last section is the security pin, in the shape of the input one in
``tests/test_desktop_input.py``: a clipboard API is reached only from
``src/desktop/clipboard.py``, and that module only through the input session
the gated Screen socket owns. The socket-level tests (reply on the wire, the
close audit) are in ``tests/api/screen/``.
"""

from __future__ import annotations

import ast
import json
import logging
import sys
import types
from pathlib import Path
from typing import List

import pytest

from src.desktop import clipboard as dc
from src.desktop import input as di
from src.desktop.capture import Monitor

REPO = Path(__file__).resolve().parent.parent
MONITOR = Monitor(id=0, left=0, top=0, width=1920, height=1080, primary=True)

# Taken at import, before tests/conftest.py's autouse fixture swaps it out.
_REAL_GET_CLIPBOARD = dc.get_clipboard

SECRET = "hunter2-ñ€ correct horse"


def _session(clipboard=None):
    rec = di.RecordingInjector()
    return di.InputSession(rec, MONITOR, [MONITOR], clipboard), rec


def _frame(text: str) -> str:
    # What the phone's JSON.stringify({type, text}) produces.
    return json.dumps({"type": "clipboard_set", "text": text}, separators=(",", ":"), ensure_ascii=False)


class _Failing:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def set_text(self, text: str) -> None:
        raise self.exc


# ------------------------------------------------------------- the session


def test_a_clipboard_set_lands_on_the_clipboard_and_replies_ok():
    clip = dc.RecordingClipboard()
    session, rec = _session(clip)
    assert session.handle_text(_frame(SECRET)) is True
    assert clip.texts == [SECRET]
    assert session.take_replies() == [{"type": "clipboard", "direction": "to_pc", "ok": True}]
    assert session.take_replies() == [], "a reply is taken once"
    assert session.clipboard_to_pc == 1
    # It is not an input event: nothing was injected and it is not an input.
    assert rec.batches == [] and session.count == 0


def test_the_session_asks_for_the_process_clipboard_only_on_a_transfer(fake_clipboard):
    session, _ = _session()
    assert fake_clipboard.texts == []
    session.handle_text(_frame("abc"))
    assert fake_clipboard.texts == ["abc"]


@pytest.mark.parametrize(
    "text", [None, 5, "", ["a"]], ids=["missing", "number", "empty", "list"],
)
def test_a_clipboard_set_without_text_is_refused_as_empty(text):
    clip = dc.RecordingClipboard()
    session, _ = _session(clip)
    message = {"type": "clipboard_set"} if text is None else {"type": "clipboard_set", "text": text}
    assert session.handle_text(json.dumps(message)) is False
    assert clip.texts == []
    assert session.take_replies() == [
        {"type": "clipboard", "direction": "to_pc", "ok": False, "reason": "empty"}
    ]
    assert session.clipboard_to_pc == 0


def test_the_cap_is_in_utf8_bytes_and_is_its_own_constant():
    assert dc.MAX_CLIPBOARD_BYTES == 64 * 1024
    assert dc.MAX_CLIPBOARD_BYTES > di.MAX_MESSAGE_CHARS
    clip = dc.RecordingClipboard()
    session, _ = _session(clip)

    at_cap = "a" * dc.MAX_CLIPBOARD_BYTES
    assert session.handle_text(_frame(at_cap)) is True
    assert clip.texts == [at_cap]
    session.take_replies()

    # One byte over, and a text whose characters are 2 bytes each: both refused.
    for too_big in ("a" * (dc.MAX_CLIPBOARD_BYTES + 1), "ñ" * (dc.MAX_CLIPBOARD_BYTES // 2 + 1)):
        assert session.handle_text(_frame(too_big)) is False
        assert session.take_replies() == [
            {"type": "clipboard", "direction": "to_pc", "ok": False, "reason": "too_large"}
        ]
    assert clip.texts == [at_cap], "an over-cap text is refused whole, never truncated"
    assert session.clipboard_to_pc == 1


def test_only_a_clipboard_frame_may_pass_the_input_frame_cap():
    clip = dc.RecordingClipboard()
    session, rec = _session(clip)
    big = "x" * (di.MAX_MESSAGE_CHARS * 2)

    # A typed-text frame that long is dropped unparsed, as ever.
    assert session.handle_text(json.dumps({"type": "text", "text": big})) is False
    assert rec.batches == []
    # So is a clipboard frame whose key order is not the phone's, and one past
    # the clipboard frame cap.
    reordered = json.dumps({"text": big, "type": "clipboard_set"}, separators=(",", ":"))
    assert session.handle_text(reordered) is False
    assert session.handle_text(_frame("x" * di.MAX_CLIPBOARD_FRAME_CHARS)) is False
    assert clip.texts == [] and session.take_replies() == []
    # The phone's own frame, over 8192 characters, goes through.
    assert session.handle_text(_frame(big)) is True
    assert clip.texts == [big]


def test_a_json_escaped_text_at_the_cap_fits_the_frame_cap():
    """Control characters grow to six characters each in the frame."""
    clip = dc.RecordingClipboard()
    session, _ = _session(clip)
    text = "\x01" * dc.MAX_CLIPBOARD_BYTES
    frame = _frame(text)
    assert di.MAX_MESSAGE_CHARS < len(frame) <= di.MAX_CLIPBOARD_FRAME_CHARS
    assert session.handle_text(frame) is True


def test_a_locked_clipboard_and_a_refusing_one_each_reply_with_their_own_reason(caplog):
    caplog.set_level(logging.DEBUG)
    busy, _ = _session(_Failing(dc.ClipboardBusy("locked")))
    assert busy.handle_text(_frame(SECRET)) is False
    broken, _ = _session(_Failing(dc.ClipboardError("refused")))
    assert broken.handle_text(_frame(SECRET)) is False
    assert busy.take_replies() == [
        {"type": "clipboard", "direction": "to_pc", "ok": False, "reason": "busy"}
    ]
    assert broken.take_replies() == [
        {"type": "clipboard", "direction": "to_pc", "ok": False, "reason": "error"}
    ]
    assert busy.clipboard_to_pc == broken.clipboard_to_pc == 0
    assert SECRET not in caplog.text, "the copied text reached a log line"


def test_a_transfer_logs_nothing_of_its_content(caplog):
    caplog.set_level(logging.DEBUG)
    session, _ = _session(dc.RecordingClipboard())
    session.handle_text(_frame(SECRET))
    session.handle_text(_frame(""))
    assert SECRET not in caplog.text


# -------------------------------------------------------- the real clipboard


class _FakeWin32:
    """A ``win32clipboard`` whose first ``locked`` opens fail, as another
    process holding the clipboard does."""

    def __init__(self, locked: int) -> None:
        self.locked = locked
        self.opens = 0
        self.closes = 0
        self.data: List = []

    def OpenClipboard(self):
        self.opens += 1
        if self.opens <= self.locked:
            raise OSError("access denied")

    def EmptyClipboard(self):
        self.data.clear()

    def SetClipboardData(self, fmt, text):
        self.data.append((fmt, text))

    def CloseClipboard(self):
        self.closes += 1


@pytest.fixture
def fake_win32(monkeypatch):
    def install(locked: int) -> _FakeWin32:
        fake = _FakeWin32(locked)
        monkeypatch.setitem(sys.modules, "win32clipboard", fake)
        monkeypatch.setitem(sys.modules, "win32con", types.SimpleNamespace(CF_UNICODETEXT=13))
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(dc.time, "sleep", lambda _s: None)
        return fake
    return install


def test_the_real_clipboard_writes_unicode_text_and_closes(fake_win32):
    fake = fake_win32(locked=0)
    dc.Win32Clipboard().set_text(SECRET)
    assert fake.data == [(13, SECRET)]
    assert (fake.opens, fake.closes) == (1, 1)


def test_a_briefly_locked_clipboard_is_retried(fake_win32):
    fake = fake_win32(locked=dc.LOCK_ATTEMPTS - 1)
    dc.Win32Clipboard().set_text("hello")
    assert fake.opens == dc.LOCK_ATTEMPTS and fake.data == [(13, "hello")]


def test_a_clipboard_that_stays_locked_fails_as_busy(fake_win32):
    fake = fake_win32(locked=dc.LOCK_ATTEMPTS + 5)
    with pytest.raises(dc.ClipboardBusy):
        dc.Win32Clipboard().set_text("hello")
    assert fake.opens == dc.LOCK_ATTEMPTS and fake.data == []
    # Busy is its own condition, not just any clipboard failure.
    assert issubclass(dc.ClipboardBusy, dc.ClipboardError)


def test_a_write_that_fails_after_the_open_is_an_error_and_still_closes(fake_win32):
    fake = fake_win32(locked=0)

    def boom(fmt, text):
        raise OSError("refused")

    fake.SetClipboardData = boom
    with pytest.raises(dc.ClipboardError) as excinfo:
        dc.Win32Clipboard().set_text("hello")
    assert not isinstance(excinfo.value, dc.ClipboardBusy)
    assert fake.closes == 1


def test_the_fake_env_gives_a_recorder(monkeypatch):
    """What the gate's disposable webapp gets (``LAUNCHER_SCREEN_FAKE_INPUT=1``)."""
    monkeypatch.setattr(dc, "_clipboard", None)
    monkeypatch.setenv(dc.FAKE_CLIPBOARD_ENV, "1")
    assert isinstance(_REAL_GET_CLIPBOARD(), dc.RecordingClipboard)


def test_the_switch_is_the_same_one_as_the_input_seams():
    assert dc.FAKE_CLIPBOARD_ENV == di.FAKE_INPUT_ENV


def test_without_the_env_the_clipboard_is_the_real_one(monkeypatch):
    # Built, never called: constructing it touches nothing.
    monkeypatch.setattr(dc, "_clipboard", None)
    monkeypatch.delenv(dc.FAKE_CLIPBOARD_ENV, raising=False)
    assert isinstance(_REAL_GET_CLIPBOARD(), dc.Win32Clipboard)


# ------------------------------------------- the one clipboard path (security)


_CLIPBOARD_NAMES = {
    "OpenClipboard", "SetClipboardData", "GetClipboardData", "EmptyClipboard",
    "CloseClipboard", "set_clipboard", "get_clipboard_data",
}
_CLIPBOARD_MODULES = {"win32clipboard", "pyperclip", "clipboard", "tkinter"}
_CLIPBOARD_COMMANDS = ("Set-Clipboard", "Get-Clipboard", "clip.exe", "pbcopy", "xclip")


def _python_files():
    for root in ("src", "app"):
        yield from (REPO / root).rglob("*.py")
    yield REPO / "launcher.py"


def _imported_modules(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            yield base
            for alias in node.names:
                yield f"{base}.{alias.name}" if base else alias.name


def _identifiers(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            yield node.id
        elif isinstance(node, ast.Attribute):
            yield node.attr


def test_only_desktop_clipboard_touches_the_clipboard():
    """No code but ``src/desktop/clipboard.py`` calls a Windows clipboard API,
    imports a clipboard library or shells out to a clipboard command."""
    offenders = []
    for path in _python_files():
        rel = path.relative_to(REPO).as_posix()
        if rel == "src/desktop/clipboard.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = set(_identifiers(tree)) & _CLIPBOARD_NAMES
        mods = {m.split(".")[0] for m in _imported_modules(tree)} & _CLIPBOARD_MODULES
        commands = {
            c for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            for c in _CLIPBOARD_COMMANDS if c in n.value
        }
        if names or mods or commands:
            offenders.append(f"{rel}: {sorted(names | mods | commands)}")
    assert offenders == [], "clipboard access outside src/desktop/clipboard.py: " + "; ".join(offenders)


def test_only_the_input_session_reaches_the_clipboard_seam():
    """``src.desktop.clipboard`` is imported by ``src/desktop/input.py`` alone,
    and that module is imported by the Screen router alone (pinned in
    ``tests/test_desktop_input.py``), so the socket is the only way in."""
    importers = []
    for path in _python_files():
        rel = path.relative_to(REPO).as_posix()
        if rel == "src/desktop/clipboard.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = any(m.startswith("src.desktop.clipboard") for m in _imported_modules(tree))
        dynamic = any(
            isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith("src.desktop.clipboard")
            for n in ast.walk(tree)
        )
        if imported or dynamic:
            importers.append(rel)
    assert importers == ["src/desktop/input.py"]


def test_the_clipboard_has_no_http_route():
    """A route that names the clipboard would be a second way in."""
    from app.webapp.routers import desktop_screen

    for route in desktop_screen.router.routes:
        assert "clipboard" not in getattr(route, "path", "")
    source = (REPO / "app/webapp/routers/desktop_screen.py").read_text(encoding="utf-8")
    assert "desktop import clipboard" not in source and "src.desktop.clipboard" not in source
