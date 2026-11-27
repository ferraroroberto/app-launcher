"""Move an installed app into a fresh clone, then prove the clone is the copy that serves.

Vendored verbatim (manifest key ``installer_migrate``), beside ``src/installer.py`` and
``src/installer_declaration.py`` (the ``.fleet.toml`` declaration, loaded by path).
Two entries, both stdlib only (schema: project-scaffolding ``docs/installer.md``):

* ``setup.bat -MigrateFrom <old folder>`` runs in a fresh clone. ``installer.py``
  loads this file from its own folder and calls ``main_migrate``. It reads the old
  copy (folder, remote, processes, ports, autostart entries), optionally stops the
  processes that run from it (``-StopOld``), copies only the files the clone's
  ``[install.migrate]`` declares (never over a file that is already here), rebinds
  old-folder paths in the copied JSON configs, compares the declared secrets by
  hash, runs the normal install, starts the app and verifies it.
* ``setup.bat -InPlace`` (``installer_inplace.py``, which imports this file) replaces
  an install at the same path: stop, rename to a backup, clone, then ``-MigrateFrom``
  the backup.

Verify passes only when the endpoint serves this clone's ``HEAD`` and the process
listening on the port runs from this clone, so an old copy still serving the port
fails it. The port is the one this PC's config sets (``port_config``), the declared
one as fallback, and the scheme is whichever of http and https answers. A fact a
probe cannot establish is ``unknown`` and fails the run. No config or secret value
is ever printed: files and keys are named, values never. ``-WhatIf`` reports every
step and every file it would copy, and changes nothing; ``-Json`` prints the report as JSON.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import importlib.util
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

# A vendor-verbatim file keeps its own copy of the no-console flag: it cannot import the adopter's tree.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

OK, MISSING, UNKNOWN, FAILED = "ok", "missing", "unknown", "failed"
SECTIONS = ("guard", "inventory", "stop", "copy", "rebind", "secrets", "shared", "install", "start", "verify",
            "leftover", "in place")
# Rows in these states fail the run: a step that did not happen, or a fact nobody established.
FAILING_STATES = (FAILED, UNKNOWN, "refused")

# Listing processes, ports and autostart entries takes a few seconds; scheduled tasks are the slow part.
SNAPSHOT_TIMEOUT_S = 120
GIT_TIMEOUT_S = 60
CLONE_TIMEOUT_S = 1800
# A stopped tray's watchdog may respawn its child once before the tree is gone, so stopping repeats.
STOP_ROUNDS = 3
STOP_SETTLE_S = 2.0
PORT_FREE_TIMEOUT_S = 30
# A tray restart starts the webapp in the background; a cold start on a slow PC takes most of a minute.
START_TIMEOUT_S = 300
VERIFY_TIMEOUT_S = 120
FETCH_TIMEOUT_S = 5
POLL_S = 2.0
# Abbreviated commit ids shorter than git's default are not compared.
MIN_SHA_LEN = 7
TEXT_SCAN_MAX_BYTES = 1_000_000

PYTHON_NAMES = ("python.exe", "pythonw.exe", "python", "pythonw", "py.exe")
DB_SUFFIXES = (".sqlite", ".sqlite3", ".db")
# Live SQLite sidecars: copying one beside a database another process writes corrupts the copy.
DB_SIDECARS = ("-wal", "-shm", "-journal")
START_LOG = "migrate-start.log"
DATA_ROOT_ENV = "FLEET_DATA_ROOT"
# no-drive-path-exempt: the fleet-wide runtime-data root is one fixed, documented location (src/runtime_data.py)
WINDOWS_DATA_ROOT = Path("C:/sqlite")
# A path in free text ends at a separator, a quote, a space or the end; `app` must not match `app.bak-1`.
PATH_END = r"(?=$|[\\/\"'\s;,])"

Runner = Callable[..., "subprocess.CompletedProcess[Any]"]


# ---------------------------------------------------------------- the declaration

def _load_declaration() -> ModuleType:
    """``installer_declaration.py`` from this file's folder, by path: ``-I`` keeps the folder off ``sys.path``."""
    path = Path(__file__).with_name("installer_declaration.py")
    spec = importlib.util.spec_from_file_location("installer_declaration", path)
    if spec is None or spec.loader is None or not path.is_file():
        raise SystemExit(f"setup.bat: {path.name} is missing beside {Path(__file__).name}; vendor it to migrate")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


if TYPE_CHECKING:
    from src.installer_declaration import (
        DEFAULT_START,
        DEFAULT_VERSION_PATH,
        Endpoint,
        Migration,
        local_port,
        parse_endpoint,
        parse_migration,
        read_fleet_toml,
    )
else:
    _d = sys.modules.get("installer_declaration") or _load_declaration()
    Endpoint, Migration, parse_endpoint, parse_migration = (_d.Endpoint, _d.Migration, _d.parse_endpoint,
                                                            _d.parse_migration)
    local_port, read_fleet_toml = _d.local_port, _d.read_fleet_toml
    DEFAULT_START, DEFAULT_VERSION_PATH = _d.DEFAULT_START, _d.DEFAULT_VERSION_PATH


# ---------------------------------------------------------------- the report

@dataclass
class MigrationReport:
    """Rows per section, each (state, item, detail); a failing row fails the run."""

    mode: str
    what_if: bool
    rows: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)
    install: dict[str, Any] | None = None
    install_text: str = ""
    migration: Any = None  # the -MigrateFrom child's JSON report, in place mode
    handed_off: bool = False  # in place: this report was printed before the child printed its own

    def add(self, section: str, state: str, item: str, detail: str = "") -> None:
        self.rows.setdefault(section, []).append((state, item, detail))

    @property
    def failed(self) -> bool:
        """Derived, never stored: a failing row, or an install pass that did not finish."""
        install_failed = bool(self.install and self.install.get("failed"))
        return install_failed or any(row[0] in FAILING_STATES for rows in self.rows.values() for row in rows)

    def to_json(self) -> dict[str, Any]:
        return {"mode": self.mode, "what_if": self.what_if, "ok": not self.failed,
                "sections": {name: [{"state": s, "item": i, "detail": d} for s, i, d in rows]
                             for name, rows in self.rows.items()},
                "install": self.install, "migration": self.migration}

    def render(self, final: bool = True) -> str:
        out = ["What-if: nothing below is changed." if self.what_if else f"{self.mode.capitalize()}:"]
        for name in SECTIONS:
            if name == "install" and self.install_text:
                out += ["", "install:", *("  " + line for line in self.install_text.splitlines())]
            if name in self.rows:
                out += ["", f"{name}:"]
                out += [f"  [{s:<11}] {i}" + (f"  - {d}" if d else "") for s, i, d in self.rows[name]]
        if final and self.failed:
            out += ["", "Migration not verified: fix what is reported above, then run it again."]
        elif final and not self.what_if:
            out += ["", "Migration verified."]
        return "\n".join(out)


# ---------------------------------------------------------------- paths

def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def inside(path: str, folder: Path) -> bool:
    """Whether ``path`` is ``folder`` or below it, by path component (``app`` never contains ``app.bak-1``)."""
    if not path:
        return False
    target, base = _norm(path), _norm(str(folder)).rstrip("\\/")
    return target == base or target.startswith(base + os.sep)


def _spellings(folder: Path) -> list[str]:
    text = str(folder)
    return list(dict.fromkeys([text, text.replace("\\", "/")]))


def mentions(text: str, folder: Path) -> bool:
    """Whether free text (a command line, a shortcut, a script) names ``folder`` or a path below it."""
    return any(re.search(re.escape(s) + PATH_END, text or "", re.IGNORECASE) for s in _spellings(folder))


def rebind_text(value: str, old: Path, new: Path) -> str | None:
    """``value`` with a leading ``old`` folder replaced by ``new`` (in the slash style it used), else None."""
    for spelling in _spellings(old):
        match = re.match(re.escape(spelling) + PATH_END, value, re.IGNORECASE)
        if match:
            base = str(new).replace("\\", "/") if "/" in spelling and "\\" not in spelling else str(new)
            return base + value[match.end():]
    return None


def rebind_values(data: Any, old: Path, new: Path, prefix: str = "") -> list[str]:
    """Rebind every string value in a JSON tree in place; returns the dotted names of the keys changed."""
    changed: list[str] = []
    items = data.items() if isinstance(data, dict) else enumerate(data) if isinstance(data, list) else []
    for key, value in list(items):
        name = (f"{prefix}.{key}" if prefix else str(key)) if isinstance(data, dict) else f"{prefix}[{key}]"
        if isinstance(value, str):
            rebound = rebind_text(value, old, new)
            if rebound is not None:
                data[key] = rebound
                changed.append(name)
        elif isinstance(value, (dict, list)):
            changed += rebind_values(value, old, new, name)
    return changed


def data_root() -> Path:
    """The fleet runtime-data root, resolved as ``src/runtime_data.py`` resolves it."""
    override = os.environ.get(DATA_ROOT_ENV, "").strip()
    if override:
        return Path(override)
    if sys.platform == "win32":
        return WINDOWS_DATA_ROOT
    return Path(os.environ.get("XDG_DATA_HOME", "").strip() or Path.home() / ".local" / "share") / "sqlite"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- git, PowerShell, processes

def git(args: list[str], cwd: Path, runner: Runner) -> str | None:
    """One read-only git query's trimmed output; None when git is missing or the query fails."""
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        # conventions-exempt: a vendor-verbatim file cannot import the adopter's run_git; read-only queries
        done = runner(["git", *args], cwd=cwd, env=env, timeout=GIT_TIMEOUT_S, check=False, capture_output=True,
                      encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (done.stdout or "").strip() if done.returncode == 0 else None


def powershell_exe() -> Path | None:
    """The absolute Windows PowerShell, never a ``pwsh`` stub found on PATH."""
    system_root = os.environ.get("SystemRoot") or os.environ.get("windir")
    return Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe" if system_root else None


def run_powershell(script: str, runner: Runner) -> Any | None:
    """Run a script that prints one JSON document; None when it cannot run or its output does not parse."""
    exe = powershell_exe()
    if exe is None:
        return None
    script = "$ErrorActionPreference = 'Stop'\n[Console]::OutputEncoding = [Text.Encoding]::UTF8\n" + script
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        done = runner([str(exe), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                      timeout=SNAPSHOT_TIMEOUT_S, check=False, capture_output=True, encoding="utf-8",
                      errors="replace", creationflags=NO_WINDOW)
        return json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


PS_PROCESSES = r"""
$procs = @(Get-CimInstance Win32_Process | ForEach-Object {
  @{ pid = [int]$_.ProcessId; ppid = [int]$_.ParentProcessId; exe = [string]$_.ExecutablePath; cmd = [string]$_.CommandLine } })
$ports = @(Get-NetTCPConnection -State Listen | ForEach-Object { @{ port = [int]$_.LocalPort; pid = [int]$_.OwningProcess } })
ConvertTo-Json -InputObject @{ procs = $procs; ports = $ports } -Depth 4 -Compress
"""

PS_AUTOSTART = r"""
$out = @()
$shell = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath('Startup'), [Environment]::GetFolderPath('CommonStartup'))) {
  if ($dir -and (Test-Path -LiteralPath $dir)) {
    Get-ChildItem -LiteralPath $dir -File | ForEach-Object {
      if ($_.Extension -eq '.lnk') {
        $link = $shell.CreateShortcut($_.FullName)
        $out += @{ kind = 'startup link'; name = $_.Name; text = "$($link.TargetPath) $($link.Arguments) $($link.WorkingDirectory)" }
      } elseif ($_.Length -lt 65536) {
        $out += @{ kind = 'startup file'; name = $_.Name; text = [IO.File]::ReadAllText($_.FullName) }
      } } } }
$run = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -ErrorAction SilentlyContinue
if ($run) { $run.PSObject.Properties | Where-Object { $_.Name -notlike 'PS*' } | ForEach-Object {
  $out += @{ kind = 'run key'; name = $_.Name; text = [string]$_.Value } } }
Get-ScheduledTask | ForEach-Object { $task = $_; foreach ($action in $task.Actions) { if ($action.Execute) {
  $out += @{ kind = 'scheduled task'; name = "$($task.TaskPath)$($task.TaskName)"; text = "$($action.Execute) $($action.Arguments) $($action.WorkingDirectory)" } } } }
ConvertTo-Json -InputObject @($out) -Depth 3 -Compress
"""


@dataclass
class Proc:
    pid: int
    ppid: int
    exe: str
    cmdline: str


@dataclass
class Snapshot:
    """Every process and which ones listen on which TCP port, at one moment."""

    procs: dict[int, Proc]
    listeners: dict[int, set[int]]  # port -> listening PIDs


def take_snapshot(runner: Runner) -> Snapshot | None:
    """The live processes and listening ports; None when they cannot be listed."""
    data = run_powershell(PS_PROCESSES, runner)
    if not isinstance(data, dict):
        return None
    procs = {int(p["pid"]): Proc(int(p["pid"]), int(p["ppid"]), p.get("exe") or "", p.get("cmd") or "")
             for p in data.get("procs") or [] if isinstance(p, dict)}
    listeners: dict[int, set[int]] = {}
    for entry in data.get("ports") or []:
        if isinstance(entry, dict):
            listeners.setdefault(int(entry["port"]), set()).add(int(entry["pid"]))
    return Snapshot(procs, listeners)


def runs_from(proc: Proc, folder: Path) -> bool:
    """A program inside ``folder``, or a python whose command line names it (a venv python's base child)."""
    return inside(proc.exe, folder) or (os.path.basename(proc.exe).lower() in PYTHON_NAMES
                                        and mentions(proc.cmdline, folder))


def ancestors(snap: Snapshot, pid: int) -> set[int]:
    """``pid`` and every parent above it still alive."""
    seen: set[int] = set()
    while pid in snap.procs and pid not in seen:
        seen.add(pid)
        pid = snap.procs[pid].ppid
    return seen


def processes_from(snap: Snapshot, folder: Path, mine: set[int]) -> list[Proc]:
    """The processes that run from ``folder``, leaving out this run and its parents."""
    return [p for p in snap.procs.values() if p.pid not in mine and runs_from(p, folder)]


def _ports_of(snap: Snapshot, pids: set[int]) -> list[int]:
    return sorted(port for port, owners in snap.listeners.items() if owners & pids)


def owner_of(snap: Snapshot, pid: int, clone: Path, old: Path | None) -> tuple[str, Proc | None]:
    """Which copy a listening process belongs to: the nearest process up its parent chain that runs from one."""
    for ancestor in _chain(snap, pid):
        proc = snap.procs[ancestor]
        if old is not None and runs_from(proc, old):
            return "old", proc
        if runs_from(proc, clone):
            return "clone", proc
    return UNKNOWN, snap.procs.get(pid)


def _chain(snap: Snapshot, pid: int) -> list[int]:
    chain: list[int] = []
    while pid in snap.procs and pid not in chain:
        chain.append(pid)
        pid = snap.procs[pid].ppid
    return chain


# ---------------------------------------------------------------- inventory and stopping

def inventory(old: Path, report: MigrationReport, runner: Runner, snapshot: Snapshot | None) -> None:
    """The old copy as it is now: folder, remote, processes and ports, autostart entries. Read-only."""
    report.add("inventory", OK, "old folder", str(old))
    remote = git(["remote", "get-url", "origin"], old, runner)
    report.add("inventory", OK if remote else "none", "git remote", remote or "not a git checkout, or no origin")
    _report_processes(old, report, "inventory", snapshot, "running")
    _report_autostart(old, report, "inventory", runner)


def _report_processes(old: Path, report: MigrationReport, section: str, snap: Snapshot | None, state: str) -> None:
    if snap is None:
        report.add(section, UNKNOWN, "processes", "could not list processes and ports")
        return
    found = processes_from(snap, old, ancestors(snap, os.getpid()))
    if not found:
        report.add(section, "none", "processes", "nothing runs from the old folder")
    for proc in found:
        ports = _ports_of(snap, {proc.pid})
        report.add(section, state, f"PID {proc.pid} {os.path.basename(proc.exe) or '?'}",
                   "ports " + ", ".join(map(str, ports)) if ports else "no listening port")


def _report_autostart(old: Path, report: MigrationReport, section: str, runner: Runner) -> None:
    entries = run_powershell(PS_AUTOSTART, runner)
    if not isinstance(entries, list):
        report.add(section, UNKNOWN, "autostart", "could not list Startup links, Run keys and scheduled tasks")
        return
    hits = [e for e in entries if isinstance(e, dict) and mentions(str(e.get("text", "")), old)]
    if not hits:
        report.add(section, "none", "autostart", "no Startup link, Run key or scheduled task targets the old folder")
    for entry in hits:
        report.add(section, "points at old", f"{entry.get('kind')}: {entry.get('name')}")


def _kill_tree(pid: int, runner: Runner) -> None:
    try:
        runner(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=GIT_TIMEOUT_S, check=False,
               capture_output=True, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        pass  # the next snapshot shows whether it is gone


def stop_old(old: Path, port: int | None, report: MigrationReport, runner: Runner,
             snapshot_fn: Callable[[], Snapshot | None]) -> None:
    """Stop every process that runs from ``old`` (by PID, its tree with it), then wait for the app's port."""
    snap = snapshot_fn()
    if snap is None:
        report.add("stop", FAILED, "old copy", "could not list processes, so nothing was stopped")
        return
    me = os.getpid()
    mine = ancestors(snap, me)
    # Only a parent whose program lives in the folder is the app; one that names it on its command line
    # (the py launcher, the cmd.exe running setup.bat) is what started this run.
    parents = [p for p in snap.procs.values() if p.pid in mine and p.pid != me and inside(p.exe, old)]
    if parents:
        report.add("stop", "refused", "old copy", f"this run was started from it (PID {parents[0].pid}), so "
                   "stopping it ends this run too; run it from a plain terminal, not from a session the app hosts")
        return
    found = processes_from(snap, old, mine)
    if report.what_if:
        for proc in found:
            report.add("stop", "would stop", f"PID {proc.pid} {os.path.basename(proc.exe) or '?'}")
        if not found:
            report.add("stop", "none", "old copy", "nothing runs from the old folder")
        return
    stopped: set[int] = set()
    for _ in range(STOP_ROUNDS):
        if not found:
            break
        pids = {p.pid for p in found}
        for proc in found:
            if proc.ppid not in pids:
                _kill_tree(proc.pid, runner)
        stopped |= pids
        time.sleep(STOP_SETTLE_S)
        snap = snapshot_fn()
        if snap is None:
            report.add("stop", UNKNOWN, "old copy", "could not list processes after stopping")
            return
        found = processes_from(snap, old, mine)
    if found:
        report.add("stop", FAILED, "old copy", "still running: PID " + ", ".join(str(p.pid) for p in found))
        return
    report.add("stop", "stopped" if stopped else "none", "old copy",
               ("PID " + ", ".join(map(str, sorted(stopped)))) if stopped else "nothing runs from the old folder")
    if port is not None:
        _wait_port_free(port, report, snapshot_fn)


def _wait_port_free(port: int, report: MigrationReport, snapshot_fn: Callable[[], Snapshot | None]) -> None:
    deadline = time.monotonic() + PORT_FREE_TIMEOUT_S
    # request-budget-exempt: a one-shot setup poll of the local port table, capped by PORT_FREE_TIMEOUT_S
    while True:
        snap = snapshot_fn()
        if snap is None:
            report.add("stop", UNKNOWN, f"port {port}", "could not list listening ports")
            return
        holders = snap.listeners.get(port, set())
        if not holders:
            report.add("stop", OK, f"port {port}", "free")
            return
        if time.monotonic() >= deadline:
            names = ", ".join(f"PID {pid} {os.path.basename(snap.procs[pid].exe) if pid in snap.procs else '?'}"
                              for pid in sorted(holders))
            report.add("stop", FAILED, f"port {port}", f"still held by {names}")
            return
        time.sleep(POLL_S)


# ---------------------------------------------------------------- copy, rebind, secrets, shared

def _copy_file(src: Path, dst: Path | None, old_running: bool, what_if: bool) -> str:
    name = src.name.lower()
    if name.endswith(DB_SIDECARS):
        return "sidecar"
    if old_running and name.endswith(DB_SUFFIXES):
        return "held back"
    if dst is not None and dst.exists():
        return "kept"
    if what_if or dst is None:
        return "would copy"
    dst.parent.mkdir(parents=True, exist_ok=True)  # runtime-write-exempt: the clone's own ignored config folder
    shutil.copy2(src, dst)  # runtime-write-exempt: copies the owner's ignored config, state or certificate
    return "copied"


COPY_NOTES = {"kept": "already here, never overwritten", "sidecar": "SQLite -wal/-shm/-journal, never copied",
              "held back": "a database while the old copy runs; stop it (-StopOld) and run again"}


def copy_declared(old: Path, new: Path | None, mig: Migration, report: MigrationReport,
                  old_running: bool) -> list[str]:
    """Copy each declared file or folder that is not here yet; returns the repo-relative files (to be) copied.

    ``new`` None is a clone that does not exist yet (``-InPlace -WhatIf``): every file would be copied.
    Under ``-WhatIf`` each file a folder would copy gets its own row, by name.
    """
    copied: list[str] = []
    for item in mig.copy:
        src = old / item
        if not src.exists():
            report.add("copy", "absent", item, "not in the old copy")
            continue
        files = [src] if src.is_file() else sorted(p for p in src.rglob("*") if p.is_file())
        counts: Counter[str] = Counter()
        taken: list[str] = []
        for path in files:
            rel = path.relative_to(old).as_posix()
            state = _copy_file(path, None if new is None else new / rel, old_running, report.what_if)
            counts[state] += 1
            if state in ("copied", "would copy"):
                taken.append(rel)
        copied += taken
        if src.is_file():
            state = next(iter(counts))
            report.add("copy", state, item, COPY_NOTES.get(state, ""))
            continue
        report.add("copy", "copied" if counts["copied"] else "folder", item + "/",
                   ", ".join(f"{state} {n}" for state, n in sorted(counts.items())) or "empty")
        if report.what_if:
            for rel in taken:
                report.add("copy", "would copy", rel)
    return copied


def rebind_copied(old: Path, new: Path, copied: list[str], report: MigrationReport) -> None:
    """Rewrite old-folder paths in the copied JSON configs; name any other copied file that still names it."""
    for rel in copied:
        source = (old if report.what_if else new) / rel
        try:
            if source.stat().st_size > TEXT_SCAN_MAX_BYTES:
                continue
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not mentions(text, old):
            continue
        if not rel.endswith(".json"):
            report.add("rebind", "names old", rel, "not JSON, so not rebound; edit it by hand")
            continue
        try:
            data = json.loads(text)
        except ValueError:
            report.add("rebind", "left alone", rel, "does not parse")
            continue
        keys = rebind_values(data, old, new)
        if not keys:
            report.add("rebind", "names old", rel, "inside a longer value, so not rebound; edit it by hand")
            continue
        if not report.what_if:
            # runtime-write-exempt: the clone's own ignored config, rewritten only to point at the clone
            (new / rel).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        report.add("rebind", "would rebind" if report.what_if else "rebound", rel, "keys: " + ", ".join(keys))


def compare_secrets(old: Path, new: Path, mig: Migration, report: MigrationReport) -> None:
    """Each declared secret here against the old copy's, by hash; a value is never read out."""
    for item in mig.secrets:
        src, dst = old / item, new / item
        if not src.is_file():
            report.add("secrets", "absent", item, "not in the old copy")
        elif not dst.is_file():
            report.add("secrets", "would copy" if report.what_if else MISSING, item)
        else:
            same = _sha256(src) == _sha256(dst)
            report.add("secrets", "same" if same else "differs", item,
                       "" if same else "this clone already had its own; the old one was not copied over it")


def report_shared(mig: Migration, report: MigrationReport) -> None:
    for name in mig.shared:
        path = data_root() / name
        report.add("shared", "left in place", name, f"{path} {'exists' if path.is_dir() else 'does not exist yet'}")


# ---------------------------------------------------------------- start and verify

def _command_line(root: Path, start: list[str]) -> list[str] | str:
    """A ``.bat``/``.cmd`` runs through cmd.exe by absolute path; anything else runs as given."""
    first = start[0].replace("\\", "/")
    if first.lower().endswith((".bat", ".cmd")):
        quoted = " ".join(f'"{part}"' if " " in part else part for part in start[1:])
        return f'cmd.exe /d /s /c ""{root / first}" {quoted}"'
    return [str(root / first) if (root / first).is_file() else start[0], *start[1:]]


def start_app(root: Path, mig: Migration, report: MigrationReport, runner: Runner) -> None:
    """Start the clone through its declared start command (``tray.bat --restart`` unless declared)."""
    label = " ".join(mig.start)
    if report.what_if:
        report.add("start", "would run", label)
        return
    # The app it starts outlives the command and inherits its output handles: a file, never this run's pipe.
    log = root / ".venv" / START_LOG
    try:
        with open(log, "wb") as out:  # runtime-write-exempt: the start log lives in the ignored .venv
            done = runner(_command_line(root, mig.start), cwd=root, timeout=START_TIMEOUT_S, check=False,
                          stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        report.add("start", FAILED, label, f"did not run ({type(exc).__name__}); output in .venv/{START_LOG}")
        return
    report.add("start", OK if done.returncode == 0 else FAILED, label,
               f"exited {done.returncode}; output in .venv/{START_LOG}")


def fetch_version(endpoint: Endpoint) -> dict[str, Any] | None:
    """The endpoint's JSON on 127.0.0.1, or None when it does not answer 200 with an object."""
    conn: http.client.HTTPConnection
    if endpoint.scheme == "https":
        context = ssl.create_default_context()
        if endpoint.self_signed:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection("127.0.0.1", endpoint.port, timeout=FETCH_TIMEOUT_S, context=context)
    else:
        conn = http.client.HTTPConnection("127.0.0.1", endpoint.port, timeout=FETCH_TIMEOUT_S)
    try:
        conn.request("GET", endpoint.version_path)
        response = conn.getresponse()
        body = json.loads(response.read()) if response.status == 200 else None
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()
    return body if isinstance(body, dict) else None


def same_commit(served: str, head: str) -> bool:
    served, head = served.strip().lower(), head.strip().lower()
    return len(served) >= MIN_SHA_LEN and len(head) >= MIN_SHA_LEN and (head.startswith(served)
                                                                       or served.startswith(head))


def _schemes(endpoint: Endpoint) -> list[Endpoint]:
    """The declared scheme, then the other one, so a PC that serves plain http (or https) still answers.

    The other https is asked with any certificate accepted: the host is always 127.0.0.1.
    """
    other = "https" if endpoint.scheme == "http" else "http"
    return [endpoint, replace(endpoint, scheme=other, self_signed=other == "https")]


def _url(endpoint: Endpoint) -> str:
    return f"{endpoint.scheme}://127.0.0.1:{endpoint.port}{endpoint.version_path}"


def _wait_for_head(endpoint: Endpoint, head: str,
                   fetch: Callable[[Endpoint], dict[str, Any] | None]) -> tuple[str | None, Endpoint]:
    """Poll until either scheme serves ``head``; the last commit served (None: no answer) and the scheme that said it."""
    deadline, served, answered = time.monotonic() + VERIFY_TIMEOUT_S, None, endpoint
    # request-budget-exempt: a one-shot setup poll of loopback, capped by VERIFY_TIMEOUT_S, ends when HEAD is served
    while True:
        for candidate in _schemes(endpoint):
            body = fetch(candidate)
            if body is not None:
                served, answered = str(body.get("git_sha") or ""), candidate
                if same_commit(served, head):
                    return served, candidate
        if time.monotonic() >= deadline:
            return served, answered
        time.sleep(POLL_S)


def verify(root: Path, old: Path | None, mig: Migration, report: MigrationReport, runner: Runner,
           snapshot_fn: Callable[[], Snapshot | None],
           fetch: Callable[[Endpoint], dict[str, Any] | None] = fetch_version) -> None:
    """Pass only when the endpoint serves this clone's HEAD and the process on its port runs from this clone.

    The port is the one this PC's config sets (``port_config``), the declared one as fallback; the scheme
    is whichever of http and https answers on it.
    """
    endpoint = mig.endpoint
    if endpoint is None:
        report.add("verify", "would be unknown" if report.what_if else UNKNOWN, "endpoint",
                   "no [install.endpoint] declared, so nothing proves the clone serves")
        return
    # Under -WhatIf the clone has no copied config yet; the old copy's is what it will get.
    port, source = local_port(old if report.what_if and old is not None else root, endpoint)
    if port is None:
        report.add("verify", UNKNOWN, "endpoint port", source)
        return
    endpoint = replace(endpoint, port=port)
    if report.what_if:
        report.add("verify", "would check", _url(endpoint), f"port from {source}; serves this clone's HEAD "
                   "from a process in this clone, on http or https")
        return
    head = git(["rev-parse", "HEAD"], root, runner)
    if not head:
        report.add("verify", UNKNOWN, "this clone's HEAD", "git could not read it")
        return
    served, answered = _wait_for_head(endpoint, head, fetch)
    if served is None:
        report.add("verify", FAILED, _url(endpoint), f"no answer on http or https within {VERIFY_TIMEOUT_S} s "
                   f"(port from {source})")
    elif not same_commit(served, head):
        report.add("verify", FAILED, _url(answered), f"serves {served[:12] or 'no git_sha'}, "
                   f"this clone is {head[:12]}")
    else:
        report.add("verify", OK, _url(answered), f"serves {head[:12]} (port from {source})")
    _verify_owner(root, old, port, report, snapshot_fn)


def _verify_owner(root: Path, old: Path | None, port: int, report: MigrationReport,
                  snapshot_fn: Callable[[], Snapshot | None]) -> None:
    snap = snapshot_fn()
    if snap is None:
        report.add("verify", UNKNOWN, f"port {port} owner", "could not list processes and ports")
        return
    pids = snap.listeners.get(port, set())
    if not pids:
        report.add("verify", FAILED, f"port {port} owner", "nothing listens on the port")
        return
    for pid in sorted(pids):
        side, proc = owner_of(snap, pid, root, old)
        exe = os.path.basename(proc.exe) if proc else "?"
        if side == "clone":
            report.add("verify", OK, f"port {port} owner", f"PID {pid} runs from this clone ({exe})")
        elif side == "old":
            report.add("verify", FAILED, f"port {port} owner", f"PID {pid} runs from the old copy ({exe}); "
                       "stop it (-StopOld) and run again")
        else:
            report.add("verify", UNKNOWN, f"port {port} owner", f"PID {pid} ({exe}) runs from neither copy")


# ---------------------------------------------------------------- -MigrateFrom

def guard_migrate(root: Path, old: Path, report: MigrationReport, runner: Runner) -> Migration | None:
    """Stop unless ``root`` is a clean clone at its own root, ``old`` is another folder, and the declaration parses."""
    top = git(["rev-parse", "--show-toplevel"], root, runner)
    if top is None or _norm(top) != _norm(str(root)):
        report.add("guard", "refused", "this folder", "not the root of a git checkout")
        return None
    status = git(["status", "--porcelain"], root, runner)
    if status is None:
        report.add("guard", UNKNOWN, "this folder", "git status did not run")
        return None
    if status:
        report.add("guard", "refused", "this folder", "not a fresh clone: it has changed or untracked files")
        return None
    if not old.is_dir():
        report.add("guard", "refused", "old folder", "does not exist")
        return None
    if inside(str(old), root) or inside(str(root), old):
        report.add("guard", "refused", "old folder", "is this folder, or inside or around it")
        return None
    try:
        mig = parse_migration(read_fleet_toml(root))
    except ValueError as exc:
        report.add("guard", "refused", ".fleet.toml", str(exc))
        return None
    report.add("guard", OK, "fresh clone", str(root))
    return mig


def _install(root: Path, installer: ModuleType, report: MigrationReport, startup: bool, as_json: bool) -> bool:
    """The normal install pass over the clone; whether it finished."""
    stdout = sys.stdout
    if as_json:
        sys.stdout = sys.stderr  # the install steps' own output must not mix with the JSON document
    try:
        result = installer.run(root, what_if=report.what_if, startup=startup)
    finally:
        sys.stdout = stdout
    report.install = {"lines": result.lines, "required": result.required, "optional": result.optional,
                      "failed": result.failed}
    report.install_text = installer.render(result)
    return not result.failed


def migrate(root: Path, old: Path, *, installer: ModuleType, what_if: bool, stop: bool, startup: bool = False,
            as_json: bool = False, runner: Runner = subprocess.run,
            snapshot_fn: Callable[[], Snapshot | None] | None = None,
            fetch: Callable[[Endpoint], dict[str, Any] | None] = fetch_version) -> MigrationReport:
    """One ``-MigrateFrom`` pass; ``runner``, ``snapshot_fn`` and ``fetch`` stand in for the system in tests."""
    report = MigrationReport(mode="migrate", what_if=what_if)
    snapshot_fn = snapshot_fn or (lambda: take_snapshot(runner))
    old = old.resolve()
    mig = guard_migrate(root, old, report, runner)
    if mig is None:
        return report
    snap = snapshot_fn()
    inventory(old, report, runner, snap)
    port = local_port(old, mig.endpoint)[0] if mig.endpoint else None
    if stop:
        stop_old(old, port, report, runner, snapshot_fn)
        if report.failed:
            return report
        snap = None if report.what_if else snapshot_fn()
    mine = ancestors(snap, os.getpid()) if snap else set()
    old_running = snap is None or bool(processes_from(snap, old, mine))
    copied = copy_declared(old, root, mig, report, old_running and not (stop and report.what_if))
    rebind_copied(old, root, copied, report)
    compare_secrets(old, root, mig, report)
    report_shared(mig, report)
    if not _install(root, installer, report, startup, as_json):
        report.add("start", "not run", "start", "the install did not finish")
        return report
    start_app(root, mig, report, runner)
    verify(root, old, mig, report, runner, snapshot_fn, fetch)
    if not report.what_if:
        _report_processes(old, report, "leftover", snapshot_fn(), "still running")
        _report_autostart(old, report, "leftover", runner)
    return report


def emit(report: MigrationReport, as_json: bool) -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")
    sys.stdout.write((json.dumps(report.to_json(), indent=2) if as_json else report.render()) + "\n")
    sys.stdout.flush()
    return 1 if report.failed else 0


def main_migrate(root: Path, old: Path, *, installer: ModuleType, what_if: bool, stop: bool, startup: bool,
                 as_json: bool) -> int:
    """``setup.bat -MigrateFrom``: run, print the report, return the exit code."""
    report = migrate(root, old, installer=installer, what_if=what_if, stop=stop, startup=startup, as_json=as_json)
    return emit(report, as_json)

