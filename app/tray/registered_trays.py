"""Registered Trays and Mini apps sequential autostart boot sequence.

Split off ``app/tray/tray.py`` (a single-file god-module flagged by
``/codebase-audit``). These are pure functions with no ``TrayApp`` state —
``port_listening`` is also reused by ``tray.py`` for its own session-host
adoption check, since it's a general-purpose loopback-port probe.

Diagnosability: ``launch_all()`` runs at real boot time under
``pythonw.exe`` (no console), spawned by ``tray_lifecycle.ps1``'s
``Start-Process`` — which never redirects stdout/stderr anywhere. So the
``logger.info``/``logger.warning`` calls below are silently discarded in
production; a sister tray failing to launch, or taking an unusually long
time to become reachable, left no trace. Mirrors the breadcrumb log
``src/boot_autostart.py`` already writes for app-launcher's own Startup
wrapper: every attempt appends a timestamped line to a
gitignored ``webapp/registered_trays.log``, so a login-time failure is
diagnosable from that log alone instead of requiring a live re-run.

A tray entry with a declared port that does not answer after the first pass
is retried on a short bounded backoff. Before every launch the port is
probed, and before every retry the target's process is looked for too: a
duplicate tray is worse than a missed one.
"""

from __future__ import annotations

import datetime
import logging
import socket
import subprocess
import threading
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src import runtime_files
from src.launching.bat_running import find_running_target
from src.registry import RegistryReadError, load_registry
from src.scanner import KIND_MINI, KIND_TRAY
from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

_TRAY_READY_TIMEOUT_S = 30.0
_TRAY_READY_POLL_S = 0.5
# Fallback wait when a tray's repo has no readable .fleet.toml port — still
# gives it a head start before the next tray launches, without a real signal.
_TRAY_FALLBACK_DELAY_S = 5.0

# Waits before each retry of a tray the first pass could not confirm ready.
# A login can lose a spawn or a port bind to the boot race; three spaced
# retries (3.5 min plus the ready-waits) cover a slow boot without ever
# becoming an open-ended loop. The tuple's length is the retry budget.
_RETRY_BACKOFF_S = (30.0, 60.0, 120.0)

# A genuine config/apps.json read failure (as opposed to "no
# autostart trays configured") is retried a small, bounded number of times —
# the suspected trigger is a transient early-boot I/O hiccup (this repo lives
# on a second drive that may still be settling right at login on a full
# restart), likely to clear within a second or two. Kept small on purpose:
# this runs synchronously in TrayApp._start()'s thread on every boot and must
# not meaningfully delay the webapp becoming usable.
_REGISTRY_LOAD_ATTEMPTS = 3
_REGISTRY_LOAD_RETRY_DELAY_S = 1.0


def _log_path() -> Path:
    """The gitignored boot breadcrumb log this repo writes at each autostart.

    Reads ``PROJECT_ROOT`` off the module (not a bound default argument) so
    tests can redirect it via ``monkeypatch.setattr(rt_mod, "PROJECT_ROOT", ...)``.
    """
    return PROJECT_ROOT / "webapp" / "registered_trays.log"


def _log_breadcrumb(msg: str) -> None:
    """Best-effort append of a timestamped line to the breadcrumb log.

    Never raises — a log-write failure must not take down the boot
    sequence it exists to make diagnosable.
    """
    try:
        path = _log_path()
        runtime_files.make_dirs(path.parent)
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with runtime_files.open_for_write(path, "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {msg}\n")
    except OSError:
        pass


def port_listening(port: int) -> bool:
    """True if something is listening on 127.0.0.1:<port> (loopback)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _fleet_toml_port(repo_dir: Path) -> Optional[int]:
    """Read the ``port`` field out of ``repo_dir/.fleet.toml``.

    This is the fleet-wide anti-staleness-enforced convention every
    tray-owning repo already keeps current —
    the only per-repo-agnostic readiness signal available here. PID-tree
    port discovery (as the Running Apps panel uses for launcher-spawned
    bats) does NOT work for a Registered Trays entry: every sister
    ``tray.bat`` hands off via ``tray_lifecycle.ps1``'s ``Start-Process``
    (verified in that shared script), which detaches the real long-lived
    tray process from the invoking ``tray.bat``'s own process — that
    invoking process exits within about a second, well before any webapp
    binds its port, so a PID-tree walk rooted there finds nothing.

    Accepts both shapes seen across the fleet: a bare int (``port =
    8447``) or a leading-colon string (``port = ":8445"``). Returns
    ``None`` on a missing file, missing field, or an unrecognised shape —
    the caller falls back to a fixed delay.
    """
    toml_path = repo_dir / ".fleet.toml"
    try:
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    raw = data.get("port")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw.lstrip(":"))
        except ValueError:
            return None
    return None


def _record(msg: str, level: int = logging.INFO) -> None:
    """Log ``msg`` and append it to the breadcrumb log, which is the only
    copy that survives a console-less boot."""
    logger.log(level, f"Registered Trays: {msg}")
    _log_breadcrumb(msg)


def _spawn_tray_bat_detached(bat_path: Path) -> Optional[int]:
    """Quiet-launch a Registered Trays entry's ``tray.bat``; return the pid of
    the ``cmd`` that starts it (it exits once the bat hands off).

    Detached the same way as the main tray's own session-host spawn —
    re-parented out of this tray's process subtree via ``cmd /c start`` —
    so a later ``tray.bat --restart`` on THIS machine never touches it (it
    isn't this repo's process to manage beyond starting it). An ``OSError``
    from ``Popen`` propagates so the caller can log it as a spawn failure.
    """
    cmd = ["cmd", "/c", "start", "", "/b", str(bat_path)]
    kw: dict = dict(
        cwd=str(bat_path.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
    )
    return subprocess.Popen(cmd, **kw).pid


def _wait_for_tray_ready(repo_dir: Path) -> bool:
    """Best-effort readiness wait for a just-launched sister tray.

    See :func:`_fleet_toml_port` for why this is `.fleet.toml`-port
    based rather than PID-tree port discovery. No/malformed port
    declaration → a fixed delay stand-in, returning ``False`` (not
    confirmed ready, but gave it a head start).
    """
    port = _fleet_toml_port(repo_dir)
    if port is None:
        time.sleep(_TRAY_FALLBACK_DELAY_S)
        return False
    deadline = time.monotonic() + _TRAY_READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if port_listening(port):
            return True
        time.sleep(_TRAY_READY_POLL_S)
    return False


def _pause(seconds: float, stop: Optional[threading.Event]) -> bool:
    """Wait out one backoff step; True when the tray is stopping."""
    if stop is None:
        time.sleep(seconds)
        return False
    return stop.wait(seconds)


@dataclass(frozen=True)
class _Pending:
    """A tray entry the first pass could not confirm ready."""

    name: str
    bat_path: Path
    port: int


def _load_autostart_entries() -> Optional[list]:
    """The autostart tray and mini rows, or ``None`` when the registry could
    not be read (retried a bounded number of times first)."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, _REGISTRY_LOAD_ATTEMPTS + 1):
        try:
            registry = load_registry(strict=True)
        except RegistryReadError as exc:
            last_exc = exc
            _record(
                f"registry read failed (attempt {attempt}/{_REGISTRY_LOAD_ATTEMPTS}): {exc}",
                logging.WARNING,
            )
            if attempt < _REGISTRY_LOAD_ATTEMPTS:
                time.sleep(_REGISTRY_LOAD_RETRY_DELAY_S)
            continue
        except Exception as exc:  # noqa: BLE001 — unexpected, not retried
            _record(f"could not load registry: {exc}", logging.WARNING)
            return None
        return [a for a in registry.apps if a.kind in (KIND_TRAY, KIND_MINI) and a.autostart]
    _record(
        f"registry unreadable after {_REGISTRY_LOAD_ATTEMPTS} attempts "
        f"— giving up: {last_exc}",
        logging.WARNING,
    )
    return None


def _usable_bat(entry) -> Optional[Path]:
    if not entry.bat_path:
        _record(f"{entry.name}: no bat_path — skipped", logging.WARNING)
        return None
    bat_path = Path(entry.bat_path)
    if not bat_path.is_file():
        _record(f"{entry.name}: bat not found at {bat_path} — skipped", logging.WARNING)
        return None
    return bat_path


def _spawn(name: str, bat_path: Path) -> bool:
    """Spawn one entry; the breadcrumb says whether the spawn itself worked,
    so a failed start never reads as a slow one."""
    try:
        pid = _spawn_tray_bat_detached(bat_path)
    except Exception as exc:  # noqa: BLE001 — one entry must not abort the walk
        _record(f"{name}: spawn failed ({type(exc).__name__}: {exc})", logging.WARNING)
        return False
    _record(f"{name}: spawn ok (launcher pid {pid}) {bat_path}")
    return True


def _launch_and_wait(name: str, bat_path: Path, port: Optional[int]) -> bool:
    """Spawn a tray entry and wait for its port; True only when it answered."""
    if not _spawn(name, bat_path):
        return False
    if _wait_for_tray_ready(bat_path.parent):
        _record(f"{name}: ready")
        return True
    if port is None:
        _record(
            f"{name}: readiness unconfirmed (no .fleet.toml port) — not retried",
            logging.WARNING,
        )
    else:
        _record(
            f"{name}: readiness timeout (:{port} closed after "
            f"{_TRAY_READY_TIMEOUT_S:.0f}s)",
            logging.WARNING,
        )
    return False


def _first_pass(entries: list) -> list:
    """Start each entry in order, waiting on each tray before the next so a
    cold boot never starts several Python processes at once. Returns the
    tray entries with a declared port that did not come up."""
    pending: list = []
    for entry in entries:
        bat_path = _usable_bat(entry)
        if bat_path is None:
            continue
        if entry.kind == KIND_MINI:
            # A mini app declares no port, so there is nothing to wait on.
            if not _already_running(entry.name, bat_path):
                _spawn(entry.name, bat_path)
            continue
        port = _fleet_toml_port(bat_path.parent)
        if port is not None and port_listening(port):
            _record(f"{entry.name}: ready (:{port} already answering) — not launched")
            continue
        if not _launch_and_wait(entry.name, bat_path, port) and port is not None:
            pending.append(_Pending(entry.name, bat_path, port))
    return pending


def _came_up(item: _Pending) -> bool:
    """True when a retry must not launch: the port answers, the target is
    running, or the process table cannot rule out that it is."""
    if port_listening(item.port):
        _record(f"{item.name}: ready (:{item.port} answering) — not relaunched")
        return True
    try:
        pid = find_running_target(item.bat_path)
    except RuntimeError as exc:
        _record(
            f"{item.name}: not relaunched, could not check it is not running: {exc}",
            logging.WARNING,
        )
        return True
    if pid is None:
        return False
    _record(f"{item.name}: already running (pid {pid}) — not relaunched")
    return True


def _reconcile(pending: list, stop: Optional[threading.Event]) -> None:
    """Retry entries the first pass missed, one round per backoff step, and
    give up on each with a ``failed`` breadcrumb once the budget is spent."""
    budget = len(_RETRY_BACKOFF_S)
    for attempt, delay in enumerate(_RETRY_BACKOFF_S, start=1):
        if not pending:
            return
        if _pause(delay, stop):
            _record(f"reconciliation stopped: tray is stopping ({len(pending)} pending)")
            return
        still: list = []
        for item in pending:
            if _came_up(item):
                continue
            _record(f"{item.name}: retry {attempt}/{budget}")
            if not _launch_and_wait(item.name, item.bat_path, item.port):
                still.append(item)
        pending = still
    for item in pending:
        _record(
            f"{item.name}: failed after {budget} retries — start it from the Apps tab",
            logging.WARNING,
        )


def launch_all(stop: Optional[threading.Event] = None) -> None:
    """Walk autostart-enabled Registered Trays entries in the registry's
    alphabetical order (see :func:`src.registry.persist_additions`), then
    retry the trays that did not come up on a bounded backoff while ``stop``
    stays clear (the tray's own shutdown event).

    One entry failing to launch, or its readiness check timing out, is
    logged and does NOT abort the rest — this function itself must never
    raise into its caller (``TrayApp._start``).
    """
    entries = _load_autostart_entries()
    if not entries:
        return
    _record(f"launching {len(entries)} autostart entr{'y' if len(entries) == 1 else 'ies'}")
    _reconcile(_first_pass(entries), stop)


def _already_running(name: str, bat_path: Path) -> bool:
    """True when a mini app's target is up already, or cannot be ruled out.

    An unreadable process table skips the launch: a missed autostart is one
    tap on the row, a duplicate is a second copy of something that may hold a
    port, a lock or a tray icon.
    """
    try:
        pid = find_running_target(bat_path)
    except RuntimeError as exc:
        _record(f"{name}: not started, could not check it is not running: {exc}", logging.WARNING)
        return True
    if pid is None:
        return False
    _record(f"{name}: already running (pid {pid}) — skipped")
    return True
