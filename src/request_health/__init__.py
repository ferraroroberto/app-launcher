"""What the launcher knows about its own request health: the budget table and the health report.

The timing machinery itself is the scaffold's vendored ``src/request_timing.py``;
this package holds the launcher's own request classes (here) and the read-only
``launcher.py health`` report (:mod:`src.request_health.snapshot`).

The phone's hot paths each get a soft and a hard limit. Over soft is an "over
budget" line in ``webapp/slow-requests.log``, over hard a "budget breach" line.
``request_budgets`` in ``webapp_config.json`` overrides a class's limits; a class
absent from it keeps the default here.
"""

from __future__ import annotations

import logging
from typing import Any

from src.request_timing import Budget, BudgetClass, RequestStats

logger = logging.getLogger(__name__)


def _is_transcript(path: str) -> bool:
    return path.startswith("/api/claude-code/sessions/") and path.endswith("/transcript")


#: The first class whose matcher accepts a request is its class, so order matters
#: where two could (the transcript tick before the transcript open).
BUDGETS: tuple[BudgetClass, ...] = (
    BudgetClass("transcript-tick", "GET", lambda p, q: _is_transcript(p) and "after=" in q, Budget(0.5, 1.5)),
    # The target names one number (1 s); soft and hard share it.
    BudgetClass("transcript-open", "GET", lambda p, q: _is_transcript(p), Budget(1.0, 1.0)),
    BudgetClass("session-list", "GET", lambda p, q: p == "/api/claude-code/sessions", Budget(0.5, 1.5)),
    BudgetClass("board", "GET", lambda p, q: p == "/api/board", Budget(0.5, 1.5)),
    BudgetClass(
        "send", "POST",
        lambda p, q: p.startswith("/api/claude-code/sessions/") and p.endswith("/input"),
        Budget(5.0, 10.0),
    ),
    BudgetClass("git-status", "GET", lambda p, q: p == "/api/claude-code/git-status", Budget(5.0, 10.0)),
    # The shell and the jobs poll answer from memory in tens of milliseconds;
    # their hard limit is the 3 s slow-request floor.
    BudgetClass("shell", "GET", lambda p, q: p == "/", Budget(0.25, 3.0)),
    BudgetClass("jobs-list", "GET", lambda p, q: p == "/api/jobs", Budget(0.25, 3.0)),
)

#: What the middleware has seen since boot, read by ``GET /api/request-stats``.
STATS = RequestStats()


def class_names() -> list[str]:
    return [cls.name for cls in BUDGETS]


def clean_overrides(raw: Any) -> tuple[dict[str, list[float]], list[str]]:
    """The usable part of a ``request_budgets`` value, and a line for each part dropped."""
    if raw is None:
        return {}, []
    if not isinstance(raw, dict):
        return {}, ["request_budgets must be an object of {class: [soft_s, hard_s]}"]
    known = class_names()
    kept: dict[str, list[float]] = {}
    dropped: list[str] = []
    for name, pair in raw.items():
        try:
            soft, hard = (float(v) for v in pair)
        except (TypeError, ValueError):
            soft = hard = 0.0
        if name not in known:
            dropped.append(f"unknown class {name!r} (known: {', '.join(known)})")
        elif not 0 < soft <= hard:
            dropped.append(f"{name!r} must be [soft_s, hard_s] with 0 < soft_s <= hard_s")
        else:
            kept[name] = [soft, hard]
    return kept, dropped


def load_request_budgets(raw: dict) -> dict[str, list[float]]:
    """``request_budgets`` out of the parsed config: the usable classes, a dropped one logged, never fatal."""
    kept, dropped = clean_overrides(raw.get("request_budgets"))
    for reason in dropped:
        logger.warning(f"⚠️  request_budgets ignored: {reason}")
    return kept
