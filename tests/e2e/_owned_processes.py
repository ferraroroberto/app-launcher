"""Disposable e2e servers die with the process that started them.

The autoboot fixture stops its webapp and session-host in a ``finally``. A
worker that dies hard never runs it: pytest-timeout ends a stuck worker with
``os._exit``, so both servers (and the venv interpreter each one runs as a
child) are orphaned and keep the checkout pinned. Two layers, each keyed on a
fact only this run owns, never on a process name or command line:

- :func:`spawn_owned` starts a server suspended, puts it in a per-process
  Windows Job Object with ``KILL_ON_JOB_CLOSE`` and only then lets it run, so
  everything it spawns joins the job too. The kernel closes the job handle
  when its owner exits for any reason, and that kills every member.
- :func:`record_owned` appends the server's pid and creation time (and its
  children's) to the ledger the gate names in ``LAUNCHER_E2E_PID_LEDGER``;
  :func:`sweep_ledger`, run by the gate after the browser phase, kills any
  recorded process that is still the same process. The creation time guards
  against a recycled pid.

No pytest import, so the gate can run it as a script::

    python -m tests.e2e._owned_processes <ledger>
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

import psutil

logger = logging.getLogger(__name__)

LEDGER_ENV = "LAUNCHER_E2E_PID_LEDGER"

# Win32 CREATE_SUSPENDED; the subprocess module does not export it.
_CREATE_SUSPENDED = 0x00000004

# Generous: TerminateProcess is asynchronous on Windows.
_KILL_WAIT_S = 5.0

_job_handle: Optional[int] = None


def _create_kill_on_close_job() -> int:
    """Create the Job Object whose last handle closing kills every member."""
    import ctypes
    from ctypes import wintypes

    class _Basic(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _Io(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _Extended(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _Basic),
            ("IoInfo", _Io),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    info = _Extended()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    extended_limit_information = 9
    if not kernel32.SetInformationJobObject(
        wintypes.HANDLE(job), extended_limit_information,
        ctypes.byref(info), ctypes.sizeof(info),
    ):
        err = ctypes.get_last_error()
        kernel32.CloseHandle(wintypes.HANDLE(job))
        raise OSError(err, "SetInformationJobObject failed")
    # Never closed on purpose: the handle lives exactly as long as this
    # process, which is what ties the servers' lifetime to it.
    return int(job)


def _assign_to_job(pid: int) -> None:
    import ctypes
    from ctypes import wintypes

    global _job_handle
    if _job_handle is None:
        _job_handle = _create_kill_on_close_job()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    process_set_quota, process_terminate = 0x0100, 0x0001
    handle = kernel32.OpenProcess(process_set_quota | process_terminate, False, pid)
    if not handle:
        raise OSError(ctypes.get_last_error(), f"OpenProcess({pid}) failed")
    try:
        if not kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(_job_handle), wintypes.HANDLE(handle)
        ):
            raise OSError(ctypes.get_last_error(), f"AssignProcessToJobObject({pid}) failed")
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def spawn_owned(cmd: List[str], **popen_kwargs: Any) -> subprocess.Popen:
    """``subprocess.Popen`` whose process tree dies when this process exits.

    On Windows the child starts suspended so it cannot spawn anything before
    it is in the job; it is always resumed, even when the job cannot be set
    up, so a job failure costs only the guarantee (logged, and the gate's
    ledger sweep still backs it up), never the boot.
    """
    if sys.platform != "win32":
        return subprocess.Popen(cmd, **popen_kwargs)
    popen_kwargs["creationflags"] = (
        popen_kwargs.get("creationflags", 0) | _CREATE_SUSPENDED
    )
    proc = subprocess.Popen(cmd, **popen_kwargs)
    try:
        _assign_to_job(proc.pid)
    except OSError as exc:
        logger.warning(
            "⚠️ autoboot: pid %d is not tied to this process (%s); "
            "only the gate's ledger sweep stops it if this process dies", proc.pid, exc,
        )
    finally:
        psutil.Process(proc.pid).resume()
    return proc


def record_owned(proc: subprocess.Popen) -> None:
    """Append ``proc`` and its current descendants to the gate's ledger, if any.

    The descendants matter because the venv's ``python.exe`` is a launcher
    that runs the real interpreter as its child: if the launcher goes first,
    the interpreter is no longer reachable from its pid.
    """
    ledger = os.environ.get(LEDGER_ENV)
    if not ledger:
        return
    try:
        root = psutil.Process(proc.pid)
        family = [root] + root.children(recursive=True)
        lines = [
            json.dumps({"pid": p.pid, "create_time": p.create_time()}) for p in family
        ]
    except psutil.Error as exc:
        logger.warning("⚠️ autoboot: could not record pid %d in %s: %s", proc.pid, ledger, exc)
        return
    with open(ledger, "a", encoding="utf-8") as handle:
        handle.write("".join(line + "\n" for line in lines))


def _still_ours(pid: int, create_time: float) -> Optional[psutil.Process]:
    try:
        proc = psutil.Process(pid)
        return proc if proc.create_time() == create_time else None
    except psutil.Error:
        return None


def sweep_ledger(ledger: Path) -> Tuple[List[str], List[str]]:
    """Kill every recorded process still alive; return (reaped, survivors).

    Each entry is described as ``pid <n> <name>``. Raises ``ValueError`` on a
    ledger line that does not parse, since that one could hide a live pid.
    """
    targets: List[psutil.Process] = []
    for raw in ledger.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        entry = json.loads(raw)
        proc = _still_ours(int(entry["pid"]), float(entry["create_time"]))
        if proc is not None and proc.pid not in {t.pid for t in targets}:
            targets.append(proc)
    described = [_describe(p) for p in targets]
    for proc in targets:
        try:
            proc.kill()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(targets, timeout=_KILL_WAIT_S)
    survivors = [_describe(p) for p in alive]
    return [d for d in described if d not in survivors], survivors


def _describe(proc: psutil.Process) -> str:
    try:
        return f"pid {proc.pid} {proc.name()}"
    except psutil.Error:
        return f"pid {proc.pid}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Print ``ORPHANS=reaped:<n>`` (exit 0), ``ORPHANS=stuck`` (1) or ``ORPHANS=unknown`` (3)."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m tests.e2e._owned_processes <ledger>", file=sys.stderr)
        return 2
    ledger = Path(args[0])
    if not ledger.is_file():
        # The gate creates the ledger before the browser phase, so a missing
        # one means the recording never happened, not that nothing leaked.
        print(f"ORPHANS=unknown no ledger at {ledger}")
        return 3
    try:
        reaped, survivors = sweep_ledger(ledger)
    except (OSError, ValueError, KeyError) as exc:
        print(f"ORPHANS=unknown unreadable ledger {ledger}: {exc}")
        return 3
    if survivors:
        print(f"ORPHANS=stuck {len(survivors)} recorded process(es) survived the kill: "
              + ", ".join(survivors))
        return 1
    print(f"ORPHANS=reaped:{len(reaped)}" + "".join(f"\n  {r}" for r in reaped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
