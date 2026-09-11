"""Issue #881: the Jobs router must not decorate a job on the event loop.

``_decorate_job`` shells out to ``schtasks`` and walks the job's run history
on disk. ``GET /api/jobs`` already offloads it "so the event loop doesn't
block", but ``create_job``, ``edit_job``, ``pause`` and ``resume`` each
called it inline to build their response, stalling every live PTY WebSocket
pump on the single-worker webapp (``app/webapp/event_loop.py``) for the
length of that query.

The behavioural test drives the real ``pause`` coroutine directly rather
than through ``TestClient``, which runs the ASGI app on a separate portal
thread and so cannot demonstrate same-loop contention (same reason as
``test_webapp_session_input_audit_nonblocking.py``). The structural test
pins all four handlers at once, without each one's preflight/registry
setup.
"""

from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

from app.webapp.routers import jobs as jobs_router

_SLOW_DECORATE_S = 0.3
_TICK_INTERVAL_S = 0.02
_TICK_COUNT = 20


def _slow_decorate_job(job, run_counts=None) -> dict:
    """Stands in for the schtasks query + run-history walk."""
    time.sleep(_SLOW_DECORATE_S)
    return {"id": job.id}


async def test_slow_decoration_does_not_stall_the_pause_endpoint(monkeypatch):
    job = SimpleNamespace(id="demo", elevated=False, session_less=False)
    monkeypatch.setattr(jobs_router, "_decorate_job", _slow_decorate_job)
    monkeypatch.setattr(jobs_router, "load_jobs", lambda: SimpleNamespace(jobs=[job]))
    monkeypatch.setattr(jobs_router, "get_by_id", lambda cfg, job_id: job)
    monkeypatch.setattr(jobs_router, "pause_job", lambda cfg, job_id: job)
    monkeypatch.setattr(jobs_router.jobs_mod, "sync_schtasks", lambda j: [])

    tick_gaps: list[float] = []

    async def ticker() -> None:
        last = time.perf_counter()
        for _ in range(_TICK_COUNT):
            await asyncio.sleep(_TICK_INTERVAL_S)
            now = time.perf_counter()
            tick_gaps.append(now - last)
            last = now

    async def post_pause() -> None:
        result = await jobs_router.pause("demo")
        assert result == {"job": {"id": "demo"}}

    await asyncio.gather(ticker(), post_pause())

    max_gap = max(tick_gaps)
    assert max_gap < _SLOW_DECORATE_S / 2, (
        f"a slow _decorate_job stalled the event loop for {max_gap:.3f}s "
        f"(tick interval is {_TICK_INTERVAL_S}s) — pause is still decorating "
        "synchronously instead of via asyncio.to_thread"
    )


def test_no_handler_decorates_a_job_on_the_event_loop():
    """Every ``_decorate_job(...)`` call in the router must be deferred into a
    worker thread: passed by reference to ``asyncio.to_thread`` (no call node
    at all), or called inside a lambda that ``to_thread`` runs (``get_jobs``'s
    list comprehension). A bare call anywhere else runs on the loop.
    """
    source = Path(jobs_router.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    parents: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_decorate_job"
        ):
            continue
        ancestor = parents.get(node)
        while ancestor is not None and not isinstance(ancestor, ast.Lambda):
            ancestor = parents.get(ancestor)
        if ancestor is None:
            offenders.append(f"jobs.py:{node.lineno}: {ast.get_source_segment(source, node)}")

    assert offenders == [], (
        "_decorate_job blocks (schtasks + disk walk) and must run via "
        "asyncio.to_thread; found direct call(s) on the event loop:\n"
        + "\n".join(offenders)
    )
