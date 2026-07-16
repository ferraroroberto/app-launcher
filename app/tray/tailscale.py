"""Tailscale CLI discovery and tailnet-hostname resolution for the tray.

Split off ``app/tray/tray.py`` (a single-file god-module flagged by
``/codebase-audit``). These are pure functions with no ``TrayApp`` state —
callers pass a debug-breadcrumb callback rather than this module owning a
log path itself, so it stays decoupled from ``tray.py``'s own
``PROJECT_ROOT``-derived paths.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# No-op default so callers that don't care about breadcrumbing (e.g. tests)
# don't have to pass one.
_NO_DEBUG: Callable[[str], None] = lambda _msg: None  # noqa: E731


def find_binary() -> Optional[str]:
    """Locate the tailscale CLI — PATH first, then the standard Windows install.

    The GUI installer drops ``tailscale.exe`` under ``Program Files`` but
    doesn't always add it to PATH, and the tray is often started by Task
    Scheduler with a minimal environment — so PATH alone isn't enough.
    """
    found = shutil.which("tailscale")
    if found:
        return found
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Tailscale" / "tailscale.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Tailscale" / "tailscale.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _run(binary: str, args: list) -> subprocess.CompletedProcess:
    """Run the tailscale CLI windowless, with stdin detached.

    ``CREATE_NO_WINDOW`` stops a console flashing out of the windowless
    tray; ``stdin=DEVNULL`` avoids the invalid-handle trap a ``pythonw``
    parent can hit when a child inherits a missing stdin.
    """
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        [binary, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=12,
        check=False,
        creationflags=creationflags,
    )


def resolve_hostname(debug: Callable[[str], None] = _NO_DEBUG) -> Optional[str]:
    """Return this machine's tailnet address, or None if unavailable.

    Prefers the full DNS name (e.g. ``tower.tailnet.ts.net``) — the form
    both the copied URL and the WebAuthn relying-party ID want — and falls
    back to the raw ``100.x`` IP. Every failure path is reported through
    ``debug`` (the tray wires this to its ``webapp/tailscale_debug.log``
    breadcrumb since it has no console).
    """
    binary = find_binary()
    if binary is None:
        debug("CLI not found on PATH or under Program Files")
        return None
    debug(f"using binary {binary}")

    # 1. `status --json` → Self.DNSName (the FQDN).
    try:
        result = _run(binary, ["status", "--self=true", "--peers=false", "--json"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        debug(f"status raised {type(exc).__name__}: {exc}")
        result = None
    if result is not None:
        if result.returncode != 0:
            debug(
                f"status rc={result.returncode} "
                f"stderr={(result.stderr or '').strip()[:200]!r}"
            )
        else:
            try:
                data = json.loads(result.stdout)
                dns = ((data.get("Self") or {}).get("DNSName") or "").rstrip(".")
                if dns:
                    debug(f"resolved DNSName {dns}")
                    return dns
                debug(
                    f"status ok but DNSName empty; "
                    f"BackendState={data.get('BackendState')!r}"
                )
            except ValueError as exc:
                debug(f"status JSON parse failed: {exc}")

    # 2. Fallback: `tailscale ip -4` → the raw 100.x address.
    try:
        ip_res = _run(binary, ["ip", "-4"])
        if ip_res.returncode == 0:
            lines = (ip_res.stdout or "").strip().splitlines()
            ip = lines[0].strip() if lines else ""
            if ip:
                debug(f"fell back to tailscale ip {ip}")
                return ip
        debug(
            f"ip -4 rc={ip_res.returncode} "
            f"stderr={(ip_res.stderr or '').strip()[:200]!r}"
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        debug(f"ip -4 raised {type(exc).__name__}: {exc}")
    return None
