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
import re
import sqlite3
from typing import Optional

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from src.build_info import resolve_git_sha
from src.runtime_data import runtime_db_path
from src.usage import perf_timings as perf

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/perf")

# What this process loaded, so a row from a stale build is told apart.
_BUILD = resolve_git_sha()[:12]


def _store() -> perf.PerfTimings:
    return perf.PerfTimings(runtime_db_path("app-launcher", "perf.sqlite3", create=True))


def _route_patterns(app: FastAPI) -> list[tuple[str, re.Pattern[str]]]:
    """Every ``/api`` template the app serves, fewest parameters first so a
    literal route wins over a ``{param}`` sibling.

    Read from the OpenAPI paths, not ``app.routes``: newer FastAPI hides an
    included router behind a lazy wrapper with no ``path_format``, so walking
    ``app.routes`` finds almost none of them. Websocket routes carry no fetch
    timing and are not in the schema.
    """
    cached = getattr(app.state, "perf_route_patterns", None)
    if cached is None:
        templates = sorted(
            (path for path in app.openapi().get("paths", {}) if path.startswith("/api/")),
            key=lambda path: (path.count("{"), path),
        )
        cached = [
            (path, re.compile("".join(
                "[^/]+" if part.startswith("{") else re.escape(part)
                for part in re.split(r"(\{[^}]+\})", path)
            )))
            for path in templates
        ]
        app.state.perf_route_patterns = cached
    return cached


def _templater(request: Request) -> perf.Templater:
    """Map a raw path to the template of the route that serves it, so only
    route names, never ids or file names, reach the table."""
    patterns = _route_patterns(request.app)

    def template(path: str) -> Optional[str]:
        for route_template, pattern in patterns:
            if pattern.fullmatch(path):
                return route_template
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
