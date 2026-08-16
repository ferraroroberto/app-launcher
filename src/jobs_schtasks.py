"""Windows Task Scheduler sync + spawn helpers for the Jobs tab.

Every :class:`~src.jobs_config.Job` with a non-``none`` schedule
materialises as one or more entries under the ``\\AppLauncher\\`` Task
Scheduler folder. ``daily_times`` jobs fan out into
``\\AppLauncher\\<id>-1``, ``…-2``, … so a single schedule with three
wake-ups becomes three Task Scheduler entries pointing at the same
executor.

Also owns the deterministic "next fire" arithmetic: the schtasks "Next Run
Time" string is a locale-formatted, lexically-sorted best-effort value —
fine to *display*, useless to *sort by* or turn into a countdown. The
schedule definition is a small deterministic set, so :func:`next_fire` /
:func:`upcoming_fires` compute the next wall-clock fire ourselves — this is
the field the UI sorts on and renders "in 3h" from. Both concerns answer
"when does this job run" (one via schtasks, one via pure computation), which
is why they share this module.

The single executor that ever runs a job is
:class:`~app.cli.commands.run_job_cmd.RunJobCommand`. Task Scheduler
calls it with ``pythonw launcher.py run-job <id>``; the webapp's
``POST /api/jobs/<id>/run`` route spawns the same command detached and
returns the new ``run_id`` immediately.

Split out of :mod:`src.jobs` (issue #315) — run-history file storage lives
in :mod:`src.jobs_history`, the mutex queue in :mod:`src.jobs_queue`, and
percentiles/health in :mod:`src.jobs_stats`.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.jobs_config import Job, Schedule
from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TASK_NAMESPACE = "AppLauncher"
TASK_FOLDER_PREFIX = f"\\{TASK_NAMESPACE}\\"

# Process-local TTL cache of the bulk schtasks "Next Run Time" query. The
# original Jobs-tab v1 shelled out to schtasks once per job, per /api/jobs
# poll (every 3 s while the tab was open) — N+1 fork+exec on Windows for
# what is effectively a static schedule. Reset by
# `invalidate_next_run_cache` whenever sync/delete writes change Task
# Scheduler state.
_NEXT_RUN_TTL_SECONDS = 30.0
_next_run_cache: Optional[Tuple[float, Dict[str, Optional[str]]]] = None
_next_run_lock = Lock()

# Defensive upper bound when blind-deleting daily_times variants without
# a query first. 24 covers every hour of the day with headroom.
_MAX_DAILY_TIMES_VARIANTS = 24


def resolve_venv_python(script_path: Path) -> Optional[Path]:
    """Walk up from ``script_path.parent`` looking for ``.venv\\Scripts\\python.exe``.

    Returns the resolved interpreter path, or ``None`` when no ancestor
    directory contains a ``.venv``. The walk stops at the filesystem root.
    Shared by the executor (``build_invocation``) and the save-time
    pre-flight (``src.jobs_preflight``).
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


# ----------------------------------------------------------- schtasks I/O


#: ``schtasks.exe`` writes the **OEM** code page (cp850 on this fleet), which
#: is not valid UTF-8. Decoding is pinned here rather than left to the ambient
#: locale because the webapp is spawned with ``PYTHONUTF8=1`` /
#: ``PYTHONIOENCODING=utf-8`` (``app/webapp/manager.py``), which makes
#: ``text=True`` decode as UTF-8 — and a UTF-8 decode of OEM bytes yields *no
#: stdout at all*, so `_bulk_records` concluded "query failed" and every
#: schtasks-backed feature degraded silently: blank ``next_run`` on all 20
#: jobs and the structural half of missed-fire coverage stuck at ``unknown``
#: fleet-wide, for weeks (issue #743). ``errors="replace"`` keeps one odd byte
#: from costing the whole query — a mangled character in a task name is
#: recoverable, a dead scheduler view is not.
SCHTASKS_ENCODING = "oem"
SCHTASKS_ENCODING_ERRORS = "replace"


def _run_schtasks(argv: List[str]) -> subprocess.CompletedProcess:
    """Invoke ``schtasks.exe`` with ``argv``. Module-level so tests can mock it."""
    return subprocess.run(
        argv,
        capture_output=True,
        check=False,
        creationflags=NO_WINDOW,
        encoding=SCHTASKS_ENCODING,
        errors=SCHTASKS_ENCODING_ERRORS,
    )


#: Absolute Windows PowerShell 5.1 — never the bare ``pwsh`` execution-alias
#: stub, which is a 0-byte WindowsApps reparse point that fails when spawned
#: non-interactively (global CLAUDE.md "Windows PowerShell in spawned
#: commands"; same constant as ``src/session_host.py`` and
#: ``src/jobs_kinds/powershell.py`` — not shared across modules to keep this
#: one self-contained).
_POWERSHELL_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def _ps_quote(value: str) -> str:
    """Escape ``value`` for embedding inside a PowerShell single-quoted string."""
    return value.replace("'", "''")


def _apply_power_policy(
    names: List[str],
    runner: Callable[[List[str]], subprocess.CompletedProcess],
) -> None:
    """Flip the on-battery power policy for every task in ``names`` (issue #746).

    ``schtasks /Create`` has no CLI flags for ``DisallowStartIfOnBatteries``,
    ``StopIfGoingOnBatteries`` or ``StartWhenAvailable`` — Windows applies its
    restrictive defaults (``True``/``True``/``False``). This host runs on an
    APC Smart-UPS, which Windows' ``Win32_Battery`` reports as a battery, so
    those defaults mean a mains loss *would* let Task Scheduler terminate
    every running job and skip every slot that elapses on battery, with no
    catch-up. The hazard is real and standing — ``home-automation``'s UPS
    monitor logs a genuine mains loss to its ``logs/power.jsonl`` roughly
    monthly — which is why this guard stays.

    It is a guard against a hazard, **not** a fix for a diagnosed incident:
    no observed job failure on this host has been traced to this mechanism.
    #746 originally attributed two to it and both attributions were wrong
    (a Windows Update reboot and a logged-out session respectively; see the
    correction on #746 for the evidence). **A silently-skipped scheduled
    task is far more likely to be #757** — every ``\\AppLauncher\\`` task is
    registered ``LogonType=InteractiveToken``, so none of them run at all
    while the machine sits logged out. Check that before suspecting power.

    ``schtasks /Change`` has no equivalent flags either, so this is a
    follow-up ``Set-ScheduledTask`` write per created task — batched into a
    single ``powershell.exe`` spawn (rather than one per task) so an N-slot
    ``daily_times`` job costs one extra process, not N. Reads the existing
    ``Settings`` object first and flips only these three fields, so every
    other schtasks-applied default (execution time limit, compatibility,
    ...) survives untouched.

    ``WakeToRun`` is deliberately left alone — waking the machine overnight
    is a separate behavioural call, not part of this fix.

    Best-effort: a failure here is logged and swallowed. The task(s) still
    exist and still run under Task Scheduler's restrictive defaults, which
    is the pre-existing gap, not a new regression introduced by this call.
    """
    if not names:
        return
    statements = ["$ErrorActionPreference = 'Stop'"]
    quoted_folder = _ps_quote(TASK_FOLDER_PREFIX)
    for full_name in names:
        task_name = _ps_quote(full_name[len(TASK_FOLDER_PREFIX):])
        statements.append(
            f"$t = Get-ScheduledTask -TaskPath '{quoted_folder}' -TaskName '{task_name}'; "
            "$s = $t.Settings; "
            "$s.DisallowStartIfOnBatteries = $false; "
            "$s.StopIfGoingOnBatteries = $false; "
            "$s.StartWhenAvailable = $true; "
            f"Set-ScheduledTask -TaskPath '{quoted_folder}' -TaskName '{task_name}' "
            "-Settings $s | Out-Null"
        )
    argv = [
        _POWERSHELL_EXE,
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "; ".join(statements),
    ]
    proc = runner(argv)
    if proc.returncode != 0:
        logger.warning(
            "⚠️  on-battery power-policy update failed for %s: rc=%s stderr=%s",
            names,
            proc.returncode,
            (proc.stderr or "").strip()[:200],
        )


def _launcher_python(*, visible: bool = False) -> str:
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


def _launcher_py() -> str:
    return str(PROJECT_ROOT / "launcher.py")


def task_run_command(job_id: str, *, visible: bool = False) -> str:
    """The /TR string Task Scheduler stores for ``job_id``.

    Quoted so paths-with-spaces survive Task Scheduler's tokenisation
    when it splits the command into argv to run. A ``visible`` job runs
    under ``python.exe`` (console window in the logged-on session); every
    other job stays on the silent ``pythonw.exe``.
    """
    interpreter = _launcher_python(visible=visible)
    return f'"{interpreter}" "{_launcher_py()}" run-job {job_id}'


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
    dispatch (``src/jobs_queue.py``). Returns the spawned PID — kept only
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
    argv = [
        _launcher_python(),
        _launcher_py(),
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


def task_names_for(job: Job) -> List[str]:
    """The Task Scheduler task names ``job`` materialises into.

    ``daily_times`` is the only schedule that produces more than one;
    every other type produces a single ``\\AppLauncher\\<id>``.
    """
    base = TASK_FOLDER_PREFIX + job.id
    if job.schedule.type == "daily_times" and isinstance(job.schedule.at, list):
        return [f"{base}-{i}" for i in range(1, len(job.schedule.at) + 1)]
    return [base]


def _once_schtasks_parts(at: str) -> List[str]:
    """Split ``YYYY-MM-DDTHH:MM`` into the schtasks ``/SC ONCE /SD … /ST …``
    pieces. Uses the ``YYYY/MM/DD`` slash form for ``/SD`` because it is
    accepted across Windows locales (the dotted / dashed forms are
    locale-dependent and silently fail on non-en-US systems).
    """
    date_part, _, time_part = at.partition("T")
    yyyy, mm, dd = date_part.split("-", 2)
    return [
        "/SC", "ONCE",
        "/SD", f"{yyyy}/{mm}/{dd}",
        "/ST", time_part,
    ]


def schedule_argv_parts(sched: Schedule) -> List[List[str]]:
    """The ``/SC …`` portion(s) of ``schtasks /Create`` — one per task.

    Returns an empty list for ``none``; one inner list for everything but
    ``daily_times``, which returns N (one per HH:MM). ``once`` returns a
    single inner list with ``/SC ONCE /SD <YYYY/MM/DD> /ST <HH:MM>``.
    """
    if sched.type == "none":
        return []
    if sched.type == "minutes":
        return [["/SC", "MINUTE", "/MO", str(sched.every)]]
    if sched.type == "hourly":
        return [["/SC", "HOURLY", "/MO", str(sched.every)]]
    if sched.type == "daily":
        return [["/SC", "DAILY", "/ST", str(sched.at)]]
    if sched.type == "daily_times" and isinstance(sched.at, list):
        return [["/SC", "DAILY", "/ST", str(t)] for t in sched.at]
    if sched.type == "weekly":
        return [["/SC", "WEEKLY", "/D", str(sched.day), "/ST", str(sched.at)]]
    if sched.type == "once" and isinstance(sched.at, str):
        return [_once_schtasks_parts(sched.at)]
    return []


# ------------------------------------------ session-less registration (#757)

#: ``schtasks`` speaks three-letter days; ``New-ScheduledTaskTrigger`` wants
#: the full ``DayOfWeek`` name. Same closed set as
#: :data:`~src.jobs_config_models.WEEKLY_DAYS`.
_PS_WEEKDAYS = {
    "MON": "Monday",
    "TUE": "Tuesday",
    "WED": "Wednesday",
    "THU": "Thursday",
    "FRI": "Friday",
    "SAT": "Saturday",
    "SUN": "Sunday",
}


def _ps_trigger_exprs(sched: Schedule) -> List[str]:
    """``New-ScheduledTaskTrigger`` expressions — one per task in
    :func:`task_names_for` order.

    The PowerShell counterpart of :func:`schedule_argv_parts`, and it fans
    out identically: ``daily_times`` yields one expression per ``HH:MM`` so
    the hand-registered entries carry the *same* ``…-1``/``…-2`` names the
    rest of this module (and the coverage check) expects. Collapsing them
    into one multi-trigger task would register a working schedule that
    :mod:`src.jobs_coverage` then reports as N-1 missing entries.
    """
    if sched.type == "minutes":
        return [
            "New-ScheduledTaskTrigger -Once -At (Get-Date) "
            f"-RepetitionInterval (New-TimeSpan -Minutes {sched.every})"
        ]
    if sched.type == "hourly":
        return [
            "New-ScheduledTaskTrigger -Once -At (Get-Date) "
            f"-RepetitionInterval (New-TimeSpan -Hours {sched.every})"
        ]
    if sched.type == "daily":
        return [f"New-ScheduledTaskTrigger -Daily -At '{_ps_quote(str(sched.at))}'"]
    if sched.type == "daily_times" and isinstance(sched.at, list):
        return [
            f"New-ScheduledTaskTrigger -Daily -At '{_ps_quote(str(t))}'"
            for t in sched.at
        ]
    if sched.type == "weekly":
        day = _PS_WEEKDAYS.get(str(sched.day or "").upper())
        if day is None:
            return []
        return [
            f"New-ScheduledTaskTrigger -Weekly -DaysOfWeek {day} "
            f"-At '{_ps_quote(str(sched.at))}'"
        ]
    if sched.type == "once" and isinstance(sched.at, str):
        return [
            "New-ScheduledTaskTrigger -Once -At "
            f"(Get-Date '{_ps_quote(sched.at)}')"
        ]
    return []


def registration_script(job: Job) -> Optional[str]:
    """The elevated PowerShell that registers ``job``'s session-less entries.

    ``None`` when there is nothing to register — no active schedule, or a
    schedule shape with no trigger mapping. Returns a paste-ready block for
    an **elevated** shell; this process cannot run it itself (that is the
    whole reason ``session_less`` is externally managed — see
    :func:`sync_schtasks`).

    The principal is ``-LogonType S4U``: "run whether the user is logged on
    or not" *without* storing a password. #757's probe measured a real S4U
    token on this host against the things these jobs actually need — same
    SID, ``SessionId=0``, both backup destinations writable, the loopback
    hub and outbound HTTPS reachable, ``gh``'s Credential-Manager token
    still resolving, and headless ``claude -p`` working — 12/12, so S4U is
    capable here and the password-storing ``/RP`` route is not needed.
    ``$env:USERDOMAIN\\$env:USERNAME`` is resolved by the script at run time
    rather than baked in, so the emitted text carries no machine identity.

    Settings mirror what :func:`_apply_power_policy` applies to every
    normally-synced task (issue #746), because ``Register-ScheduledTask``
    replaces the whole settings object rather than merging into it — a
    hand-registered entry that omitted them would silently inherit Windows'
    restrictive on-battery defaults on a UPS-backed host.
    """
    triggers = _ps_trigger_exprs(job.schedule)
    names = task_names_for(job)
    if not triggers or len(triggers) != len(names):
        return None
    # session_less forbids `visible` (validate_principal_shape), so the
    # interpreter is always the windowless one — spelled out rather than
    # passed through so a future `visible` regression can't quietly emit a
    # console-window task that has no desktop to render on.
    interpreter = _launcher_python(visible=False)
    run_level = " -RunLevel Highest" if job.elevated else ""
    # Deliberately ASCII-only: this text is meant to be pasted into a
    # PowerShell console or saved as a .ps1, and Windows PowerShell 5.1
    # mis-parses non-ASCII in an ANSI-saved script file.
    lines = [
        f"# app-launcher issue #757 - register job '{job.id}' to run without a",
        "# logged-on session (S4U). Run from an ELEVATED PowerShell; this",
        "# webapp process cannot register an S4U principal itself.",
        "$ErrorActionPreference = 'Stop'",
        "$user = \"$env:USERDOMAIN\\$env:USERNAME\"",
        (
            "$action = New-ScheduledTaskAction "
            f"-Execute '{_ps_quote(interpreter)}' "
            f"-Argument '\"{_ps_quote(_launcher_py())}\" run-job "
            f"{_ps_quote(job.id)}'"
        ),
        (
            "$principal = New-ScheduledTaskPrincipal -UserId $user "
            f"-LogonType S4U{run_level}"
        ),
        (
            "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
            "-DontStopIfGoingOnBatteries -StartWhenAvailable"
        ),
    ]
    for name, trigger in zip(names, triggers):
        task_name = name[len(TASK_FOLDER_PREFIX):]
        lines.append(
            "Register-ScheduledTask "
            f"-TaskPath '{_ps_quote(TASK_FOLDER_PREFIX)}' "
            f"-TaskName '{_ps_quote(task_name)}' "
            f"-Action $action -Trigger ({trigger}) "
            "-Principal $principal -Settings $settings -Force | Out-Null"
        )
    return "\n".join(lines)


# ----------------------------------------------------------- sync API


def list_known_tasks(
    runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
) -> List[str]:
    """All task names currently under ``\\AppLauncher\\``. Best-effort.

    A failed query (Task Scheduler service down, no permission) returns
    an empty list — the sync layer then falls back to blind deletes so
    a single read failure can't strand stale tasks forever.
    """
    runner = runner or _run_schtasks
    proc = runner(["schtasks", "/Query", "/FO", "CSV", "/NH"])
    if proc.returncode != 0 or not proc.stdout:
        return []
    names: List[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # CSV first column = TaskName, optionally quoted.
        first = line.split(",", 1)[0].strip().strip('"')
        if first.startswith(TASK_FOLDER_PREFIX):
            names.append(first)
    return names


def delete_schtasks(
    job_id: str,
    runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
) -> List[str]:
    """Delete every ``\\AppLauncher\\<job_id>`` and ``…-N`` variant.

    Tries a directed query first; on query failure, falls back to a
    blind delete of the bare name plus ``-1..-N`` so a transient query
    failure can't leave stale tasks behind. Returns the list of task
    names actually deleted (best-effort — schtasks errors are swallowed).
    """
    runner = runner or _run_schtasks
    targets: List[str] = []
    base = TASK_FOLDER_PREFIX + job_id
    known = list_known_tasks(runner=runner)
    if known:
        targets = [
            n
            for n in known
            if n == base or n.startswith(base + "-")
        ]
    else:
        # Blind fallback — covers the bare task + every daily_times slot.
        targets = [base] + [
            f"{base}-{i}" for i in range(1, _MAX_DAILY_TIMES_VARIANTS + 1)
        ]
    deleted: List[str] = []
    for name in targets:
        proc = runner(["schtasks", "/Delete", "/F", "/TN", name])
        if proc.returncode == 0:
            deleted.append(name)
    invalidate_next_run_cache()
    return deleted


def sync_schtasks(
    job: Job,
    runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
) -> List[str]:
    """Re-create the Task Scheduler entries for ``job`` from its schedule.

    Deletes anything currently under ``\\AppLauncher\\<job.id>*`` first,
    then creates one task per schedule slot. Every created task then gets
    its on-battery power policy flipped off Windows' restrictive defaults
    (issue #746, :func:`_apply_power_policy`) — otherwise a real mains loss
    would let Task Scheduler terminate the running job, which this
    UPS-backed host is exposed to about monthly. Returns the list of task
    names created (empty for ``schedule.type == "none"`` after the
    pre-existing tasks are deleted).

    That power policy is the *only* schtasks default this function corrects.
    The principal is still whatever ``schtasks /Create`` defaults to —
    ``LogonType=InteractiveToken``, i.e. "run only when the user is logged
    on" — so every task created here silently does nothing while the machine
    sits logged out (issue #757). If a scheduled job is missing runs, look
    there before looking at power.

    ``job.session_less`` jobs are the opt-out from that default, and are
    **never touched at all** — no create, no delete (issue #757). An S4U
    principal cannot be registered from this non-elevated process by any
    route (measured on #757: ``/RU``+``/RP``, a direct S4U registration, and
    the create-then-``Set-ScheduledTask`` patch that works for *Settings* all
    return ``Access is denied``), so the entry is hand-registered from an
    elevated shell — see :func:`registration_script`. Deleting is skipped
    too, and that is the deliberate difference from the ``elevated``
    carve-out below: a delete here would succeed, destroy an entry this
    process can never recreate, and silently kill the job. The cost of not
    deleting is a stale interactive entry surviving a schedule edit, which
    :mod:`src.jobs_coverage` reports loudly (``principal_interactive`` for a
    task still registered ``Interactive only``, plus the pre-existing
    missed-fire half for the slot that moved) instead of destroying state to
    stay tidy.

    ``job.elevated`` jobs are skipped entirely (issue #352): their real
    ``/RL HIGHEST`` entry can only be created by an already-elevated
    caller, which this webapp process never is. Deleting-then-failing-to
    recreate on every edit/pause/resume silently strands a job that the
    Jobs tab still shows as scheduled — so an elevated job's Task
    Scheduler entry is treated as externally-managed and never touched
    here; it must be registered/updated by hand from an elevated shell.
    """
    runner = runner or _run_schtasks
    if job.session_less:
        # Hands off entirely (see docstring) — this is checked before the
        # elevated branch so a job that is both keeps the safer rule.
        logger.info(
            "ℹ️  job %s is session_less — Task Scheduler entry left untouched "
            "(register it from an elevated shell; see registration_script)",
            job.id,
        )
        return []
    if job.elevated:
        # Still delete any stale entry from a prior non-elevated schedule
        # (issue #409) — otherwise it keeps firing un-elevated on its old
        # schedule indefinitely. We just never *create* the elevated entry
        # ourselves (see docstring): that still needs an elevated shell.
        delete_schtasks(job.id, runner=runner)
        return []
    delete_schtasks(job.id, runner=runner)
    if job.schedule.type == "none":
        return []
    names = task_names_for(job)
    parts = schedule_argv_parts(job.schedule)
    if len(names) != len(parts):
        # Defensive — task_names_for and schedule_argv_parts must agree.
        logger.error(
            f"❌ schedule fan-out mismatch for job {job.id}: "
            f"names={names!r} parts={parts!r}"
        )
        return []
    tr = task_run_command(job.id, visible=job.visible)
    created: List[str] = []
    for name, schedule_part in zip(names, parts):
        argv = [
            "schtasks",
            "/Create",
            "/F",
            "/TN",
            name,
            "/TR",
            tr,
        ] + schedule_part
        proc = runner(argv)
        if proc.returncode == 0:
            created.append(name)
        else:
            logger.warning(
                f"⚠️  schtasks create failed for {name}: "
                f"rc={proc.returncode} stderr={proc.stderr!r}"
            )
    _apply_power_policy(created, runner=runner)
    invalidate_next_run_cache()
    return created


_NEXT_RUN_RE = re.compile(
    r"^Next Run Time:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)
_TASK_NAME_RE = re.compile(
    r"^TaskName:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)

# schtasks renders the enabled/disabled fact under two different keys
# depending on Windows build: the explicit "Scheduled Task State" and the
# coarser "Status" (Ready / Running / Disabled / Could not start). Both are
# read; neither present leaves the fact *unknown* rather than assuming
# enabled-or-disabled either way (see `_parse_bulk_records`).
_STATE_KEYS = ("Scheduled Task State", "Status")
_DISABLED_WORDS = {"DISABLED"}
_ENABLED_WORDS = {"ENABLED", "READY", "RUNNING", "QUEUED"}

# schtasks renders the principal's logon type as "Logon Mode": "Interactive
# only" for the InteractiveToken default (session-bound — issue #757's whole
# defect), and a value containing "Background" for the S4U / stored-password
# principals that also run with no session. Matched by substring, and
# anything else leaves the fact *unknown* rather than guessing — a localised
# Windows renders these strings translated, and "we could not read the
# principal" must never be folded into either answer.
_LOGON_MODE_KEY = "Logon Mode"
_LOGON_MODE_BACKGROUND = "BACKGROUND"
_LOGON_MODE_INTERACTIVE_ONLY = "INTERACTIVE ONLY"


def _parse_bulk_records(stdout: str) -> Dict[str, Dict[str, Any]]:
    """Parse ``schtasks /Query /FO LIST /V`` into ``{task_name: record}``.

    Each task record is a block of ``Key: Value`` lines separated from
    the next by blank line(s). We walk records, pluck the first
    ``TaskName:`` we find plus the fields we care about, and keep only
    entries under ``\\AppLauncher\\`` so foreign tasks never leak into the
    cache.

    Each value is ``{"next_run": Optional[str], "enabled": Optional[bool]}``:

    * ``next_run`` — the raw schtasks string, or ``None`` when it renders
      ``N/A`` / ``Disabled`` / is absent.
    * ``background_capable`` — ``True`` when the task's principal can run
      with no logged-on session ("Logon Mode" mentions Background, i.e. S4U
      or a stored password), ``False`` for "Interactive only", and ``None``
      when the field is absent or unrecognised. This is the fact issue #757
      turns on, and it is the same tri-state discipline as ``enabled``.
    * ``enabled`` — ``True``/``False`` when schtasks states it, and
      ``None`` when *neither* state key is present in the record. A
      registered-but-unreadable task is deliberately not collapsed into
      "disabled": an unestablished fact gets its own value, never the
      failing one (global CLAUDE.md "Verify before declaring done").
    """
    out: Dict[str, Dict[str, Any]] = {}
    block: Dict[str, str] = {}

    def commit(b: Dict[str, str]) -> None:
        name = b.get("TaskName", "").strip()
        if not name.startswith(TASK_FOLDER_PREFIX):
            return
        next_run = b.get("Next Run Time", "").strip()
        # schtasks renders missing / disabled as "N/A" or "Disabled" —
        # both collapse to None at the UI layer.
        if not next_run or next_run.upper() in {"N/A", "DISABLED"}:
            resolved_next: Optional[str] = None
        else:
            resolved_next = next_run
        enabled: Optional[bool] = None
        for key in _STATE_KEYS:
            word = b.get(key, "").strip().upper()
            if not word:
                continue
            if word in _DISABLED_WORDS:
                enabled = False
                break
            if word in _ENABLED_WORDS:
                enabled = True
                break
        mode = b.get(_LOGON_MODE_KEY, "").strip().upper()
        background_capable: Optional[bool] = None
        if _LOGON_MODE_BACKGROUND in mode:
            background_capable = True
        elif _LOGON_MODE_INTERACTIVE_ONLY in mode:
            background_capable = False
        out[name] = {
            "next_run": resolved_next,
            "enabled": enabled,
            "background_capable": background_capable,
        }

    for raw in stdout.splitlines():
        line = raw.rstrip()
        if not line:
            if block:
                commit(block)
                block = {}
            continue
        # New TaskName line ends the previous record (schtasks LIST output
        # has no consistent blank-line separator on all locales).
        m = _TASK_NAME_RE.match(line)
        if m and block.get("TaskName"):
            commit(block)
            block = {}
        if ":" in line:
            key, _, value = line.partition(":")
            block[key.strip()] = value.strip()
    if block:
        commit(block)
    return out


def _parse_bulk_query(stdout: str) -> Dict[str, Optional[str]]:
    """``{task_name: next_run}`` view of :func:`_parse_bulk_records`."""
    return {
        name: record["next_run"]
        for name, record in _parse_bulk_records(stdout).items()
    }


def _bulk_records(
    runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
) -> Optional[Dict[str, Dict[str, Any]]]:
    """One ``schtasks /Query /FO LIST /V`` covering every AppLauncher task.

    ``None`` when the query itself failed — distinct from ``{}`` ("the
    query worked and there are no AppLauncher tasks"). The coverage check
    (:mod:`src.jobs_coverage`) needs that distinction: without it, one
    failed query would flag every scheduled job as missing its task.
    """
    runner = runner or _run_schtasks
    proc = runner(["schtasks", "/Query", "/FO", "LIST", "/V"])
    if proc.returncode != 0 or not proc.stdout:
        # Silence here is what let #743 hide for weeks: every consumer
        # degrades politely to "unknown"/blank, so a dead query looks like a
        # quiet system. Say so once, with the two facts needed to diagnose it.
        logger.warning(
            "⚠️  schtasks bulk query failed (rc=%s, stdout=%d chars) — "
            "next-run and coverage will report unknown: %s",
            proc.returncode,
            len(proc.stdout or ""),
            (proc.stderr or "").strip()[:200] or "<no stderr>",
        )
        return None
    return _parse_bulk_records(proc.stdout)


def _cached_bulk_records(
    runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
) -> Optional[Dict[str, Dict[str, Any]]]:
    """Return the bulk record map, refreshing the cache on TTL miss."""
    global _next_run_cache
    now = time.monotonic()
    with _next_run_lock:
        if _next_run_cache is not None:
            ts, snapshot = _next_run_cache
            if now - ts < _NEXT_RUN_TTL_SECONDS:
                return snapshot
        fresh = _bulk_records(runner=runner)
        _next_run_cache = (now, fresh)
        return fresh


def _cached_bulk_next_runs(
    runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
) -> Dict[str, Optional[str]]:
    """``{task_name: next_run}`` view of the cached bulk snapshot."""
    snapshot = _cached_bulk_records(runner=runner)
    if snapshot is None:
        return {}
    return {name: record["next_run"] for name, record in snapshot.items()}


def registered_task_states(
    runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
) -> Optional[Dict[str, Optional[bool]]]:
    """``{task_name: enabled}`` for every ``\\AppLauncher\\`` task.

    Backed by the same 30 s cached bulk query the "next run" column uses,
    so the coverage check costs no extra ``schtasks`` shell-out on top of
    what an ``/api/jobs`` poll already pays for. Returns ``None`` when the
    query failed — callers must treat that as *unknown*, never as "no
    tasks registered".
    """
    snapshot = _cached_bulk_records(runner=runner)
    if snapshot is None:
        return None
    return {name: record["enabled"] for name, record in snapshot.items()}


def registered_task_principals(
    runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
) -> Optional[Dict[str, Optional[bool]]]:
    """``{task_name: background_capable}`` for every ``\\AppLauncher\\`` task.

    ``True`` = the registered principal runs with no logged-on session (S4U
    or stored password), ``False`` = ``Interactive only``, per-task ``None``
    = registered but the principal could not be read. The whole map is
    ``None`` when the query failed — same contract as
    :func:`registered_task_states`, and served off the same 30 s cached bulk
    query, so the #757 check costs no extra shell-out.
    """
    snapshot = _cached_bulk_records(runner=runner)
    if snapshot is None:
        return None
    return {
        name: record.get("background_capable")
        for name, record in snapshot.items()
    }


def invalidate_next_run_cache() -> None:
    """Drop the cached schtasks snapshot.

    Called after ``sync_schtasks`` / ``delete_schtasks`` so a Task
    Scheduler edit shows up on the next ``/api/jobs`` poll instead of
    waiting out the TTL. The derived missed-fire/coverage cache
    (:mod:`src.jobs_coverage`) is dropped with it — it reads this same
    snapshot, so a stale one would keep a just-fixed job flagged.
    """
    global _next_run_cache
    with _next_run_lock:
        _next_run_cache = None
    # Local import breaks the jobs_coverage -> jobs_schtasks module cycle,
    # same pattern as jobs_reap's local jobs_queue import.
    from src.jobs_coverage import invalidate_coverage_cache

    invalidate_coverage_cache()


def query_next_run(
    job_id: str,
    runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
) -> Optional[str]:
    """Best-effort: the earliest 'Next Run Time' across this job's tasks.

    Backed by a 30 s process-local cache of one bulk ``schtasks /Query``
    call (see :func:`_cached_bulk_next_runs`). Returns ``None`` when no
    task exists, the field is ``N/A``, or the query failed entirely. The
    string is the raw schtasks rendering — the UI is responsible for
    localisation tidying.
    """
    snapshot = _cached_bulk_next_runs(runner=runner)
    base = TASK_FOLDER_PREFIX + job_id
    candidates: List[str] = []
    for name, next_run in snapshot.items():
        if name != base and not name.startswith(base + "-"):
            continue
        if next_run:
            candidates.append(next_run)
    # Sort lexicographically — schtasks renders the locale-default
    # date/time string, so this is a best-effort "earliest"; the legacy
    # code's first-hit behaviour was no better. UI shows the picked
    # string verbatim either way.
    candidates.sort()
    return candidates[0] if candidates else None


# ------------------------------------------------------- computed next fire
#
# The schtasks "Next Run Time" string above is a locale-formatted, lexically
# sorted best-effort value — fine to *display*, useless to *sort by* or to
# turn into a countdown. The schedule definition, however, is a small
# deterministic set (see src.jobs_config), so we compute the next wall-clock
# fire ourselves. This is the field the UI sorts on and renders "in 3h" from.

# Day-name → datetime.weekday() index (Mon=0 .. Sun=6).
_WEEKDAY_INDEX = {
    "MON": 0,
    "TUE": 1,
    "WED": 2,
    "THU": 3,
    "FRI": 4,
    "SAT": 5,
    "SUN": 6,
}


def _hhmm(value: Any) -> Optional[Tuple[int, int]]:
    """Parse ``"HH:MM"`` → ``(hour, minute)``, or ``None`` when malformed."""
    if not isinstance(value, str):
        return None
    try:
        hh, mm = value.split(":", 1)
        h, m = int(hh), int(mm)
    except ValueError:
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h, m
    return None


def next_fire(
    sched: Schedule, *, now: Optional[datetime] = None
) -> Optional[datetime]:
    """The next wall-clock fire time for ``sched``, computed from its shape.

    Pure + deterministic — derived from the bounded schedule definition,
    not from schtasks. Returns ``None`` for ``none`` (which includes a
    *paused* job, whose active schedule is parked as ``none`` while the
    real shape lives in ``paused_schedule``) and for a ``once`` schedule
    that has already elapsed. ``now`` is injectable for testing.

    Computed in local naive time: the launcher and Task Scheduler both run
    in the logged-on session's local time, so this matches what the user
    sees and what actually fires.
    """
    now = now or datetime.now()
    t = sched.type
    if t == "minutes" and isinstance(sched.every, int) and sched.every > 0:
        return now + timedelta(minutes=sched.every)
    if t == "hourly" and isinstance(sched.every, int) and sched.every > 0:
        return now + timedelta(hours=sched.every)
    if t == "daily":
        hm = _hhmm(sched.at)
        if hm is None:
            return None
        candidate = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    if t == "daily_times" and isinstance(sched.at, list):
        best: Optional[datetime] = None
        for entry in sched.at:
            hm = _hhmm(entry)
            if hm is None:
                continue
            candidate = now.replace(
                hour=hm[0], minute=hm[1], second=0, microsecond=0
            )
            if candidate <= now:
                candidate += timedelta(days=1)
            if best is None or candidate < best:
                best = candidate
        return best
    if t == "weekly" and sched.day in _WEEKDAY_INDEX:
        hm = _hhmm(sched.at)
        if hm is None:
            return None
        candidate = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        days_ahead = (_WEEKDAY_INDEX[sched.day] - now.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate
    if t == "once" and isinstance(sched.at, str):
        try:
            fire = datetime.fromisoformat(sched.at)
        except ValueError:
            return None
        return fire if fire > now else None
    # "none" and any malformed shape fall through to no next fire.
    return None


# Schedule types whose cadence is too dense to enumerate over an agenda
# window (issue #230). The agenda summarises these as a single "frequent"
# row instead of one entry per fire. next_fire never returns None for
# them, so upcoming_fires must short-circuit before the enumeration loop.
FREQUENT_SCHEDULE_TYPES = frozenset({"minutes", "hourly"})


def upcoming_fires(
    sched: Schedule, *, start: datetime, end: datetime, cap: int = 200
) -> List[datetime]:
    """Every fire of ``sched`` in the half-open window ``[start, end)``.

    Built by walking :func:`next_fire` forward — each call with
    ``now=cursor`` returns a fire strictly after ``cursor`` (the
    ``candidate <= now`` roll-forward guarantees it), so advancing the
    cursor to each result enumerates the window without re-deriving any
    cadence math (issue #230 reuses #229's tested helper).

    Returns ``[]`` for ``none`` / already-elapsed ``once`` and for the
    dense :data:`FREQUENT_SCHEDULE_TYPES` (``minutes`` / ``hourly``),
    which the agenda summarises rather than expands. ``cap`` bounds the
    list defensively (a 3-slot ``daily_times`` over a week is ~21).
    """
    if sched.type in FREQUENT_SCHEDULE_TYPES:
        return []
    fires: List[datetime] = []
    cursor = start
    while len(fires) < cap:
        nf = next_fire(sched, now=cursor)
        if nf is None or nf >= end:
            break
        fires.append(nf)
        cursor = nf
    return fires
