"""Replace an installed app with a fresh clone at the same path, keeping the old folder as a backup.

Vendored verbatim (manifest key ``installer_inplace``), beside ``src/installer.py`` and
``src/installer_migrate.py`` (schema: project-scaffolding ``docs/installer.md``).
``setup.bat -InPlace`` copies this file, ``installer_migrate.py`` and ``installer_declaration.py`` to a
temporary folder and runs this one from there with the system python, because Windows refuses
to rename a folder that a running process, or a process's current folder, is inside.

One run, repeatable any number of times:

1. stop every process that runs from the folder (by PID, never by name), and refuse
   when this run was itself started from one of them;
2. rename the folder to ``<folder>.bak-YYYYMMDD-N`` (the first free N), a working checkout;
3. clone the folder's ``origin`` into the original path;
4. run the new clone's ``setup.bat -MigrateFrom <backup>``: copy, rebind, secrets,
   install, start, verify.

The path does not change, so whatever stores it (a launcher's app list, a Startup
link) needs no edit. A failed clone renames the backup back when it can.
``-WhatIf`` shows the backup name, every step and every file the migration would
copy (by name, from this folder's declaration), and changes nothing.
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
        raise SystemExit(f"setup.bat -InPlace: {path.name} is missing beside {Path(__file__).name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


m = sys.modules.get("installer_migrate") or _load_migrate()

Runner = Callable[..., "subprocess.CompletedProcess[Any]"]


def backup_path(folder: Path, today: datetime.date) -> Path:
    """``<folder>.bak-YYYYMMDD-N``, the first N from 1 that is free."""
    n = 1
    while True:
        candidate = folder.with_name(f"{folder.name}.bak-{today:%Y%m%d}-{n}")
        if not os.path.lexists(candidate):
            return candidate
        n += 1


def _declaration(folder: Path) -> tuple[Any, str]:
    """The folder's own migration declaration, or (None, why): the fresh clone reads its own, normally the same."""
    try:
        return m.parse_migration(m.read_fleet_toml(folder)), ""
    except ValueError as exc:
        return None, str(exc)


def _port(folder: Path, mig: Any) -> int | None:
    """The port the folder's app serves on this PC (its config first), to wait on after the stop."""
    return m.local_port(folder, mig.endpoint)[0] if mig is not None and mig.endpoint else None


def _preview_copy(folder: Path, mig: Any, why: str, report: Any) -> None:
    """Every file the migration would copy from the backup into the empty clone, by name, never by value.

    No rebind is listed: the configs name the original path, which is where the clone lands.
    """
    if mig is None:
        report.add("copy", m.UNKNOWN, ".fleet.toml", f"the copy list cannot be shown: {why}")
        return
    m.copy_declared(folder, None, mig, report, old_running=False)  # the stop runs before the copy


def guard_in_place(folder: Path, report: Any, runner: Runner) -> str | None:
    """The origin URL to clone again, or None when the folder cannot be replaced in place."""
    if m.inside(sys.executable, folder):
        report.add("guard", "refused", "python", "this run's python lives in the folder it would rename; "
                   "run setup.bat -InPlace, which uses the system python")
        return None
    top = m.git(["rev-parse", "--show-toplevel"], folder, runner)
    if top is None or not m.inside(top, folder) or not m.inside(str(folder), Path(top)):
        report.add("guard", "refused", "folder", "not the root of a git checkout")
        return None
    url = m.git(["remote", "get-url", "origin"], folder, runner)
    if not url:
        report.add("guard", "refused", "folder", "has no origin remote to clone again")
        return None
    report.add("guard", m.OK, "folder", str(folder))
    branch = m.git(["branch", "--show-current"], folder, runner)
    report.add("guard", m.OK, "branch", f"{branch or 'detached'}; the clone takes the remote's default branch")
    changes = m.git(["status", "--porcelain", "--untracked-files=no"], folder, runner)
    if changes:
        report.add("guard", "kept", "uncommitted changes",
                   f"{len(changes.splitlines())} tracked file(s), kept in the backup")
    return str(url)


def _run_child(folder: Path, backup: Path, as_json: bool, runner: Runner) -> tuple[int, Any]:
    """The new clone's ``setup.bat -MigrateFrom <backup>``; with ``as_json``, its parsed report."""
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


def _clone(url: str, folder: Path, backup: Path, report: Any, runner: Runner) -> bool:
    try:
        # conventions-exempt: a vendor-verbatim file cannot import the adopter's run_git; one clone
        done = runner(["git", "clone", "--", url, str(folder)], cwd=folder.parent, timeout=m.CLONE_TIMEOUT_S,
                      check=False, stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr)
        code = done.returncode
    except (OSError, subprocess.TimeoutExpired):
        code = -1
    if code == 0:
        report.add("in place", "cloned", url, f"into {folder}")
        return True
    if not os.path.lexists(folder):
        os.rename(backup, folder)
        report.add("in place", m.FAILED, "clone", f"git clone exited {code}; the backup was renamed back, "
                   "nothing changed")
    else:
        report.add("in place", m.FAILED, "clone", f"git clone exited {code} and left {folder.name}; the old "
                   f"copy is untouched at {backup.name}: rename the partial clone away, then {backup.name} back")
    return False


def in_place(folder: Path, *, what_if: bool, as_json: bool, runner: Runner = subprocess.run,
             snapshot_fn: Callable[[], Any] | None = None, today: datetime.date | None = None) -> Any:
    """Stop, rename to a backup, clone into the same path and migrate from the backup."""
    report = m.MigrationReport(mode="in place", what_if=what_if)
    snapshot_fn = snapshot_fn or (lambda: m.take_snapshot(runner))
    folder = folder.resolve()
    os.chdir(folder.parent)  # a process whose current folder is inside it blocks the rename
    url = guard_in_place(folder, report, runner)
    if url is None:
        return report
    m.inventory(folder, report, runner, snapshot_fn())
    mig, why = _declaration(folder)
    m.stop_old(folder, _port(folder, mig), report, runner, snapshot_fn)
    if report.failed:
        return report
    backup = backup_path(folder, today or datetime.date.today())
    if what_if:
        _preview_copy(folder, mig, why, report)
        report.add("in place", "would rename", folder.name, f"to {backup.name}, kept as a working checkout")
        report.add("in place", "would clone", url, f"into {folder}")
        report.add("in place", "would run", "setup.bat -MigrateFrom", f"{backup.name}: copy (as listed, from "
                   "this folder's [install.migrate]; the clone's own is the one used), secrets, install, start, "
                   "verify")
        return report
    try:
        os.rename(folder, backup)
    except OSError as exc:
        report.add("in place", m.FAILED, "rename", f"{type(exc).__name__}: a terminal, editor or process still "
                   "has a file or its current folder inside it; close it and run again, nothing was changed")
        return report
    report.add("in place", "renamed", folder.name, f"to {backup.name}")
    if not _clone(url, folder, backup, report, runner):
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


def main(argv: list[str] | None = None) -> int:
    """``setup.bat -InPlace``, run from a temporary copy of this file."""
    parser = argparse.ArgumentParser(prog="setup.bat -InPlace", description="Replace this install with a fresh "
                                     "clone at the same path, keeping the old folder as a backup.")
    parser.add_argument("-InPlace", "--in-place", dest="in_place", action="store_true", required=True)
    parser.add_argument("-Folder", "--folder", dest="folder", required=True, help="the install to replace")
    parser.add_argument("-WhatIf", "--what-if", dest="what_if", action="store_true",
                        help="list every step and change nothing")
    parser.add_argument("-Json", "--json", dest="as_json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    report = in_place(Path(args.folder), what_if=args.what_if, as_json=args.as_json)
    if report.handed_off:
        state, item, detail = report.rows["in place"][-1]
        sys.stdout.write(f"\nin place: [{state}] {item}  - {detail}\n")
        return 1 if report.failed else 0
    return int(m.emit(report, args.as_json))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
