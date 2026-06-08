"""Windows named-mutex primitive: in-process single-instance + start serialization.

CANONICAL, VENDORED VERBATIM from project-scaffolding. Do **not** edit this file
per-app — it is byte-identical across every fleet tray so a fix made once in the
scaffold re-propagates everywhere. App-specific values (the mutex *names*) are
passed in by the caller, never hardcoded here, which is what keeps the file
identical. Full reasoning: scaffold ``docs/windows-tray.md`` (gotcha #4) +
project-scaffolding#39 / #12.

Two guarantees, one primitive (a Windows named mutex via ``ctypes``):

* :class:`SingleInstance` — the tray's in-process single-instance guard. A
  launcher ``.bat`` pre-check cannot guarantee single-instance: two near
  simultaneous ``tray.bat`` runs both pass the CIM detection blind-spot and both
  survive (project-scaffolding#12). The guarantee must live *in the process*.
  Acquire at tray entry; if another process in the session already holds the
  named mutex, ``acquired`` is False and the caller must exit immediately.

* :func:`cross_process_lock` — serializes a check-then-act critical section
  across processes. Wrap the webapp adopt-or-spawn in it so two trays starting
  at once cannot both ``Popen`` uvicorn: the first holds the mutex, spawns, and
  binds the port; the second blocks, then observes the now-listening port and
  *adopts* instead of spawning a duplicate (project-scaffolding#39 root cause 2).

Windows is the supported surface (these are ``pythonw`` tray apps). On any other
platform both helpers degrade to no-ops (single-instance assumed, lock a pass
through) so unit tests import and run cross-platform — the real cross-process
guarantee is Windows-only and that is by design.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

# Win32 return codes (winbase.h / winerror.h).
_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_ERROR_ALREADY_EXISTS = 183


def _create_named_mutex(name: str, initial_owner: bool) -> tuple[Any, bool]:
    """Create/open a Windows named mutex. Returns ``(handle, already_existed)``.

    ``handle`` is None on failure (caller should fail open — never block startup
    on a mutex glitch). ``already_existed`` reflects ``GetLastError() ==
    ERROR_ALREADY_EXISTS`` at creation, i.e. another process owns the name.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    create_mutex.restype = wintypes.HANDLE

    handle = create_mutex(None, initial_owner, name)
    last_error = ctypes.get_last_error()
    if not handle:
        logger.warning("⚠️  CreateMutexW failed for %r (err=%s)", name, last_error)
        return None, False
    return handle, last_error == _ERROR_ALREADY_EXISTS


def _close_handle(handle: Any) -> None:
    import ctypes

    if handle:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)


class SingleInstance:
    """In-process single-instance guard backed by a Windows named mutex.

    Acquire once at tray entry and **keep the instance alive for the lifetime of
    the process** (hold a reference) — the OS releases the mutex when the process
    exits, so a crashed tray frees the name automatically. If another instance
    already holds the name, :attr:`acquired` is False and the caller must exit.

    The mutex ``name`` is the caller's responsibility and should be stable +
    unique per app, e.g. ``"Global\\\\whatsapp-radar-tray"`` (a ``Global\\``
    prefix spans terminal-server sessions; a bare/``Local\\`` name is per
    session — pick per the app's needs).
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._handle = None
        self.acquired = self._acquire()

    def _acquire(self) -> bool:
        if not _IS_WINDOWS:
            return True  # cross-process exclusion is Windows-only; assume single.
        handle, already_existed = _create_named_mutex(self.name, initial_owner=True)
        if handle is None:
            return True  # fail open: a mutex glitch must not block the tray.
        self._handle = handle
        if already_existed:
            # A live sibling owns the name — we are the duplicate; stand down.
            self.release()
            return False
        return True

    def release(self) -> None:
        """Drop the handle. Idempotent. Called on shutdown or when standing down."""
        _close_handle(self._handle)
        self._handle = None

    def __enter__(self) -> SingleInstance:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


@contextmanager
def cross_process_lock(name: str, timeout_s: float = 30.0) -> Iterator[bool]:
    """Serialize a critical section across processes via a Windows named mutex.

    Yields True if the lock was held (the body ran mutually-exclusive), or False
    if acquisition timed out — in which case the caller should proceed
    best-effort rather than deadlock. Use to wrap an adopt-or-spawn check so two
    trays cannot both spawn the same service.

    On non-Windows this is a pass-through that always yields True.
    """
    if not _IS_WINDOWS:
        yield True
        return

    import ctypes

    handle, _ = _create_named_mutex(name, initial_owner=False)
    if handle is None:
        yield True  # fail open rather than serialize-by-deadlock.
        return

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    held = False
    try:
        wait = kernel32.WaitForSingleObject(handle, int(timeout_s * 1000))
        # WAIT_ABANDONED means the previous holder died mid-section; we still own
        # the mutex and must run + release it (and the state it guarded is the
        # caller's concern — here, an idempotent adopt-or-spawn).
        held = wait in (_WAIT_OBJECT_0, _WAIT_ABANDONED)
        if not held:
            logger.warning("⚠️  timed out acquiring lock %r after %ss", name, timeout_s)
        yield held
    finally:
        if held:
            kernel32.ReleaseMutex(handle)
        _close_handle(handle)


__all__ = ["SingleInstance", "cross_process_lock"]
