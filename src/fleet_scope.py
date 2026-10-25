"""Fleet scope: which repos the fleet-wide runs include (active) or skip (dormant).

The switch lives in fleet-config's machine-local ``hooks/projects.toml`` and
fleet-config's ``skills/_lib/fleet_status.py`` is the only code that reads or
writes it. This module loads that contract by path (the same load
:mod:`src.board.active_issue_claims` uses) and calls ``list_status()`` /
``set_status()``; it never opens the TOML itself.

A missing, old or unloadable contract is its own state, ``available: False``
with a reason. It is never an empty list and never "everything active".
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional, Tuple

from src._fleet_contract import load_fleet_contract
from src._log_once import log_once

logger = logging.getLogger(__name__)

ACTIVE = "active"
DORMANT = "dormant"
VALID_STATUSES = (ACTIVE, DORMANT)

# One warning per distinct failure per process: the sheet re-reads on every
# open, and a fleet-config checkout that is absent stays absent.
_logged_failures: set[str] = set()
_FAILURE_LOG_CAP = 64


class FleetScopeError(Exception):
    """The writer refused a change; the message is the writer's own."""


def _unavailable(reason: str) -> Dict[str, Any]:
    return {"available": False, "reason": reason, "repos": []}


def _load(fleet_config_dir: str) -> Tuple[Optional[ModuleType], str]:
    """The contract module, or ``(None, reason)`` when it cannot serve."""
    if not str(fleet_config_dir or "").strip():
        return None, "The fleet-config folder is not set in Settings."
    path = Path(fleet_config_dir) / "skills" / "_lib" / "fleet_status.py"
    if not path.is_file():
        return None, "This fleet-config checkout has no fleet scope support yet."
    try:
        module = load_fleet_contract(
            str(path.resolve()), "launcher_fleet_status", "fleet-scope"
        )
        for name in ("list_status", "set_status", "FleetStatusError"):
            getattr(module, name)
    except Exception as exc:  # a broken checkout must not break Settings
        log_once(
            _logged_failures, str(exc), _FAILURE_LOG_CAP, logger.warning,
            "⚠️ fleet scope: cannot load %s: %s", path, exc,
        )
        return None, "The fleet-config fleet scope module could not be loaded."
    return module, ""


def list_repos(fleet_config_dir: str) -> Dict[str, Any]:
    """``{available, reason, repos: [{name, status}]}``. Blocking: wrap in a thread."""
    module, reason = _load(fleet_config_dir)
    if module is None:
        return _unavailable(reason)
    try:
        rows = module.list_status()
    except module.FleetStatusError as exc:
        log_once(
            _logged_failures, str(exc), _FAILURE_LOG_CAP, logger.warning,
            "⚠️ fleet scope: cannot read the repo list: %s", exc,
        )
        return _unavailable(str(exc))
    repos: List[Dict[str, str]] = [
        {"name": str(r["name"]), "status": str(r["status"])} for r in rows
    ]
    return {"available": True, "reason": "", "repos": repos}


def set_status(fleet_config_dir: str, repo: str, status: str) -> str:
    """Flip ``repo``; returns the writer's ``changed`` / ``unchanged``.

    Raises :class:`FleetScopeError` with the writer's message on a refusal.
    The caller validates ``repo`` and ``status`` first; the writer checks both
    again, so a race still ends in a refusal, never a wrong write.
    """
    module, reason = _load(fleet_config_dir)
    if module is None:
        raise FleetScopeError(reason)
    try:
        return str(module.set_status(repo, status))
    except module.FleetStatusError as exc:
        raise FleetScopeError(str(exc)) from exc
