"""Issue #661: the session router's HTTP audit writes must not stall the loop.

#660 threaded the audit writes in this router's **WS proxy** path (see
``test_webapp_ws_proxy_audit_nonblocking.py``). The identical synchronous
``open``/``write``/``close`` on ``webapp/sessions/*.log`` was still called
unthreaded from five HTTP handlers in the same file, so the defect #660 fixed
stayed reachable through a different door.

``POST /api/claude-code/sessions/{sid}/input`` is the one that matters most:
it is the path the Board's dispatch bar and any orchestrator use to drive
sessions, so it fires at message rate against every session under a
multi-worker run. The webapp runs a **single** uvicorn worker
(``app/webapp/event_loop.py``), so a slow write there freezes every other
live session's in-flight WS output — the "terminal opens blank and never
paints" class from #610.

Exercises the real ``session_input`` coroutine directly rather than through
``TestClient``, which runs the ASGI app on a separate portal thread and so
cannot demonstrate same-loop contention — the same reason the #660 test
drives ``_proxy_websocket`` by hand.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from app.webapp.routers import sessions as sessions_router

_SLOW_WRITE_S = 0.3
_TICK_INTERVAL_S = 0.02
_TICK_COUNT = 20


class _FakeRequest:
    """The three things ``session_input`` touches: the config off app state,
    the JSON body, and nothing else."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                webapp_config=SimpleNamespace(session_host_port=8446)
            )
        )
        # maybe_json() only parses a body when the content type says JSON.
        self.headers: dict = {"content-type": "application/json"}
        self.query_params: dict = {}

    async def json(self) -> dict:
        return self._payload


def _slow_session_log(session_id, event, **fields) -> None:
    """Stands in for audit.session_log's blocking open/write/close."""
    time.sleep(_SLOW_WRITE_S)


async def test_slow_audit_write_does_not_stall_the_input_endpoint(monkeypatch):
    monkeypatch.setattr(sessions_router.audit, "session_log", _slow_session_log)
    monkeypatch.setattr(
        sessions_router.session_client, "send_input",
        lambda *a, **kw: {"ok": True},
    )

    tick_gaps: list[float] = []

    async def ticker() -> None:
        last = time.perf_counter()
        for _ in range(_TICK_COUNT):
            await asyncio.sleep(_TICK_INTERVAL_S)
            now = time.perf_counter()
            tick_gaps.append(now - last)
            last = now

    async def post_input() -> None:
        result = await sessions_router.session_input(
            "sid", _FakeRequest({"data": "hello", "submit": True})
        )
        assert result["ok"] is True

    # Both coroutines share one event loop, exactly like a real single-worker
    # uvicorn process serving a dispatch-bar input while another session's WS
    # proxy is pumping output.
    await asyncio.gather(ticker(), post_input())

    max_gap = max(tick_gaps)
    assert max_gap < _SLOW_WRITE_S / 2, (
        f"a slow audit.session_log call stalled the event loop for "
        f"{max_gap:.3f}s (tick interval is {_TICK_INTERVAL_S}s) — the write "
        "is still running synchronously on the loop instead of via _audit"
    )


#: ``audit`` members that only build a path — no I/O, so wrapping them in a
#: thread hop would be pure noise (``board.py`` reads both to locate a
#: session's files).
_PURE_PATH_HELPERS = {"transcript_path", "session_log_path"}


async def test_no_router_writes_audit_on_the_event_loop():
    """AC as an executable check, across **every** router (#674 widened this
    from #661's single file).

    A new handler that writes an audit line unthreaded re-opens the defect
    silently, and it has now re-opened three times in a row through a
    different door each time (#660 the WS path, #661 the rest of
    ``sessions.py``, #674 the four other routers). A grep-shaped assertion
    over the whole package catches the next one at the one place the pattern
    must hold, without needing a load probe per endpoint.
    """
    import re
    from pathlib import Path

    routers_dir = Path(sessions_router.__file__).parent
    # ``audit_mod.`` is matched too (issue #721): ``_helpers.py``'s shared
    # PTY-spawn tail takes the caller's own audit module as a parameter named
    # ``audit_mod`` so per-router monkeypatches still apply, and that alias
    # walked straight past the original ``audit.``-only pattern — the missed
    # call site of the #660/#661/#674 sweep.
    call = re.compile(r"(?<!_)\baudit(?:_mod)?\.(\w+)\s*\(")
    offenders: list[str] = []
    for path in sorted(routers_dir.glob("*.py")):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            match = call.search(line)
            if match and match.group(1) not in _PURE_PATH_HELPERS:
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "audit writes must go through _helpers.audit_off_loop (off the event "
        "loop, #660/#661/#674); found direct call(s):\n"
        + "\n".join(offenders)
    )
