"""Tree-aware teardown for the e2e gate's disposable servers (issue #1335).

The disposable session-host runs every ``--e2e-stub`` launch as a ConPTY:
``OpenConsole.exe --headless`` plus ``cmd.exe`` → ``e2e_stub_child.py`` (a
venv launcher ``python.exe`` and the base ``python.exe`` it starts). Stopping
only the host's own pid is not enough. Twice a gate left the stub pair and its
``OpenConsole.exe`` alive after the host had exited, pinning the worktree so
``git worktree remove`` refused. Nothing else ever reaps them: the stub blocks
on ``readline`` against a console that the surviving stub itself keeps open.

Two layers, both keyed on facts only this run owns:

- :func:`terminate_tree` snapshots a server's descendants *before* stopping
  it, then kills whatever of that snapshot outlived it. The snapshot has to
  come first: once the parent is gone an orphan no longer shows up as its
  descendant.
- :func:`reap_stub_children` catches what the snapshot cannot, a stub whose
  parent chain broke earlier or that started after the snapshot. It matches
  on the stub script's path in the process's argv, and every run writes that
  script under its own pytest temp dir. So a scope is one run (or one xdist
  worker) and never another gate, the live tray or a design-review instance.
  Killing the stub is sufficient: its ``OpenConsole.exe`` exits within a
  second once its last client is gone (measured on #1335).

No pytest import, so a plain script can use it too::

    python -m tests.e2e._process_teardown <dir>   # a pytest temp dir or a checkout
"""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence

import psutil

from tests.e2e.stub_session import STUB_SCRIPT_NAME

logger = logging.getLogger(__name__)

# How long a killed process gets to actually exit before it counts as a
# survivor. Generous: TerminateProcess is asynchronous on Windows.
_REAP_WAIT_S = 5.0


def _kill_all(procs: Sequence[psutil.Process]) -> List[psutil.Process]:
    """Kill ``procs`` and wait; return the ones still alive afterwards."""
    for proc in procs:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _, alive = psutil.wait_procs(list(procs), timeout=_REAP_WAIT_S)
    return alive


def terminate_tree(proc: Optional[subprocess.Popen]) -> None:
    """Stop ``proc`` and every process it had spawned.

    Same graceful sequence as before for the root (CTRL_BREAK, terminate,
    kill after 5s), then any descendant from the pre-stop snapshot that is
    still running is killed. Best effort, never raises: a teardown error must
    not mask the test result.
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        descendants = psutil.Process(proc.pid).children(recursive=True)
    except psutil.Error:
        descendants = []
    try:
        if sys.platform == "win32":
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug("CTRL_BREAK_EVENT failed: %s", exc)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("⚠️  autoboot: process teardown failed: %s", exc)
    survivors = [p for p in descendants if p.is_running()]
    if survivors:
        logger.info(
            "ℹ️ autoboot: reaping %d process(es) that outlived pid %d: %s",
            len(survivors), proc.pid, ", ".join(_describe(p) for p in survivors),
        )
        for p in _kill_all(survivors):
            logger.warning("⚠️ autoboot: could not reap %s", _describe(p))


def _is_stub_under(cmdline: Sequence[str], scope: Path) -> bool:
    """True for a stub child whose script or interpreter lives under ``scope``.

    The script path scopes one run (it sits in that run's pytest temp dir);
    the interpreter path scopes one checkout (``<checkout>/.venv/...``, the
    shape ``dir_holders.py`` reports for a pinned worktree). Whole-element
    match on purpose: a shell whose command line merely mentions the path (a
    grep, a ``python -c`` one-liner) is not a stub.
    """
    paths = [Path(arg) for arg in cmdline]
    if not any(p.name == STUB_SCRIPT_NAME for p in paths):
        return False
    return any(p.is_relative_to(scope) for p in paths)


def stub_children(scope: Path) -> List[psutil.Process]:
    """Live stub children whose script or interpreter lives under ``scope``."""
    found = []
    for proc in psutil.process_iter(["cmdline"]):
        try:
            if _is_stub_under(proc.info["cmdline"] or [], scope):
                found.append(proc)
        except (ValueError, OSError):  # pragma: no cover - odd argv element
            continue
    return found


def reap_stub_children(scope: Path) -> List[str]:
    """Kill every stub child under ``scope``; return a description of each.

    Empty when there was nothing to reap. Raises ``RuntimeError`` when one
    survives the kill, since that one keeps the checkout pinned.
    """
    found = stub_children(scope)
    described = [_describe(p) for p in found]
    alive = _kill_all(found)
    if alive:
        raise RuntimeError(
            f"could not reap {len(alive)} e2e stub child(ren) under {scope}: "
            + ", ".join(_describe(p) for p in alive)
        )
    return described


def _describe(proc: psutil.Process) -> str:
    try:
        return f"pid {proc.pid} {proc.name()}"
    except psutil.Error:
        return f"pid {proc.pid}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m tests.e2e._process_teardown <dir>", file=sys.stderr)
        return 2
    scope = Path(args[0]).resolve()
    try:
        reaped = reap_stub_children(scope)
    except RuntimeError as exc:
        print(f"STUBS=stuck {exc}")
        return 1
    print(f"STUBS=reaped:{len(reaped)}" + "".join(f"\n  {r}" for r in reaped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
