"""Process build identity — the git SHA this process actually loaded.

Captured once, at import time, so it reflects the code running in *this*
process rather than live git state (a process started three days ago still
reports the SHA it booted with, even if ``HEAD`` has since moved). Shared by
the webapp's ``/api/version`` and the session-host's ``/healthz`` (#615) so
both processes report their identity the same way — the session-host is
excluded from ``tray.bat --restart``'s reclaim sweep (project-scaffolding#35,
to protect live PTYs), so it can run stale for days with nothing visible
saying so; this is the mechanism that makes that determinable.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Dict, Optional

from src.git_utils import resolve_default_ref, run_git

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def resolve_git_sha(project_root: Path = PROJECT_ROOT) -> str:
    """Short git SHA of ``project_root``'s checkout.

    Falls back to ``"unknown"`` if git isn't on PATH or this isn't a repo —
    both happen in test envs and shouldn't crash startup.
    """
    sha = run_git(project_root, ["rev-parse", "--short", "HEAD"], warn_on_failure=True)
    return sha or "unknown"


def build_identity(project_root: Path = PROJECT_ROOT) -> Dict[str, str]:
    """``{"git_sha", "captured_at"}`` for ``project_root``, computed now.

    Call once at process/module import to capture "what this process
    loaded"; call again later (fresh, uncached) to get the live, current
    value for comparison — that's exactly the staleness check #615 needs.
    """
    return {
        "git_sha": resolve_git_sha(project_root),
        "captured_at": _dt.datetime.now().replace(microsecond=0).isoformat(),
    }


def _resolve_default_remote_ref(project_root: Path) -> Optional[str]:
    """``origin/HEAD``'s target (e.g. ``"origin/main"``), falling back to
    whichever of ``origin/main`` / ``origin/master`` exists. ``None`` when
    neither resolves (no ``origin`` remote, git missing, not a repo)."""
    return resolve_default_ref(project_root, fallback_refs=("origin/main", "origin/master"))


def resolve_deployed_sha(project_root: Path = PROJECT_ROOT) -> str:
    """Short git SHA of ``project_root``'s resolved default remote branch
    (``origin/HEAD`` -> ``origin/main`` -> ``origin/master``) — the ref that
    reflects what's actually mergeable/deployed.

    Unlike :func:`resolve_git_sha`, which reports the live checkout's
    *current branch tip*, this is stable across whatever branch the checkout
    transiently sits on (e.g. a worker occupying the primary tree mid-issue)
    — the exact mismatch that made ``/api/version``'s ``stale_relevant``
    compare against the wrong ref in #641. No fetch is performed: this reads
    the local ``origin/*`` remote-tracking ref as last synced, same as
    :func:`src.scanner._default_branch`. Falls back to ``"unknown"`` when no
    candidate ref resolves, git isn't on PATH, or this isn't a repo.
    """
    ref = _resolve_default_remote_ref(project_root)
    if ref is None:
        return "unknown"
    sha = run_git(project_root, ["rev-parse", "--short", ref], warn_on_failure=True)
    return sha or "unknown"
