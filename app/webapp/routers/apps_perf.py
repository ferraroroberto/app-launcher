"""The Apps tab's phone-speed results — read-only, from the perf ledger.

    GET /api/apps/perf     latest result and cold-ready trend per ledger app,
                           plus which registry row belongs to which app

The ledger is fleet-config's; :mod:`src.perf_ledger` reads it. Nothing here
writes, and nothing about it reaches the Board.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from src import perf_ledger
from src.registry import load_registry
from src.scanner import KIND_CLAUDE_CODE, KIND_TUNNEL

router = APIRouter()


def _report() -> Dict[str, Any]:
    report = perf_ledger.build_report()
    # A tunnel row is not the app's own web row, and a Coding row is a
    # project, not a launchable app: neither is measured.
    rows: Dict[str, str] = {}
    for entry in load_registry().apps:
        if entry.kind in (KIND_CLAUDE_CODE, KIND_TUNNEL) or not entry.bat_path:
            continue
        app = perf_ledger.ledger_app_for(entry.bat_path, report["apps"])
        if app:
            rows[entry.id] = app
    report["rows"] = rows
    return report


@router.get("/api/apps/perf")
async def get_apps_perf() -> Dict[str, Any]:
    return await run_in_threadpool(_report)
