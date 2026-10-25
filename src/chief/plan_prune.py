"""Clear the chief's finished plan rows from the Board.

The plan file is written only through fleet-config's ``skills/_lib/chief_plan.py``,
which owns the format and the locking. This module loads that contract by path
(the load :mod:`src.fleet_scope` uses) and calls ``prune_plan_file``; it never
opens the plan itself.

A missing or older fleet-config is its own outcome, :class:`PlanPruneUnavailable`,
never a pruned count of zero: a tap that wrote nothing must not read as success.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import ModuleType

from src._fleet_contract import load_fleet_contract
from src._log_once import log_once

logger = logging.getLogger(__name__)

MERGED = "merged"

# One warning per distinct failure per process: a fleet-config checkout that
# is absent stays absent across taps.
_logged_failures: set[str] = set()
_FAILURE_LOG_CAP = 64


class PlanPruneUnavailable(Exception):
    """fleet-config cannot serve the prune; the plan file was not touched."""


class PlanPruneRefused(Exception):
    """The writer refused (e.g. a plan it cannot parse); its message is the reason."""


def _load(fleet_config_dir: str) -> ModuleType:
    if not str(fleet_config_dir or "").strip():
        raise PlanPruneUnavailable("The fleet-config folder is not set in Settings.")
    path = Path(fleet_config_dir) / "skills" / "_lib" / "chief_plan.py"
    if not path.is_file():
        raise PlanPruneUnavailable("This fleet-config checkout has no chief plan writer.")
    try:
        module = load_fleet_contract(str(path.resolve()), "launcher_chief_plan", "chief-plan")
        if not callable(getattr(module, "prune_plan_file", None)):
            raise AttributeError("prune_plan_file")
    except Exception as exc:  # a broken checkout must not become a 500
        log_once(
            _logged_failures, str(exc), _FAILURE_LOG_CAP, logger.warning,
            "⚠️ chief plan: cannot load %s: %s", path, exc,
        )
        raise PlanPruneUnavailable(
            "This fleet-config is too old to clear merged items; update it."
        ) from exc
    return module


def prune_merged(fleet_config_dir: str, plan_file: str) -> int:
    """Remove every ``merged`` queue row; returns how many went. Blocking.

    Raises :class:`PlanPruneUnavailable` when fleet-config cannot serve it and
    :class:`PlanPruneRefused` when the writer refuses the plan on disk.
    """
    module = _load(fleet_config_dir)
    try:
        pruned = int(module.prune_plan_file(Path(plan_file), (MERGED,)))
    except (ValueError, OSError) as exc:
        logger.warning("⚠️ chief plan: prune refused: %s", exc)
        raise PlanPruneRefused(str(exc)) from exc
    logger.info("ℹ️ chief plan: cleared %d merged item(s)", pruned)
    return pruned
