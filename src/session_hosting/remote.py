"""Detached console sessions: the launch, the tracked session and its typed input."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.agents import DEFAULT_AGENT, supports_console_input
from src.diagnostics import is_pid_alive
from src.session_host_input import (
    DELIVERED_UNCONFIRMED,
    INPUT_CONSOLE_FAILED,
    INPUT_DROPPED,
    INPUT_NOOP,
    INPUT_UNVERIFIED,
    SUBMIT_NOT_REQUESTED,
    SUBMIT_NOT_SUBMITTED,
    SUBMIT_UNCONFIRMED,
)
from src.session_hosting.child_env import agent_child_env
from src.session_hosting.kinds import STOP_GRACE_SECONDS, KIND_REMOTE, STOP_KILL
from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)

# The console-input helper a RemoteSession shells out to —
# string-referenced on purpose, never imported: it must stay off the
# session-host import closure (CLAUDE.md "## session-host") so a fix to it
# goes live on the next send instead of needing the :8446 restart that kills
# every live PTY. Run from the repo root so ``-m`` resolves the package.
_CONSOLE_INPUT_MODULE = "src.console_input"
_REPO_ROOT = Path(__file__).resolve().parents[2]
# Helper start-up (~100 ms) + the settle it sleeps (<= 3 s) + slack; a
# helper wedged past this is killed and reported, never waited on forever.
_CONSOLE_INPUT_TIMEOUT_S = 20.0

# Absolute Windows PowerShell 5.1 — never the bare `pwsh` execution-alias stub
# (a 0-byte reparse point that fails when spawned non-interactively).
_POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

# Guess, not measured: PowerShell start-up + `Start-Process` handing back the
# console PID should finish in well under this; a detached launch wedged past
# it fails loudly rather than hanging the create_remote caller forever.
_DETACHED_SPAWN_TIMEOUT_S = 30.0


def _ps_quote(value: str) -> str:
    """Escape ``value`` for embedding inside a PowerShell single-quoted string."""
    return value.replace("'", "''")


def _parse_started_pid(stdout: Optional[str]) -> Optional[int]:
    """Pull the PID ``Start-Process -PassThru`` printed (last numeric line)."""
    for line in reversed((stdout or "").splitlines()):
        text = line.strip()
        if text.isdigit():
            return int(text)
    return None


def launch_detached(
    directory: Path, session_id: str, agent: str, exe: str, flags: str
) -> int:
    """Start ``<exe> <flags>`` in its own console window and return the
    console's PID; ``RuntimeError`` when PowerShell hands back none.

    The orphaning rationale is :meth:`SessionManager.create_remote`'s.
    """
    # `cmd /c` resolves the agent command (e.g. claude.cmd) off PATH and
    # closes the window when the agent exits — same shape as the PTY spawn.
    inner = (
        f'set "APP_LAUNCHER_SESSION_ID={session_id}" && '
        f'set "APP_LAUNCHER_AGENT={agent}" && '
        f"{exe} {flags}"
    ).strip()
    ps_command = (
        "(Start-Process -FilePath 'cmd' "
        f"-ArgumentList '/c {_ps_quote(inner)}' "
        f"-WorkingDirectory '{_ps_quote(str(directory))}' -PassThru).Id"
    )
    # The console inherits its environment through this PowerShell, so the
    # scrub has to happen here too — otherwise a detached session gets the
    # same monochrome, transcript-less child a PTY session would.
    result = subprocess.run(
        [_POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", ps_command],
        capture_output=True,
        text=True,
        creationflags=NO_WINDOW,
        timeout=_DETACHED_SPAWN_TIMEOUT_S,
        env=agent_child_env(session_id, agent),
    )
    pid = _parse_started_pid(result.stdout)
    if pid is None:
        detail = (result.stderr or result.stdout or "").strip()[:200]
        raise RuntimeError(f"Failed to launch detached session: {detail}")
    return pid


@dataclass(frozen=True)
class RemoteInputOutcome:
    """What happened to one message typed into a detached console (#967).

    The console-shaped sibling of :class:`~src.session_host_input.InputOutcome`
    — same ``to_api()`` keys, so every reader of a PTY verdict (the webapp
    route, the Board drawer, ``last_input``) reads this one unchanged — with
    the one difference the whole feature turns on: a console has no output
    stream to verify a write against, so a successful send reports
    ``delivered: "unconfirmed"`` (:data:`DELIVERED_UNCONFIRMED`), never
    ``True``. ``reason`` is ``unverified`` on success (the #929 vocabulary
    for "sent, nothing checked it"), ``dropped`` when the console process is
    gone, ``console_failed`` when the helper could not attach or type.
    """

    reason: str
    submitted: bool = False
    submit_requested: bool = True
    units: int = 0
    error: str = ""

    @property
    def sent(self) -> bool:
        return self.reason in (INPUT_UNVERIFIED, INPUT_NOOP)

    @property
    def submit_state(self) -> str:
        if self.submitted:
            return SUBMIT_UNCONFIRMED
        if not self.submit_requested:
            return SUBMIT_NOT_REQUESTED
        return SUBMIT_NOT_SUBMITTED

    @property
    def delivered(self) -> "bool | str":
        """``"unconfirmed"`` when the keystrokes went into the console,
        ``False`` when they did not — never ``True`` (issue #967)."""
        return DELIVERED_UNCONFIRMED if self.sent else False

    def to_api(self) -> Dict[str, Any]:
        return {
            "delivered": self.delivered,
            "reason": self.reason,
            "ingested": None,
            "submitted": self.submitted,
            "submit_confirmed": None,
            "submit_state": self.submit_state,
            "waited_ms": 0,
            "deferred": False,
            "units": self.units,
            "error": self.error or None,
        }


class RemoteSession:
    """A detached ``claude`` window the launcher tracks but does not stream.

    Spawned in its own console window and deliberately **orphaned out of the
    session-host's process tree** (see :meth:`SessionManager.create_remote`),
    so it stays visible on the PC and survives a session-host *or* a
    ``tray.bat --restart`` tear-down (issue #130) — that survival is the whole
    point of the detached mode. The launcher keeps only the console PID, so the
    session shows up in the running-sessions list and can be killed from the
    phone — there is no PTY, no scrollback, and no WebSocket. Remote control
    comes from the Claude cloud app.

    Since issue #967 the phone can also *type into* that console:
    :meth:`submit_input` shells out to ``src/console_input.py``, which
    attaches to the console by this PID and writes key records into its
    shared input buffer. Nothing can observe the agent consuming them, so the
    verdict is ``delivered: "unconfirmed"`` by design.
    """

    kind = KIND_REMOTE

    def __init__(
        self,
        session_id: str,
        project_dir: str,
        name: str,
        flags: str,
        started_at: float,
        pid: int,
        agent: str = DEFAULT_AGENT,
    ) -> None:
        self.session_id = session_id
        self.project_dir = project_dir
        self.name = name
        self.flags = flags
        self.started_at = started_at
        self._pid = pid
        self.agent = agent
        self.manual_title = ""
        # Most recent submit_input verdict (same role as PtySession.last_input):
        # a detached session that stopped taking console input
        # is a readable state, not a guess.
        self.last_input: Optional[Dict[str, Any]] = None
        # Serializes sends (mirrors PtySession._write_lock): two phones
        # steering the same detached console must not interleave key records.
        self._input_lock = threading.Lock()

    @property
    def alive(self) -> bool:
        return is_pid_alive(self._pid, self.started_at)

    def submit_input(self, data: str, submit: bool) -> RemoteInputOutcome:
        """Type ``data`` into the detached console and, if ``submit``,
        press Enter (issue #967).

        The shape of :meth:`PtySession.submit_input` minus everything that
        needs an output stream: no bracketed-paste decision, no ingest/echo
        verification, no deferred watcher. The helper process
        (``python -m src.console_input <pid>``, ``CREATE_NO_WINDOW`` per the
        fleet gotcha, text on **stdin**) writes the text's key records, waits
        its own settle, then writes the one submitting Enter. The verdict is
        therefore ``delivered: "unconfirmed"`` on success — never ``True``.

        Refused up front, without spawning anything, for an agent the #967
        probe did not prove (``agents.supports_console_input``): the menu
        never offers those a Send item, and an API caller gets the same
        ``console_failed`` answer rather than keystrokes into an unprobed TUI.
        """
        sid = self.session_id[:8]
        if not data and not submit:
            outcome = RemoteInputOutcome(reason=INPUT_NOOP, submit_requested=False)
            self.last_input = outcome.to_api()
            return outcome
        if not supports_console_input(self.agent):
            outcome = RemoteInputOutcome(
                reason=INPUT_CONSOLE_FAILED, submit_requested=submit,
                error=f"agent {self.agent!r} was not probed for console input",
            )
            self.last_input = outcome.to_api()
            logger.info(f"⚠️ remote {sid} console input refused: {outcome.error}")
            return outcome
        if not self.alive:
            outcome = RemoteInputOutcome(reason=INPUT_DROPPED, submit_requested=submit)
            self.last_input = outcome.to_api()
            return outcome
        argv = [sys.executable, "-m", _CONSOLE_INPUT_MODULE, str(self._pid)]
        if not submit:
            argv.append("--no-submit")
        with self._input_lock:
            outcome = self._run_console_input(argv, data, submit)
        self.last_input = outcome.to_api()
        if outcome.sent:
            # Breadcrumb carries the size only — never the text.
            logger.info(
                f"⌨️ remote {sid} console input typed ({outcome.units} units, "
                f"submit={submit}) — delivery unconfirmed by design"
            )
        else:
            logger.info(
                f"⚠️ remote {sid} console input not typed "
                f"({outcome.reason}, submit={outcome.submit_state}): {outcome.error}"
            )
        return outcome

    def _run_console_input(
        self, argv: List[str], data: str, submit: bool
    ) -> RemoteInputOutcome:
        try:
            result = subprocess.run(
                argv,
                input=data,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=NO_WINDOW,
                timeout=_CONSOLE_INPUT_TIMEOUT_S,
                cwd=str(_REPO_ROOT),
            )
        except subprocess.TimeoutExpired:
            return RemoteInputOutcome(
                reason=INPUT_CONSOLE_FAILED, submit_requested=submit,
                error=f"console input helper timed out after {_CONSOLE_INPUT_TIMEOUT_S:.0f}s",
            )
        except OSError as exc:
            return RemoteInputOutcome(
                reason=INPUT_CONSOLE_FAILED, submit_requested=submit,
                error=f"console input helper could not start: {exc}",
            )
        verdict: Dict[str, Any] = {}
        for line in reversed((result.stdout or "").splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    verdict = json.loads(line)
                except ValueError:
                    verdict = {}
                break
        if result.returncode == 0 and verdict.get("ok") is True:
            return RemoteInputOutcome(
                reason=INPUT_UNVERIFIED,
                submitted=bool(submit),
                submit_requested=submit,
                units=int(verdict.get("units") or 0),
            )
        stderr_tail = (result.stderr or "").strip().splitlines()
        detail = (
            str(verdict.get("error") or "")
            or (stderr_tail[-1] if stderr_tail else "")
            or f"helper exited {result.returncode} with no verdict"
        )
        return RemoteInputOutcome(
            reason=INPUT_CONSOLE_FAILED, submit_requested=submit, error=detail[:300]
        )

    def stop(
        self, mode: str = STOP_KILL, grace_seconds: float = STOP_GRACE_SECONDS
    ) -> None:
        """Stop and close the detached console session.

        Detached processes (RemoteSession) cannot be gracefully stopped without
        closing the window, since they have no stdin/PTY. We use taskkill /T /F
        to terminate the console's whole subtree (cmd.exe + agent + children).
        The console is orphaned from the host tree, but it is still reachable by
        its own PID, so an explicit Stop from the phone still works.

        ``mode`` / ``grace_seconds`` are accepted for interface parity with
        :class:`PtySession` but ignored — there is no PTY to type a quit into.
        """
        if not is_pid_alive(self._pid, self.started_at):
            return

        try:
            subprocess.run(
                ["taskkill", "/PID", str(self._pid), "/T", "/F"],
                capture_output=True,
                creationflags=NO_WINDOW,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug(f"remote {self.session_id[:8]} taskkill failed: {exc}")

    def force_kill(self) -> None:
        self.stop()

    def to_api(self) -> Dict[str, object]:
        return {
            "session_id": self.session_id,
            "kind": self.kind,
            "agent": self.agent,
            "project_dir": self.project_dir,
            "name": self.name,
            "flags": self.flags,
            "started_at": self.started_at,
            "alive": self.alive,
            "live_title": "",
            "manual_title": self.manual_title,
            "last_input": self.last_input,
            # No PTY to read keystrokes from: always unknown.
            "composer_dirty": None,
            "composer_dirty_since": None,
        }
