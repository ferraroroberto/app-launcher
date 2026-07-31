"""Registered Trays sequential autostart boot sequence (issue #456 part 2/2).

Split off ``app/tray/tray.py`` (a single-file god-module flagged by
``/codebase-audit``). These are pure functions with no ``TrayApp`` state —
``port_listening`` is also reused by ``tray.py`` for its own session-host
adoption check, since it's a general-purpose loopback-port probe.
"""

from __future__ import annotations

import logging
import socket
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Optional

from src.registry import load_registry
from src.scanner import KIND_TRAY
from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)

_TRAY_READY_TIMEOUT_S = 30.0
_TRAY_READY_POLL_S = 0.5
# Fallback wait when a tray's repo has no readable .fleet.toml port — still
# gives it a head start before the next tray launches, without a real signal.
_TRAY_FALLBACK_DELAY_S = 5.0


def port_listening(port: int) -> bool:
    """True if something is listening on 127.0.0.1:<port> (loopback)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _fleet_toml_port(repo_dir: Path) -> Optional[int]:
    """Read the ``port`` field out of ``repo_dir/.fleet.toml``.

    This is the fleet-wide anti-staleness-enforced convention every
    tray-owning repo already keeps current (project-scaffolding#83/#148) —
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


def _spawn_tray_bat_detached(bat_path: Path) -> None:
    """Quiet-launch a Registered Trays entry's ``tray.bat``.

    Detached the same way as the main tray's own session-host spawn —
    re-parented out of this tray's process subtree via ``cmd /c start`` —
    so a later ``tray.bat --restart`` on THIS machine never touches it (it
    isn't this repo's process to manage beyond starting it).
    """
    cmd = ["cmd", "/c", "start", "", "/b", str(bat_path)]
    kw: dict = dict(
        cwd=str(bat_path.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
    )
    subprocess.Popen(cmd, **kw)


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


def launch_all() -> None:
    """Walk autostart-enabled Registered Trays entries one at a time,
    waiting for each to report ready before starting the next — avoids
    a boot-time CPU/disk spike from launching several sister
    Python/Streamlit processes concurrently. The registry's existing
    alphabetical order (see :func:`src.registry.persist_additions`) is
    the "fixed order" the issue accepts for v1 — no UI reordering.

    One entry failing to launch, or its readiness check timing out,
    is logged and does NOT abort the rest of the sequence — this
    function itself must never raise into its caller (``TrayApp._start``).
    """
    try:
        registry = load_registry()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"⚠️  Registered Trays: could not load registry: {exc}")
        return
    trays = [a for a in registry.apps if a.kind == KIND_TRAY and a.autostart]
    if not trays:
        return
    logger.info(f"🧭 Registered Trays: launching {len(trays)} autostart entr{'y' if len(trays) == 1 else 'ies'}")
    for entry in trays:
        if not entry.bat_path:
            logger.warning(f"⚠️  Registered Trays: {entry.name} has no bat_path — skipped")
            continue
        bat_path = Path(entry.bat_path)
        if not bat_path.is_file():
            logger.warning(f"⚠️  Registered Trays: {entry.name} bat not found at {bat_path} — skipped")
            continue
        try:
            logger.info(f"🚀 Registered Trays: launching {entry.name}")
            _spawn_tray_bat_detached(bat_path)
            ready = _wait_for_tray_ready(bat_path.parent)
            if ready:
                logger.info(f"✅ Registered Trays: {entry.name} ready")
            else:
                logger.warning(
                    f"⚠️  Registered Trays: {entry.name} readiness unconfirmed "
                    "(timed out or no .fleet.toml port) — continuing"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"⚠️  Registered Trays: {entry.name} failed to launch: {exc}")
            continue
