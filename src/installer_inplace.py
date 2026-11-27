"""Replace an installed app with a fresh clone at the same path, keeping the old folder as a backup.

Vendored verbatim (manifest key ``installer_inplace``), beside ``src/installer.py`` and
``src/installer_migrate.py`` (schema: project-scaffolding ``docs/installer.md``).
``setup.bat`` copies this file, ``installer_migrate.py`` and ``installer_declaration.py`` to a
temporary folder and runs this one from there with the system python, because Windows refuses
to rename a folder that a running process, or a process's current folder, is inside.

Two entries, one swap:

* ``<app>.incoming\\setup.bat -Replace <app>`` runs from a fresh clone beside the install, so
  it works whatever version the install is: none of the old copy's code runs;
* ``<app>\\setup.bat -InPlace`` first clones the folder's ``origin`` into ``<app>.incoming``
  itself, so it needs an install new enough to have it.

The swap, repeatable any number of times:

1. stop every process that runs from the install (by PID, never by name), and refuse
   when this run was itself started from one of them;
2. rename the install to ``<folder>.bak-YYYYMMDD-N`` (the first free N), a working checkout
   that keeps any uncommitted edits;
3. rename the incoming clone to the install's path;
4. run the moved clone's ``setup.bat -MigrateFrom <backup>``: copy, rebind, secrets,
   install, start, verify.

The path does not change, so whatever stores it (a launcher's app list, a Startup
link) needs no edit. A failure before step 2 leaves the install in its folder (start it
again with its own start command); a failure at step 3 renames the backup back.
``-WhatIf`` shows the backup name, every step and every file the migration would
copy (by name), and changes nothing.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_migrate() -> ModuleType:
    """``installer_migrate.py`` from this file's folder, by path: ``-I`` keeps the folder off ``sys.path``."""
    path = Path(__file__).with_name("installer_migrate.py")
    spec = importlib.util.spec_from_file_location("installer_migrate", path)
    if spec is None or spec.loader is None or not path.is_file():
        raise SystemExit(f"setup.bat -InPlace/-Replace: {path.name} is missing beside {Path(__file__).name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


m = sys.modules.get("installer_migrate") or _load_migrate()

Runner = Callable[..., "subprocess.CompletedProcess[Any]"]
INCOMING_SUFFIX = ".incoming"


def backup_path(folder: Path, today: datetime.date) -> Path:
    """``<folder>.bak-YYYYMMDD-N``, the first N from 1 that is free."""
    n = 1
    while True:
        candidate = folder.with_name(f"{folder.name}.bak-{today:%Y%m%d}-{n}")
        if not os.path.lexists(candidate):
            return candidate
        n += 1


def incoming_path(folder: Path) -> Path:
    """Where ``-InPlace`` clones, and the name the playbook gives the clone ``-Replace`` runs from."""
    return folder.with_name(folder.name + INCOMING_SUFFIX)


def same_origin(a: str, b: str) -> bool:
    """Whether two remote URLs name one repository: case, slash style, a trailing slash and ``.git`` ignored."""
    def norm(url: str) -> str:
        url = url.strip().replace("\\", "/").rstrip("/").casefold()
        return url[:-4] if url.endswith(".git") else url
    return norm(a) == norm(b)


def _declaration(folder: Path) -> tuple[Any, str]:
    """A folder's own migration declaration, or (None, why)."""
    try:
        return m.parse_migration(m.read_fleet_toml(folder)), ""
    except ValueError as exc:
        return None, str(exc)


def _port(folder: Path, mig: Any) -> int | None:
    """The port the install serves on this PC (its config first), to wait on after the stop."""
    return m.local_port(folder, mig.endpoint)[0] if mig is not None and mig.endpoint else None


def _preview_copy(folder: Path, mig: Any, why: str, report: Any) -> None:
    """Every file the migration would copy from the backup into the empty clone, by name, never by value.

    No rebind is listed: the configs name the original path, which is where the clone lands.
    """
    if mig is None:
        report.add("copy", m.UNKNOWN, ".fleet.toml", f"the copy list cannot be shown: {why}")
        return
    m.copy_declared(folder, None, mig, report, old_running=False)  # the stop runs before the copy


def _python_outside(folders: list[Path], report: Any) -> bool:
    for folder in folders:
        if m.inside(sys.executable, folder):
            report.add("guard", "refused", "python", f"this run's python lives in {folder.name}, which it would "
                       "rename; run setup.bat, which uses the system python")
            return False
    return True


def guard_in_place(folder: Path, report: Any, runner: Runner) -> str | None:
    """The install's origin URL, or None when the folder cannot be replaced at its path."""
    if not _python_outside([folder], report):
        return None
    top = m.git(["rev-parse", "--show-toplevel"], folder, runner)
    if top is None or not m.inside(top, folder) or not m.inside(str(folder), Path(top)):
        report.add("guard", "refused", "folder", "not the root of a git checkout")
        return None
    url = m.git(["remote", "get-url", "origin"], folder, runner)
    if not url:
        report.add("guard", "refused", "folder", "has no origin remote, so the clone cannot be shown to be "
                   "the same app")
        return None
    report.add("guard", m.OK, "folder", str(folder))
    branch = m.git(["branch", "--show-current"], folder, runner)
    report.add("guard", m.OK, "branch", f"{branch or 'detached'}; the clone takes the remote's default branch")
    changes = m.git(["status", "--porcelain", "--untracked-files=no"], folder, runner)
    if changes:
        report.add("guard", "kept", "uncommitted changes",
                   f"{len(changes.splitlines())} tracked file(s), kept in the backup")
    return str(url)


def guard_incoming(incoming: Path, folder: Path, url: str, report: Any, runner: Runner, snap: Any) -> bool:
    """Whether ``incoming`` is a fresh, unused clone of the install's own repository, beside it."""
    if not _python_outside([incoming], report):
        return False
    if m.inside(str(incoming), folder) or m.inside(str(folder), incoming):
        report.add("guard", "refused", "incoming clone", "is the install, or inside or around it")
        return False
    top = m.git(["rev-parse", "--show-toplevel"], incoming, runner)
    if top is None or not m.inside(top, incoming) or not m.inside(str(incoming), Path(top)):
        report.add("guard", "refused", "incoming clone", "not the root of a git checkout")
        return False
    status = m.git(["status", "--porcelain"], incoming, runner)
    if status is None:
        report.add("guard", m.UNKNOWN, "incoming clone", "git status did not run")
        return False
    if status:
        report.add("guard", "refused", "incoming clone", "not a fresh clone: it has changed or untracked files")
        return False
    if os.path.lexists(incoming / ".venv"):
        report.add("guard", "refused", "incoming clone", "already has a .venv, which names its current path; "
                   "clone it again and run nothing in it before -Replace")
        return False
    theirs = m.git(["remote", "get-url", "origin"], incoming, runner)
    if not theirs or not same_origin(theirs, url):
        report.add("guard", "refused", "incoming clone",
                   ("its origin is not the install's, so it is not the same app") if theirs
                   else "has no origin remote, so it cannot be shown to be the same app")
        return False
    if snap is None:
        report.add("guard", m.UNKNOWN, "incoming clone", "could not list processes to show nothing runs from it")
        return False
    running = m.processes_from(snap, incoming, m.ancestors(snap, os.getpid()))
    if running:
        report.add("guard", "refused", "incoming clone", "something runs from it (PID "
                   + ", ".join(str(p.pid) for p in running) + "), so it cannot be renamed")
        return False
    report.add("guard", m.OK, "incoming clone", str(incoming))
    return True


def _first_declaration(folders: list[Path]) -> tuple[Any, str]:
    """The first folder's declaration that parses, else (None, the first one's reason)."""
    first = ""
    for folder in folders:
        mig, why = _declaration(folder)
        if mig is not None:
            return mig, ""
        first = first or why
    return None, first


def _start_hint(folder: Path, mig: Any) -> str:
    start = " ".join(mig.start) if mig is not None else " ".join(m.DEFAULT_START)
    return f"start it again from {folder} with {start}"


def _run_child(folder: Path, backup: Path, as_json: bool, runner: Runner) -> tuple[int, Any]:
    """The moved clone's ``setup.bat -MigrateFrom <backup>``; with ``as_json``, its parsed report."""
    line = f'cmd.exe /d /s /c ""{folder / "setup.bat"}" -MigrateFrom "{backup}"{" -Json" if as_json else ""}"'
    try:
        done = runner(line, cwd=folder, timeout=m.CLONE_TIMEOUT_S, check=False, stdin=subprocess.DEVNULL,
                      stdout=subprocess.PIPE if as_json else sys.stdout, stderr=sys.stderr,
                      encoding="utf-8" if as_json else None, errors="replace" if as_json else None)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, {"error": f"setup.bat -MigrateFrom did not run ({type(exc).__name__})"}
    if not as_json:
        return done.returncode, None
    try:
        return done.returncode, json.loads(done.stdout)
    except ValueError:
        return done.returncode, {"error": "setup.bat -MigrateFrom printed no JSON report"}


def _clone(url: str, incoming: Path, report: Any, runner: Runner) -> bool:
    """``-InPlace``: clone beside the install before anything is stopped, so a failed clone changes nothing."""
    try:
        # conventions-exempt: a vendor-verbatim file cannot import the adopter's run_git; one clone
        done = runner(["git", "clone", "--", url, str(incoming)], cwd=incoming.parent, timeout=m.CLONE_TIMEOUT_S,
                      check=False, stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr)
        code = done.returncode
    except (OSError, subprocess.TimeoutExpired):
        code = -1
    if code == 0:
        report.add("in place", "cloned", url, f"into {incoming}")
        return True
    report.add("in place", m.FAILED, "clone", f"git clone exited {code}; the install was not touched"
               + (f"; remove the partial {incoming.name} before running again" if os.path.lexists(incoming) else ""))
    return False


def _swap(folder: Path, incoming: Path, backup: Path, mig: Any, report: Any) -> bool:
    """Rename the install to the backup and the clone into its path; on a failure, leave the install at its path."""
    try:
        os.rename(folder, backup)
    except OSError as exc:
        report.add("in place", m.FAILED, "rename", f"{type(exc).__name__}: a terminal, editor or process still has "
                   f"a file or its current folder inside {folder.name}; nothing was renamed, {_start_hint(folder, mig)}"
                   ", close what holds it and run again")
        return False
    report.add("in place", "renamed", folder.name, f"to {backup.name}")
    try:
        os.rename(incoming, folder)
    except OSError as exc:
        why = f"{type(exc).__name__} renaming {incoming.name} to {folder.name}"
        try:
            os.rename(backup, folder)
        except OSError as back:
            report.add("in place", m.FAILED, "rename", f"{why}, and {type(back).__name__} renaming {backup.name} "
                       f"back: rename {backup.name} to {folder.name} by hand, then {_start_hint(folder, mig)}")
            return False
        report.add("in place", m.FAILED, "rename", f"{why}; {backup.name} was renamed back, "
                   f"{_start_hint(folder, mig)}; {incoming.name} is unchanged")
        return False
    report.add("in place", "renamed", incoming.name, f"to {folder.name}")
    return True


def replace(folder: Path, incoming: Path | None, *, what_if: bool, as_json: bool, runner: Runner = subprocess.run,
            snapshot_fn: Callable[[], Any] | None = None, today: datetime.date | None = None) -> Any:
    """Swap the install at ``folder`` for the clone at ``incoming`` and migrate from the backup.

    ``incoming`` None is ``-InPlace``: the clone is made here, at ``<folder>.incoming``.
    """
    report = m.MigrationReport(mode="in place" if incoming is None else "replace", what_if=what_if)
    snapshot_fn = snapshot_fn or (lambda: m.take_snapshot(runner))
    folder = folder.resolve()
    os.chdir(folder.parent)  # a process whose current folder is inside it blocks the rename
    if not folder.is_dir():
        report.add("guard", "refused", "folder", f"{folder} does not exist")
        return report
    url = guard_in_place(folder, report, runner)
    if url is None:
        return report
    snap = snapshot_fn()
    cloning = incoming is None
    incoming = (incoming or incoming_path(folder)).resolve()
    if cloning and os.path.lexists(incoming):
        report.add("guard", "refused", "incoming clone", f"{incoming.name} already exists: run {incoming.name}"
                   f"\\setup.bat -Replace {folder.name} if it is a fresh clone, or remove it")
        return report
    if cloning and what_if:
        report.add("in place", "would clone", url, f"into {incoming}")
    elif cloning and not _clone(url, incoming, report, runner):
        return report
    if not (cloning and what_if) and not guard_incoming(incoming, folder, url, report, runner, snap):
        return report
    m.inventory(folder, report, runner, snap)
    # The clone's declaration is the one -MigrateFrom uses; an install too old to declare one still has a port.
    mig, why = _first_declaration([incoming, folder] if incoming.is_dir() else [folder])
    m.stop_old(folder, _port(folder, mig), report, runner, snapshot_fn)
    if report.failed:
        if cloning and not what_if:
            report.add("in place", "kept", incoming.name, f"a fresh clone: run {incoming.name}\\setup.bat -Replace "
                       f"{folder.name} to try again, or remove it")
        return report
    backup = backup_path(folder, today or datetime.date.today())
    if what_if:
        _preview_copy(folder, mig, why, report)
        report.add("in place", "would rename", folder.name, f"to {backup.name}, kept as a working checkout")
        report.add("in place", "would rename", incoming.name, f"to {folder.name}")
        report.add("in place", "would run", "setup.bat -MigrateFrom", f"{backup.name}: copy (as listed), secrets, "
                   "install, start, verify")
        return report
    if not _swap(folder, incoming, backup, mig, report):
        return report
    if not as_json:
        sys.stdout.write(report.render(final=False) + "\n\nMigrating from the backup:\n")
        sys.stdout.flush()
        report.handed_off = True
    code, child = _run_child(folder, backup, as_json, runner)
    report.migration = child
    report.add("in place", m.OK if code == 0 else m.FAILED, "setup.bat -MigrateFrom",
               f"exited {code}; the backup {backup.name} stays as the fallback")
    return report


def in_place(folder: Path, *, what_if: bool, as_json: bool, runner: Runner = subprocess.run,
             snapshot_fn: Callable[[], Any] | None = None, today: datetime.date | None = None) -> Any:
    """``-InPlace``: clone the install's origin beside it, then swap and migrate."""
    return replace(folder, None, what_if=what_if, as_json=as_json, runner=runner, snapshot_fn=snapshot_fn,
                   today=today)


def main(argv: list[str] | None = None) -> int:
    """``setup.bat -InPlace`` or ``setup.bat -Replace``, run from a temporary copy of this file."""
    parser = argparse.ArgumentParser(prog="setup.bat", description="Replace an install with a fresh clone at the "
                                     "same path, keeping the old folder as a backup.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("-InPlace", "--in-place", dest="in_place", action="store_true",
                      help="clone this install's origin beside it, then replace it")
    mode.add_argument("-Replace", "--replace", dest="replace", metavar="INSTALL",
                      help="replace INSTALL with the clone this setup.bat is in (setup.bat resolves it to -Folder)")
    parser.add_argument("-Folder", "--folder", dest="folder", required=True, help="the install to replace")
    parser.add_argument("-Incoming", "--incoming", dest="incoming", help="with -Replace: the clone to move in")
    parser.add_argument("-WhatIf", "--what-if", dest="what_if", action="store_true",
                        help="list every step and change nothing")
    parser.add_argument("-Json", "--json", dest="as_json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    if bool(args.replace) != bool(args.incoming):
        parser.error("-Incoming goes with -Replace, and -Replace needs it")
    incoming = Path(args.incoming) if args.incoming else None
    report = replace(Path(args.folder), incoming, what_if=args.what_if, as_json=args.as_json)
    if report.handed_off:
        state, item, detail = report.rows["in place"][-1]
        sys.stdout.write(f"\nin place: [{state}] {item}  - {detail}\n")
        return 1 if report.failed else 0
    return int(m.emit(report, args.as_json))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
