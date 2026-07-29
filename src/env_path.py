"""The **effective** Windows search path — registry truth, not just inherited.

A Windows process inherits its parent's environment block at spawn and never
re-reads the registry. An installer that appends to ``HKCU\\Environment``'s
``PATH`` and broadcasts ``WM_SETTINGCHANGE`` updates the *persisted* value,
but every already-running process — Explorer included, and therefore
everything launched from the taskbar afterwards — keeps the pre-install
block. So a coding-agent CLI installed while the launcher is running stays
invisible to :func:`shutil.which`, its Coding-tab button greyed, and stays
that way across tray restarts whenever the restart itself is launched from a
stale-environment shell (issue #668 — hit four times in one day while
integrating Grok Build). Restarting Explorer or logging off were the only
reliable remedies; a launcher should not require either.

:func:`effective_path` returns the union of the two registry ``PATH`` values
(machine, then user — the order Windows itself composes them in) and the
inherited one, so a freshly installed CLI resolves on the next detection poll.

Two rules this module never breaks:

* **Never raise, never regress.** Any registry failure, a non-Windows host,
  or a missing value degrades to the inherited ``PATH`` — exactly today's
  behaviour. Detection must never get *worse* than before this existed.
* **Detection and spawn must agree.** Both :func:`src.agents.is_installed`
  and the session-host's child environment resolve through here, so a button
  can't light up for a launch that then dies with "is not recognized as an
  internal or external command".
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import List, Optional

logger = logging.getLogger(__name__)

_MACHINE_ENV_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
_USER_ENV_KEY = r"Environment"

# The registry read is cheap but not free, and `GET /api/agents` is polled.
# A short TTL keeps a fresh install visible within seconds without turning
# every poll into two registry opens.
_CACHE_TTL_S = 10.0

_lock = threading.Lock()
_cached: Optional[str] = None
_cached_at = 0.0


def _read_registry_path(root: int, sub_key: str) -> str:
    """The raw ``PATH`` value under one registry key, or ``""``.

    ``REG_EXPAND_SZ`` values hold unexpanded references (``%SystemRoot%``,
    ``%USERPROFILE%``) — :func:`os.path.expandvars` resolves them against
    *this* process's environment, which is correct: those roots don't move,
    unlike the ``PATH`` entries themselves.
    """
    import winreg  # noqa: PLC0415 — Windows-only, imported at call time

    try:
        with winreg.OpenKey(root, sub_key) as key:
            value, kind = winreg.QueryValueEx(key, "PATH")
    except OSError:
        return ""
    if not isinstance(value, str):
        return ""
    if kind == winreg.REG_EXPAND_SZ:
        value = os.path.expandvars(value)
    return value


def _merge(*paths: str) -> str:
    """Join path strings, dropping blanks and case-insensitive duplicates
    while preserving first-seen order (Windows resolves left to right, so
    the inherited entries must keep their precedence).

    A trailing separator is stripped so ``C:\\Windows\\`` and ``C:\\Windows``
    dedupe to one entry — *except* on a bare drive root, where stripping it
    would change the meaning: ``C:\\`` is the root directory, while ``C:`` is
    "the current directory on drive C:", an entirely different lookup.
    """
    merged: List[str] = []
    seen = set()
    for chunk in paths:
        for entry in chunk.split(os.pathsep):
            entry = entry.strip()
            if not entry.rstrip("\\/").endswith(":"):
                entry = entry.rstrip("\\/")
            if not entry:
                continue
            key = entry.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(entry)
    return os.pathsep.join(merged)


def _registry_path(*, refresh: bool = False) -> str:
    """The machine + user registry ``PATH`` values, joined; ``""`` on any
    failure.

    This is the only expensive half, so this is the only half cached — the
    inherited ``PATH`` is a dict lookup and is re-read on every call. Caching
    the *merged* result instead would pin a stale inherited value for the
    whole TTL, which is both wrong and invisible.
    """
    global _cached, _cached_at
    now = time.monotonic()
    with _lock:
        if not refresh and _cached is not None and now - _cached_at < _CACHE_TTL_S:
            return _cached
    try:
        import winreg

        machine = _read_registry_path(winreg.HKEY_LOCAL_MACHINE, _MACHINE_ENV_KEY)
        user = _read_registry_path(winreg.HKEY_CURRENT_USER, _USER_ENV_KEY)
        value = _merge(machine, user)
    except Exception as exc:  # noqa: BLE001 — detection must never break
        logger.debug(f"env_path: registry read failed ({exc}); inherited PATH only")
        value = ""
    with _lock:
        _cached = value
        _cached_at = now
    return value


def effective_path(*, refresh: bool = False) -> str:
    """The search path to resolve agent commands against.

    On Windows: the inherited ``PATH`` first (so anything this process was
    started with keeps its precedence), then the machine and user registry
    values — which is where a just-installed CLI shows up. Everywhere else,
    and on any registry error, the inherited ``PATH`` unchanged.

    ``refresh=True`` forces the registry half to be re-read rather than
    served from its :data:`_CACHE_TTL_S` cache.
    """
    inherited = os.environ.get("PATH", "")
    if sys.platform != "win32":
        return inherited
    return _merge(inherited, _registry_path(refresh=refresh))
