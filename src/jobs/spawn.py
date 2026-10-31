"""Job process spawning: the detached ``run-job`` spawner and interpreter lookup.

Split out of :mod:`src.jobs.schtasks` so the job kinds, the mutex queue and the
run route reach the spawner without importing the Task Scheduler client. The
launcher's own interpreter/entry-point paths live here because both the
detached spawn and the Task Scheduler ``/TR`` string must name the same
command.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from src.jobs.argv import reject_cmd_unsafe
from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resolve_venv_python(script_path: Path) -> Optional[Path]:
    """Walk up from ``script_path.parent`` looking for ``.venv\\Scripts\\python.exe``.

    Returns the resolved interpreter path, or ``None`` when no ancestor
    directory contains a ``.venv``. The walk stops at the filesystem root.
    Shared by the executor (``build_invocation``) and the save-time
    pre-flight (``src.jobs.preflight``).
    """
    try:
        cur = script_path.parent.resolve()
    except OSError:
        return None
    for parent in (cur, *cur.parents):
        candidate = parent / ".venv" / "Scripts" / "python.exe"
        if candidate.is_file():
            return candidate
    return None


def launcher_python(*, visible: bool = False) -> str:
    """The launcher's own venv interpreter, with PATH fallback.

    ``visible=True`` resolves ``python.exe`` (console subsystem) — used by
    jobs so the scheduled task fires with a console window in the
    logged-on session. ``visible=False`` (default) resolves the windowless
    ``pythonw.exe``.
    """
    name = "python.exe" if visible else "pythonw.exe"
    candidate = PROJECT_ROOT / ".venv" / "Scripts" / name
    if candidate.is_file():
        return str(candidate)
    return name


def launcher_py() -> str:
    return str(PROJECT_ROOT / "launcher.py")


def spawn_run_job_detached(
    job_id: str,
    run_id: str,
    trigger: str = "manual",
    params: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
) -> int:
    """Spawn ``launcher.py run-job <id> --run-id <rid> --trigger <t>`` detached.

    Used by the webapp's ``POST /api/jobs/<id>/run`` route to fire a job
    without blocking the request, plus the mutex-queue drain and DAG chain
    dispatch (``src/jobs/queue.py``). Returns the spawned PID — kept only
    for diagnostics; the run record is tracked via the filesystem.

    Re-parented via ``cmd /c start`` (issue #416) rather than
    ``DETACHED_PROCESS``, mirroring ``app/tray/tray.py``'s
    ``_start_session_host()``: that function's own comment documents,
    empirically verified, that ``DETACHED_PROCESS``/``CREATE_NEW_PROCESS_GROUP``
    do NOT escape ``taskkill /T`` (which ``tray.bat --restart`` uses to kill
    the tray's whole subtree) — only re-parenting does. Without this, a job
    fired here stays inside the tray's process subtree and can be silently
    killed mid-run by a ``tray.bat --restart`` that happens anywhere during
    its execution (including one the job's own work triggers, e.g. shipping
    an app-launcher issue via ``/issue-finish``).

    ``params`` (issue #67) is the validated ``{name: value}`` payload from
    the run-now dialog. When present, it is JSON-encoded onto argv as
    ``--params <json>`` so the executor (which re-validates) sees an
    exact byte-for-byte copy. Schedule + Stream-Deck callers omit the
    arg entirely.

    ``dry_run`` (issue #69 'execute' mode) appends ``--dry-run`` so the
    executor spawns the child with ``JOB_DRY_RUN=1`` and stamps the run
    record. Mutex queue / chain callers never set it.
    """
    # This argv is tokenized twice — once by us, once by the `cmd /c start`
    # hop below — and cmd does not honour Python's argv quoting, so a value
    # can land outside quoting where these characters are operators. Same
    # contract `src.jobs_kinds.batch` enforces for `.bat` argv,
    # applied here at the earlier hop and *before* the command line is
    # assembled, so a refused value never reaches a shell at all.
    #
    # Checked per param key/value rather than on the encoded blob: the JSON's
    # own structural quotes are ours, balanced, and fine — what must not get
    # through is a quote or operator inside a *value*, which is the part a
    # webhook payload can reach (`src.jobs.webhook.resolve_mapping`).
    caller_supplied = [job_id, run_id, trigger]
    for key, value in (params or {}).items():
        caller_supplied.append(str(key))
        caller_supplied.append(value if isinstance(value, str) else str(value))
    reject_cmd_unsafe(caller_supplied, "run-job argument")

    argv = [
        launcher_python(),
        launcher_py(),
        "run-job",
        job_id,
        "--run-id",
        run_id,
        "--trigger",
        trigger,
    ]
    if params:
        argv.extend(["--params", json.dumps(params)])
    if dry_run:
        argv.append("--dry-run")
    # `start` launches the child and cmd exits, orphaning it out of this
    # tray's subtree; /b keeps it windowless. CREATE_NO_WINDOW hides the
    # transient cmd.
    cmd = ["cmd", "/c", "start", "", "/b"] + argv
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
        close_fds=True,
    )
    logger.info(f"🚀 spawned run-job {job_id} (rid={run_id}, pid={proc.pid})")
    return proc.pid
