"""Screen clipboard (#1719): the phone's text onto the PC's clipboard.

The Screen view's input session (the input module's ``InputSession``) hands a
``clipboard_set`` message's text to a :class:`Clipboard`, the clipboard's own
seam beside the input one:

* **One seam.** :class:`Win32Clipboard` is the only code in the repo that
  touches the Windows clipboard; tests and the pre-ship gate's disposable
  webapp (``LAUNCHER_SCREEN_FAKE_INPUT=1``, the same switch as the input
  seam) use :class:`RecordingClipboard`, which keeps the text in memory and
  never touches the real one. ``tests/test_desktop_clipboard.py`` fails if a
  clipboard API appears anywhere else.
* **Reached through the socket only.** Only the input module imports
  this module, and only the gated Screen socket reaches that.
* **A locked clipboard is retried.** Another process can hold the clipboard
  for a moment (``OpenClipboard`` fails while it does), so a write tries a few
  times before it gives up with :class:`ClipboardBusy`, which the phone shows
  as its own message.
* **Nothing copied is logged.** Not the text, not its length.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import List, Optional, Protocol

logger = logging.getLogger(__name__)

# Set by the pre-ship gate's disposable webapp: the same switch as the input
# seam (src.desktop_input.FAKE_INPUT_ENV), so one variable keeps every Screen
# side effect off the real desktop.
FAKE_CLIPBOARD_ENV = "LAUNCHER_SCREEN_FAKE_INPUT"

# The most text one ``clipboard_set`` carries, in UTF-8 bytes. Its own cap,
# separate from the 8192-character input frame: a pasted paragraph or a
# long link is the point of this message.
MAX_CLIPBOARD_BYTES = 64 * 1024

# A locked clipboard is tried this many times, this far apart.
LOCK_ATTEMPTS = 6
LOCK_RETRY_S = 0.05


class ClipboardError(RuntimeError):
    """The PC's clipboard could not be written (not a lock: see below)."""


class ClipboardBusy(ClipboardError):
    """Another process kept the clipboard locked through every attempt."""


class Clipboard(Protocol):
    def set_text(self, text: str) -> None:
        """Replace the clipboard with ``text``; raise :class:`ClipboardBusy`
        when it stays locked, :class:`ClipboardError` for anything else."""


class RecordingClipboard:
    """Keeps what would be copied in memory and touches nothing (tests, the gate)."""

    def __init__(self) -> None:
        self.texts: List[str] = []
        self._lock = threading.Lock()

    def set_text(self, text: str) -> None:
        with self._lock:
            self.texts.append(text)


class Win32Clipboard:
    """The real thing: ``win32clipboard`` (pywin32). Windows only."""

    def set_text(self, text: str) -> None:
        if sys.platform != "win32":
            raise ClipboardError("the clipboard needs Windows")
        try:
            import win32clipboard
            import win32con
        except ImportError as exc:  # pragma: no cover - pywin32 is a requirement
            raise ClipboardError("pywin32 is not available") from exc

        for attempt in range(LOCK_ATTEMPTS):
            try:
                win32clipboard.OpenClipboard()
            except Exception:  # noqa: BLE001 - pywintypes.error: held by another process
                if attempt + 1 < LOCK_ATTEMPTS:
                    time.sleep(LOCK_RETRY_S)
                continue
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
                return
            except Exception as exc:  # noqa: BLE001
                raise ClipboardError("the clipboard refused the text") from exc
            finally:
                win32clipboard.CloseClipboard()
        raise ClipboardBusy("the clipboard stayed locked")


_clipboard_lock = threading.Lock()
_clipboard: Optional[Clipboard] = None


def get_clipboard() -> Clipboard:
    """The process's clipboard: real, or a recorder under ``FAKE_CLIPBOARD_ENV``."""
    global _clipboard
    with _clipboard_lock:
        if _clipboard is None:
            if os.environ.get(FAKE_CLIPBOARD_ENV, "") == "1":
                logger.info("ℹ️ Screen: %s=1, the clipboard is recorded and never touched", FAKE_CLIPBOARD_ENV)
                _clipboard = RecordingClipboard()
            else:
                _clipboard = Win32Clipboard()
        return _clipboard
