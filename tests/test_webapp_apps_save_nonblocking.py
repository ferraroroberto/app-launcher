"""Issue #881: ``POST /api/apps/save`` must not walk the scan root on the loop.

``save_apps`` re-runs ``discover_new`` — the full recursive
``apps_scan_root`` walk — to resolve the ids the user ticked. ``scan_apps``
fifteen lines above it already offloads the identical call to a worker
thread; ``save_apps`` ran it inline, so tapping Save in the Apps tab froze
every live PTY WebSocket pump and the 4 s ``/api/apps`` poll for the length
of the tree walk. The webapp runs a **single** uvicorn worker
(``app/webapp/event_loop.py``), so there is no other loop to absorb it.

Drives the real ``save_apps`` coroutine directly rather than through
``TestClient``, which runs the ASGI app on a separate portal thread and so
cannot demonstrate same-loop contention — the same reason
``test_webapp_session_input_audit_nonblocking.py`` does.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from app.webapp.routers import apps as apps_router
from src.registry import Registry

_SLOW_WALK_S = 0.3
_TICK_INTERVAL_S = 0.02
_TICK_COUNT = 20


class _FakeRequest:
    """What ``save_apps`` touches: the config off app state and the body."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                webapp_config=SimpleNamespace(apps_scan_root="C:\\stub")
            )
        )
        # maybe_json() only parses a body when the content type says JSON.
        self.headers: dict = {"content-type": "application/json"}
        self.query_params: dict = {}

    async def json(self) -> dict:
        return self._payload


def _slow_discover_new(**_kwargs) -> list:
    """Stands in for the blocking apps_scan_root tree walk."""
    time.sleep(_SLOW_WALK_S)
    return []


async def test_slow_scan_walk_does_not_stall_the_save_endpoint(monkeypatch):
    monkeypatch.setattr(apps_router, "discover_new", _slow_discover_new)
    monkeypatch.setattr(
        apps_router, "load_registry", lambda: Registry(scan_root="C:\\stub", apps=[])
    )
    monkeypatch.setattr(apps_router, "persist_additions", lambda *a, **kw: [])

    tick_gaps: list[float] = []

    async def ticker() -> None:
        last = time.perf_counter()
        for _ in range(_TICK_COUNT):
            await asyncio.sleep(_TICK_INTERVAL_S)
            now = time.perf_counter()
            tick_gaps.append(now - last)
            last = now

    async def post_save() -> None:
        result = await apps_router.save_apps(_FakeRequest({"ids": ["freshapp"]}))
        assert result == {"added": []}

    # One shared loop, exactly like the single-worker uvicorn process serving
    # a Save tap while a live session's WS proxy is pumping output.
    await asyncio.gather(ticker(), post_save())

    max_gap = max(tick_gaps)
    assert max_gap < _SLOW_WALK_S / 2, (
        f"a slow discover_new walk stalled the event loop for {max_gap:.3f}s "
        f"(tick interval is {_TICK_INTERVAL_S}s) — save_apps is still running "
        "the scan synchronously instead of via asyncio.to_thread"
    )
