"""Owner liveness for the Board's active-issue claims (issue #948).

A claim row in ``active-issues.json`` outlives a lane that died without
``/issue-finish`` for up to the 24h read horizon. fleet-config#852 records the
row's owner (session id, agent pid, host) and owns the ``live`` / ``dead`` /
``unknown`` judgement in ``skills/_lib/active_issue.py``'s
``classify_owner``; this module only loads that contract (the same
file-path load :mod:`src.quota_usage` uses for ``quota_snapshot.py``) and
feeds it the launcher's own live-session list.

``unknown`` is its own state, never folded into either verdict: an owner-less
legacy row, an unreadable session list, a missing contract, or a classifier
that raises all read ``unknown``.
"""

from __future__ import annotations

import logging
from types import ModuleType
from typing import AbstractSet, Any, Dict, Optional

from src._fleet_contract import load_fleet_contract
from src._log_once import log_once

logger = logging.getLogger(__name__)

CLAIM_LIVE = "live"
CLAIM_DEAD = "dead"
CLAIM_UNKNOWN = "unknown"
_CLAIM_STATES = frozenset({CLAIM_LIVE, CLAIM_DEAD, CLAIM_UNKNOWN})

# One warning per distinct contract failure per process: GET /api/board polls
# every five seconds, and a missing fleet-config checkout is a steady state.
# Bounded since #1003 — this was the one of the three log-once sites with no
# cap at all, so a process seeing many distinct failure strings grew it
# without limit. 64 matches the glyph bucket: the key is a failure reason,
# a small and slow-growing set.
_logged_failures: set[str] = set()
_FAILURE_LOG_CAP = 64


def _load_contract(path_text: str) -> ModuleType:
    return load_fleet_contract(
        path_text, "launcher_active_issue", "active-issue"
    )


def _warn_once(reason: str) -> None:
    log_once(
        _logged_failures, reason, _FAILURE_LOG_CAP, logger.warning,
        "⚠️ board: active-issue claims unverified: %s", reason,
    )


def classify_claims(
    rows: Dict[str, Any],
    *,
    live_session_ids: Optional[AbstractSet[str]],
    fleet_config_dir: Path,
) -> Dict[str, str]:
    """Map each ``active_issues`` row key to ``live`` / ``dead`` / ``unknown``.

    ``live_session_ids`` is every alive session-host id, or ``None`` when the
    session list could not be read. Blocking (a first-call module load plus a
    pid probe per owned row) — callers wrap in ``asyncio.to_thread``.
    """
    if not rows:
        return {}
    contract_path = fleet_config_dir / "skills" / "_lib" / "active_issue.py"
    try:
        contract = _load_contract(str(contract_path.resolve()))
        classify_owner = contract.classify_owner
        pid_probe = contract.pid_state
        host = contract.current_host()
    except Exception as exc:  # a missing/old fleet-config must not break the Board
        _warn_once(f"cannot load {contract_path}: {exc}")
        return {key: CLAIM_UNKNOWN for key in rows}

    states: Dict[str, str] = {}
    for key, row in rows.items():
        try:
            state = classify_owner(
                row, live_session_ids=live_session_ids, pid_probe=pid_probe, host=host
            )
        except Exception as exc:
            _warn_once(f"classify_owner raised: {exc}")
            state = CLAIM_UNKNOWN
        states[key] = state if state in _CLAIM_STATES else CLAIM_UNKNOWN
    return states
