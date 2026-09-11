"""Issue #881: ``GET /api/board`` must not merge session cards on the loop.

``get_board`` thread-offloads its five cheaper inputs through one
``asyncio.gather``, then called ``board.merge_sessions`` inline — even though
that does real blocking file IO per card (a transcript stat plus bounded tail
reads and a ``json.loads`` of every line for each waiting card). The cost is
unbounded in session count and repeats every 5s while the Board tab is open,
on a **single**-worker webapp (``app/webapp/event_loop.py``) whose loop also
pumps every live PTY WebSocket.

Drives the real ``get_board`` coroutine directly rather than through
``TestClient``, which runs the ASGI app on a separate portal thread and so
cannot demonstrate same-loop contention (same reason as
``test_webapp_session_input_audit_nonblocking.py``). Every input is stubbed;
only ``merge_sessions`` is slow.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from app.webapp.routers import board as board_router

_SLOW_MERGE_S = 0.3
_TICK_INTERVAL_S = 0.02
_TICK_COUNT = 20


def _slow_merge_sessions(live, state_rows, **_kwargs) -> list:
    """Stands in for the per-card transcript stat + tail reads."""
    time.sleep(_SLOW_MERGE_S)
    return []


def _no_rows(_path) -> dict:
    return {"available": False, "stale": False, "updated_at": None, "rows": {}}


async def test_slow_session_merge_does_not_stall_the_board_poll(monkeypatch, tmp_path):
    monkeypatch.setattr(board_router.board, "merge_sessions", _slow_merge_sessions)
    monkeypatch.setattr(board_router, "_safe_list_sessions", lambda port: [])
    monkeypatch.setattr(board_router.board, "read_sessions_state", _no_rows)
    monkeypatch.setattr(board_router.board, "read_active_issues", _no_rows)
    monkeypatch.setattr(board_router.board, "jobs_attention", lambda: [])
    monkeypatch.setattr(board_router, "_read_quota_lines", lambda cfg: [])
    monkeypatch.setattr(board_router, "_refresh_codex_for_lines", lambda cfg, lines: None)
    monkeypatch.setattr(
        board_router.board_chief, "_reconcile_chief_labels", lambda live, rows: live
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                webapp_config=SimpleNamespace(
                    sessions_state_file=str(tmp_path / "sessions-state.json"),
                    session_host_port=8446,
                )
            )
        )
    )

    tick_gaps: list[float] = []

    async def ticker() -> None:
        last = time.perf_counter()
        for _ in range(_TICK_COUNT):
            await asyncio.sleep(_TICK_INTERVAL_S)
            now = time.perf_counter()
            tick_gaps.append(now - last)
            last = now

    async def poll_board() -> None:
        body = await board_router.get_board(request)
        assert set(body["columns"]) == {
            "backlog", "claude_turn", "your_turn", "other", "done",
        }

    # One shared loop, exactly like the single-worker uvicorn process serving
    # the Board's 5s poll while a live session's WS proxy is pumping output.
    await asyncio.gather(ticker(), poll_board())

    max_gap = max(tick_gaps)
    assert max_gap < _SLOW_MERGE_S / 2, (
        f"a slow merge_sessions stalled the event loop for {max_gap:.3f}s "
        f"(tick interval is {_TICK_INTERVAL_S}s) — get_board is still merging "
        "session cards synchronously instead of via asyncio.to_thread"
    )
