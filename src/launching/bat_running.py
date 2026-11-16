"""Is what a ``.bat`` starts already running?

The boot autostart walk runs on every tray start, not only at log on, so a
mini app the owner already has up (or a tray restart) must not get a second
copy. A bat is a launcher, not the process: it may run the work inline (the
``cmd`` that runs it stays alive) or ``start`` a script and exit. Both are
covered by looking for the bat's own path and for the scripts and programs
its body names, in the command lines of the processes now running.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import List, Optional, Set

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover — psutil is in requirements.txt
    psutil = None  # type: ignore

logger = logging.getLogger(__name__)

# A file the bat hands to an interpreter or runs itself. Anything else on a
# command line (a flag, a data file) says nothing about what the bat started.
_TARGET_RE = re.compile(
    r"""(?P<path>[^\s"'<>|&()=,;]+\.(?:py|pyw|ahk|ps1|vbs|js|exe))(?=$|[\s"'<>|&()=,;])""",
    re.IGNORECASE,
)

# Programs every bat names and every machine runs many of: seeing one in a
# process table says nothing about this bat.
_GENERIC_PROGRAMS = frozenset(
    {
        "python", "pythonw", "py", "pyw", "cmd", "powershell", "pwsh",
        "wscript", "cscript", "node", "start", "taskkill", "timeout",
        "ping", "curl", "explorer", "conhost", "where", "findstr", "tasklist",
    }
)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def target_paths(bat_path: Path) -> Set[str]:
    """Normalised absolute paths of the files ``bat_path``'s body runs.

    ``%~dp0`` and a path relative to the bat's folder resolve against it. A
    token still holding another ``%variable%`` cannot be resolved, so it is
    left out rather than guessed.
    """
    try:
        text = bat_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return set()
    folder = str(bat_path.parent)
    # A function, not a string: the folder's backslashes are not regex escapes.
    text = re.sub(r"%~dp0\\?", lambda _m: folder + os.sep, text, flags=re.IGNORECASE)
    found: Set[str] = set()
    for match in _TARGET_RE.finditer(text):
        token = match.group("path")
        if "%" in token:
            continue
        if Path(token).stem.lower() in _GENERIC_PROGRAMS:
            continue
        candidate = token if os.path.isabs(token) else os.path.join(folder, token)
        found.add(_norm(candidate))
    return found


def _process_mentions(proc: "psutil.Process", needles: Set[str]) -> bool:
    info = proc.info
    cwd = info.get("cwd") or ""
    for arg in info.get("cmdline") or []:
        arg = str(arg).strip('"')
        if not arg:
            continue
        resolved = arg if os.path.isabs(arg) else os.path.join(cwd, arg) if cwd else arg
        if _norm(resolved) in needles:
            return True
    exe = info.get("exe") or ""
    return bool(exe) and _norm(exe) in needles


def find_running_target(bat_path: Path) -> Optional[int]:
    """The pid of a process already running ``bat_path`` or what it starts.

    ``None`` means none was found. Raises ``RuntimeError`` when the process
    table cannot be read at all, so a caller never reads "could not look" as
    "nothing is running" and starts a duplicate.
    """
    if psutil is None:
        raise RuntimeError("psutil is not installed")
    needles: Set[str] = {_norm(str(bat_path))} | target_paths(bat_path)
    own = os.getpid()
    scanned: List[int] = []
    try:
        for proc in psutil.process_iter(["pid", "cmdline", "cwd", "exe"]):
            try:
                if proc.info["pid"] == own:
                    continue
                scanned.append(proc.info["pid"])
                if _process_mentions(proc, needles):
                    return int(proc.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except Exception as exc:  # noqa: BLE001 — a table we cannot read is "unknown"
        raise RuntimeError(f"could not read the process table: {exc}") from exc
    if not scanned:
        raise RuntimeError("the process table came back empty")
    return None
