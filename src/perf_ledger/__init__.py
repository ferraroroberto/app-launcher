"""Read-only view of fleet-config's perf-review ledger, for the Apps tab.

``/perf-review`` appends one JSON line per app per run to
``<hooks state dir>/perf-review/ledger.jsonl`` (schema: fleet-config
``skills/perf-review/SKILL.md``, "The ledger"). This module only reads it.
The file is append-only and a reader keeps the last line per
``(app, run_id)`` and skips a line that does not parse.

A reader that cannot establish a fact says so: a missing file is ``missing``,
one that cannot be read is ``unreadable``, and a record whose verdict is not
one of the three known values is ``unknown``. None of those is ever folded
into ``pass``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

STATE_PASS = "pass"
STATE_OVER_BUDGET = "over-budget"
STATE_UNMEASURED = "unmeasured"
STATE_UNKNOWN = "unknown"
_KNOWN_OVERALL = (STATE_PASS, STATE_OVER_BUDGET, STATE_UNMEASURED)

LEDGER_OK = "ok"
LEDGER_MISSING = "missing"
LEDGER_UNREADABLE = "unreadable"

# Runs kept for the cold-ready sparkline: enough to show a drift on a phone
# width without the line turning into noise.
TREND_RUNS = 12

# The repo's open breach issues, as a search the repo's own page answers: a
# link built from a template needs no network call per request.
_ISSUES_URL = "https://github.com/ferraroroberto/{repo}/issues?q=is%3Aissue+is%3Aopen+label%3Aperf-review"


def ledger_path() -> Path:
    """The ledger file: ``CLAUDE_HOOKS_STATE_DIR`` or ``~/.claude/hooks/state``."""
    override = os.environ.get("CLAUDE_HOOKS_STATE_DIR", "").strip()
    base = Path(override) if override else Path.home() / ".claude" / "hooks" / "state"
    return base / "perf-review" / "ledger.jsonl"


def read_ledger(path: Path) -> Tuple[str, List[Dict[str, Any]], int]:
    """``(status, records, skipped)``; records deduped to the last line per (app, run_id)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return LEDGER_MISSING, [], 0
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("⚠️ perf ledger unreadable (%s): %s", path.name, exc)
        return LEDGER_UNREADABLE, [], 0
    latest: Dict[Tuple[str, str], Dict[str, Any]] = {}
    skipped = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        app = rec.get("app") if isinstance(rec, dict) else None
        run_id = rec.get("run_id") if isinstance(rec, dict) else None
        if not isinstance(app, str) or not app or not isinstance(run_id, str) or not run_id:
            skipped += 1
            continue
        latest[(app, run_id)] = rec
    return LEDGER_OK, list(latest.values()), skipped


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _check(rec: Dict[str, Any], check_id: str) -> Optional[Dict[str, Any]]:
    """One check as ``{measured, budget, status}``; None when absent or unmeasured."""
    checks = rec.get("checks")
    raw = checks.get(check_id) if isinstance(checks, dict) else None
    if not isinstance(raw, dict):
        return None
    measured = _num(raw.get("measured"))
    if measured is None or raw.get("status") == "unmeasured":
        return None
    return {"measured": measured, "budget": _num(raw.get("budget")), "status": raw.get("status")}


def _state(rec: Dict[str, Any]) -> str:
    overall = rec.get("overall")
    return overall if overall in _KNOWN_OVERALL else STATE_UNKNOWN


def _entry(app: str, runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The Apps-tab entry for one app; ``runs`` is oldest first, never empty."""
    rec = runs[-1]
    state = _state(rec)
    checks = rec.get("checks")
    fails = sorted(
        cid for cid, raw in (checks.items() if isinstance(checks, dict) else [])
        if isinstance(raw, dict) and raw.get("status") == "fail"
    )
    trend: List[float] = []
    for run in runs:
        cold = _check(run, "cold.ready_ms")
        if cold is not None:
            trend.append(cold["measured"])
    return {
        "state": state,
        "run_id": rec["run_id"],
        "ts": rec.get("ts") if isinstance(rec.get("ts"), str) else None,
        "build": rec.get("build") if isinstance(rec.get("build"), str) else None,
        "seeded": rec.get("seeded_from") is not None,
        "cold_ready_ms": _check(rec, "cold.ready_ms"),
        "warm_ready_ms": _check(rec, "warm.ready_ms"),
        "warm_bytes_kb": _check(rec, "warm.bytes_kb"),
        "fails": fails,
        "trend": trend[-TREND_RUNS:],
        "issues_url": _ISSUES_URL.format(repo=app) if state == STATE_OVER_BUDGET else None,
    }


def summarise(records: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Latest result and trend per ledger app."""
    by_app: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        by_app.setdefault(rec["app"], []).append(rec)
    return {
        app: _entry(app, sorted(runs, key=lambda r: r["run_id"]))
        for app, runs in by_app.items()
    }


def ledger_app_for(bat_path: str, ledger_apps: Iterable[str]) -> Optional[str]:
    """The ledger app a registry row belongs to: the nearest ancestor folder of its bat that the ledger names.

    Matched on the folder name, case-insensitively, since the ledger's app is
    the fleet repo's folder name. Split on both separators: a registry written
    on Windows is read here as text.
    """
    names = {a.lower(): a for a in ledger_apps}
    parts = [p for p in bat_path.replace("\\", "/").split("/") if p]
    for folder in reversed(parts[:-1]):
        hit = names.get(folder.lower())
        if hit:
            return hit
    return None


def build_report(path: Optional[Path] = None) -> Dict[str, Any]:
    """The read-only answer behind ``GET /api/apps/perf`` (rows are added by the route)."""
    path = path or ledger_path()
    status, records, skipped = read_ledger(path)
    apps = summarise(records) if status == LEDGER_OK else {}
    logger.info("ℹ️ perf ledger: %s, %d records, %d lines skipped", status, len(records), skipped)
    return {"ledger": status, "apps": apps}
