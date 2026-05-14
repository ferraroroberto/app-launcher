"""Claude Code session discovery + lifecycle.

The launcher spawns ``claude`` in a detached CMD window (see
:func:`src.launcher.spawn_claude`) and keeps no handle, so running
sessions are *discovered* rather than tracked: scan for the ``claude``
CLI's process and keep the ones whose working directory matches a
registered claude-code project.

Stopping is graceful-first — :func:`stop_session` delivers Ctrl+C to
the session's console (Claude Code's own interrupt/exit path) and only
falls back to a force tree-kill if the session doesn't exit in time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover — psutil is in requirements.txt
    psutil = None  # type: ignore

from src.console_ctrl import get_console_title, send_ctrl_c

logger = logging.getLogger(__name__)

# The claude CLI runs under Node (npm install) or as a native shim.
# Match either, then confirm via the command line mentioning "claude".
_SESSION_PROC_NAMES = frozenset({"node.exe", "node", "claude.exe", "claude"})


@dataclass
class ClaudeSession:
    pid: int
    project_dir: str
    name: str
    started_at: float  # epoch seconds (psutil create_time)
    title: Optional[str] = None  # console title — Claude Code's task summary

    def to_api(self) -> Dict[str, object]:
        return {
            "pid": self.pid,
            "project_dir": self.project_dir,
            "name": self.name,
            "started_at": self.started_at,
            "title": self.title,
        }


def _norm(path: str) -> str:
    try:
        return str(Path(path).resolve()).lower()
    except (OSError, ValueError):
        return (path or "").lower()


def _looks_like_claude(proc) -> bool:
    name = (proc.info.get("name") or "").lower()
    if name not in _SESSION_PROC_NAMES:
        return False
    cmdline = " ".join(proc.info.get("cmdline") or []).lower()
    return "claude" in cmdline


def discover_sessions(
    projects: Dict[str, str], with_titles: bool = False
) -> List[ClaudeSession]:
    """Find running Claude Code sessions.

    ``projects`` maps a raw project directory → display name (built
    from the claude-code entries in the registry). A process counts as
    a session when it is the claude CLI *and* its cwd matches one of
    those directories.

    ``with_titles`` additionally reads each session's console title
    (Claude Code's task summary) — skip it on the hot path where only
    the pid set matters, since each title costs a helper subprocess.
    """
    if psutil is None:
        return []
    by_dir = {_norm(raw): name for raw, name in projects.items()}
    found: List[ClaudeSession] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            if not _looks_like_claude(proc):
                continue
            cwd = proc.cwd()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        name = by_dir.get(_norm(cwd))
        if name is None:
            continue
        found.append(
            ClaudeSession(
                pid=int(proc.info["pid"]),
                project_dir=cwd,
                name=name,
                started_at=float(proc.info.get("create_time") or 0.0),
            )
        )
    found.sort(key=lambda s: s.started_at)
    if with_titles:
        for session in found:
            session.title = get_console_title(session.pid)
    return found


def stop_session(
    pid: int, graceful: bool = True, timeout: float = 3.0
) -> Dict[str, object]:
    """Stop a session: Ctrl+C first, force tree-kill as the fallback.

    Returns ``{"stopped": bool, "method": "ctrl_c"|"killed"|"gone"|"none"}``.
    """
    if psutil is None:
        return {"stopped": False, "method": "none", "detail": "psutil unavailable"}
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return {"stopped": True, "method": "gone", "detail": "already exited"}

    if graceful:
        # Claude Code needs Ctrl+C twice to exit an idle session (first
        # press interrupts, second exits) — send it, wait, send again.
        half = max(timeout / 2.0, 0.5)
        if send_ctrl_c(pid) and _wait_gone(proc, half):
            return {"stopped": True, "method": "ctrl_c"}
        if send_ctrl_c(pid) and _wait_gone(proc, half):
            return {"stopped": True, "method": "ctrl_c"}

    killed = _tree_kill(proc)
    return {"stopped": killed, "method": "killed" if killed else "none"}


def _wait_gone(proc, timeout: float) -> bool:
    try:
        proc.wait(timeout=timeout)
        return True
    except psutil.TimeoutExpired:
        return False
    except psutil.NoSuchProcess:
        return True


def _tree_kill(proc) -> bool:
    """Force-kill ``proc`` and its descendants. The detached ``cmd /c``
    wrapper exits on its own once the claude process is gone."""
    try:
        targets = proc.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        targets = []
    targets.append(proc)
    for p in targets:
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    gone, alive = psutil.wait_procs(targets, timeout=3)
    if alive:
        logger.warning(
            f"⚠️  {len(alive)} process(es) survived tree-kill of pid {proc.pid}"
        )
    return not alive
