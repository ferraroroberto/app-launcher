"""Which checkout a running process serves from, and whether it is this one.

Two checkouts of this app on one machine can be configured for the same ports. A port that
answers, or a ``/api/version`` that reports ``HEAD``, says nothing about which tree the answering
process runs: a second clone at the same commit gives the same answer. The tray spawns the webapp
and the session-host with ``cwd`` set to its checkout root, so a process's working directory names
the tree it imported its code from. Paths are compared after ``resolve()`` because the same tree
is reachable through a junction under two drive letters.

Every answer is one of three states: ``ours``, ``foreign`` (a named other directory) and
``unknown`` (the owner or its directory could not be read), and ``unknown`` is never ``ours``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from src.diagnostics import listener_pids

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - psutil is a declared dependency
    psutil = None  # type: ignore

logger = logging.getLogger(__name__)

OURS = "ours"
FOREIGN = "foreign"
UNKNOWN = "unknown"

# The addresses a loopback probe reaches and that keep the webapp from binding 0.0.0.0. A listener
# bound to one other address (Tailscale serving the same port on the tailnet) coexists with it.
_PROBE_ADDRESSES = frozenset({"0.0.0.0", "::", "127.0.0.1", "::1"})


@dataclass(frozen=True)
class PortOwner:
    """Who serves a port, relative to one checkout root. ``listening`` is ``False`` only when
    nothing was found on the port, which is still ``unknown``, never ``ours``."""

    state: str
    pids: List[int]
    checkout: Optional[Path]
    detail: str
    listening: bool = True


def process_checkout(pid: int) -> Optional[Path]:
    """The resolved working directory of ``pid``, or ``None`` when it cannot be read."""
    if psutil is None:
        return None
    try:
        return Path(psutil.Process(pid).cwd()).resolve()
    except (psutil.Error, OSError, ValueError) as exc:
        logger.info(f"ℹ️  could not read the working directory of pid {pid}: {exc}")
        return None


def probe_listener_pids(port: int, host: str = "") -> Optional[List[int]]:
    """The PIDs listening on ``port`` on a loopback or wildcard address, or on ``host``.

    ``None`` when unreadable. A socket table this process may not read falls back to every
    listener on the port, which can only make an answer stricter.
    """
    if psutil is None:
        return None
    try:
        connections = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError, OSError) as exc:
        logger.info(f"ℹ️  socket table unreadable ({exc}); counting every listener on :{port}")
        return listener_pids(port)
    addresses = _PROBE_ADDRESSES | {host}
    return sorted({
        int(c.pid)
        for c in connections
        if c.status == psutil.CONN_LISTEN
        and c.laddr
        and c.laddr.port == port
        and c.laddr.ip in addresses
        and c.pid
    })


def same_checkout(a: Path, b: Path) -> bool:
    """Whether two paths name the same tree, whatever junction or case spells them."""
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except (OSError, ValueError):
        return False


def classify_pid(
    pid: int, root: Path, checkout_of: Callable[[int], Optional[Path]] = process_checkout
) -> PortOwner:
    """Whether ``pid`` runs from ``root``."""
    checkout = checkout_of(pid)
    if checkout is None:
        return PortOwner(UNKNOWN, [pid], None, f"pid {pid}, whose directory could not be read")
    if same_checkout(checkout, root):
        return PortOwner(OURS, [pid], checkout, f"pid {pid} from this checkout {checkout}")
    return PortOwner(FOREIGN, [pid], checkout, f"pid {pid} from another checkout {checkout}")


def port_owner(port: int, root: Path, *, host: str = "") -> PortOwner:
    """Who listens on ``port``: ``ours`` only when every counted listener runs from ``root``."""
    pids = probe_listener_pids(port, host)
    if pids is None:
        return PortOwner(UNKNOWN, [], None, f"the listener on :{port} could not be read")
    if not pids:
        return PortOwner(UNKNOWN, [], None, f"no listener found on :{port}", listening=False)
    verdicts = [classify_pid(pid, root) for pid in pids]
    for state in (FOREIGN, UNKNOWN):
        hit = next((v for v in verdicts if v.state == state), None)
        if hit is not None:
            return PortOwner(state, pids, hit.checkout, f":{port} held by {hit.detail}")
    return PortOwner(OURS, pids, verdicts[0].checkout, f":{port} held by {verdicts[0].detail}")
