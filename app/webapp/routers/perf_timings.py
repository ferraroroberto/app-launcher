"""Real-phone timing routes — the server half of ``static/perf-timing.js``.

    POST /api/perf/timings          a batch of load / slow-API rows
    GET  /api/perf/summary?days=N   median and p95 per leg, slowest paths

Both sit behind the bearer-token middleware like every ``/api`` route. The
store, its validation and the summary maths live in
:mod:`src.usage.perf_timings`; this module only turns a raw request path into
the matching route's template and stamps the build this process loaded.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.routing import Match

from src.build_info import resolve_git_sha
from src.runtime_data import runtime_db_path
from src.usage import perf_timings as perf

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/perf")

# What this process loaded, so a row from a stale build is told apart.
_BUILD = resolve_git_sha()[:12]


def _store() -> perf.PerfTimings:
    return perf.PerfTimings(runtime_db_path("app-launcher", "perf.sqlite3", create=True))


def _templater(request: Request) -> perf.Templater:
    """Map a raw path to the template of the route that serves it, so only
    route names, never ids or file names, reach the table."""
    def template(path: str) -> Optional[str]:
        # Resource Timing carries no method; a method mismatch is a partial
        # match, which still names the route.
        scope = {"type": "http", "method": "GET", "path": path, "root_path": ""}
        for route in request.app.routes:
            matches = getattr(route, "matches", None)
            path_format = getattr(route, "path_format", None)
            if matches is None or path_format is None or not path_format.startswith("/api/"):
                continue
            match, _ = matches(scope)
            if match is not Match.NONE:
                return path_format
        return None
    return template


@router.post("/timings")
async def post_timings(request: Request) -> JSONResponse:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > perf.MAX_BODY_BYTES:
        return JSONResponse({"detail": "batch too large"}, status_code=413)
    body = await request.body()
    try:
        envelope, rows, rejected = perf.parse_batch(body, _templater(request))
    except perf.PayloadError as exc:
        status = 413 if len(body) > perf.MAX_BODY_BYTES else 400
        return JSONResponse({"detail": str(exc)}, status_code=status)
    if not rows:
        return JSONResponse({"accepted": 0, "rejected": rejected})
    try:
        accepted = await run_in_threadpool(_store().record, envelope, rows, build=_BUILD)
    except sqlite3.Error as exc:
        logger.info("ℹ️ perf timings: dropped %d row(s), database unavailable (%s)", len(rows), exc)
        return JSONResponse({"detail": "perf timings unavailable"}, status_code=503)
    return JSONResponse({"accepted": accepted, "rejected": rejected})


@router.get("/summary")
def get_summary(days: int = Query(7, ge=1, le=perf.MAX_SUMMARY_DAYS)) -> JSONResponse:
    db_path = runtime_db_path("app-launcher", "perf.sqlite3")
    try:
        conn = perf.open_read_only(db_path)
    except FileNotFoundError:
        return JSONResponse({"days": days, "rows": 0, "log_exists": False})
    try:
        report = perf.summarize(conn, days)
    except sqlite3.Error as exc:
        logger.info("ℹ️ perf timings: summary unavailable (%s)", exc)
        return JSONResponse({"detail": "perf timings unavailable"}, status_code=503)
    finally:
        conn.close()
    return JSONResponse({**report, "log_exists": True})
