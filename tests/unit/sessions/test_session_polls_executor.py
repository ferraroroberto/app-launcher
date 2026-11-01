"""#1605: the Chat pane's per-session polls leave the default executor free
while a transcript read hangs.

A file-system call on a large live PTY transcript stalled for tens of seconds
(#1597). ``/context``, ``/plan-picker``, ``/transcript`` and the Board
drawer's ``/exchange`` each held one of the loop's default-executor threads
for as long as their read stalled, so a few open Chat panes filled the pool,
and ``POST /input`` (the phone's message send) queued behind them. Each test
here hangs one endpoint's read (a fake stalled file), polls it more times
than the default executor has threads, and asserts that a trivial
``asyncio.to_thread`` call and a message send still answer at once.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict

import httpx
import pytest

from app.webapp import hot_poll

_HANG_S = 6.0
_FREE_S = 0.2
_SEND_BOUND_S = 1.0
# More polls than the default executor gets threads below, so pre-#1605 code
# fills it with stalled reads.
_POLLS = 4
_DEFAULT_WORKERS = 2

_LIVE = {
    "session_id": "s1", "kind": "pty", "agent": "claude", "alive": True,
    "rows": 40, "cols": 60, "project_dir": "C:/stub/proj",
}


class _Stall:
    """A read that blocks until released (or :data:`_HANG_S` passes)."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        self.entered.set()
        self.release.wait(_HANG_S)
        return self.answer


def _stall_screen(monkeypatch, _tmp_path) -> _Stall:
    from src.session_view import plan_picker

    stall = _Stall(None)
    monkeypatch.setattr(plan_picker, "read_capture_tail", stall)
    return stall


def _stall_transcript(monkeypatch, tmp_path) -> _Stall:
    from app.webapp.routers import session_transcript

    native = tmp_path / "native.jsonl"
    native.write_text("", encoding="utf-8")
    monkeypatch.setattr(session_transcript, "_resolve_path", lambda *a: (native, "row"))
    stall = _Stall({"entries": [], "reset": False, "size": 0})
    monkeypatch.setattr(session_transcript, "transcript_tail", stall)
    return stall


def _stall_exchange(monkeypatch, _tmp_path) -> _Stall:
    from app.webapp.routers import board as board_router

    stall = _Stall({"available": False, "reason": "no_transcript"})
    monkeypatch.setattr(board_router, "resolve_exchange", stall)
    return stall


_ENDPOINTS: Dict[str, tuple] = {
    "context": ("/api/claude-code/sessions/s1/context", _stall_screen),
    "plan-picker": ("/api/claude-code/sessions/s1/plan-picker", _stall_screen),
    "transcript": ("/api/claude-code/sessions/s1/transcript?after=0&size=0", _stall_transcript),
    "exchange": ("/api/board/sessions/s1/exchange", _stall_exchange),
}


@pytest.fixture
def chat_app(webapp_client, monkeypatch):
    from app.webapp import middleware

    monkeypatch.setattr(middleware.trust, "LOOPBACK_HOSTS",
                        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}))
    _, app, overrides = webapp_client
    session = overrides["session"]
    session.list_sessions.return_value = [_LIVE]
    session.send_input.return_value = {
        "delivered": True, "ingested": True, "submitted": True,
        "submit_confirmed": True, "reason": "ok",
    }
    return app


async def _until(check: Callable[[], bool], timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < end, "the stalled read was never reached"
        await asyncio.sleep(0.01)


@pytest.mark.parametrize("name", list(_ENDPOINTS))
def test_a_stalled_poll_leaves_the_default_executor_and_sends_free(
    chat_app, monkeypatch, tmp_path, name
):
    path, install = _ENDPOINTS[name]
    stall = install(monkeypatch, tmp_path)

    async def run():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=_DEFAULT_WORKERS))
        transport = httpx.ASGITransport(app=chat_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            polls = [asyncio.create_task(client.get(path)) for _ in range(_POLLS)]
            try:
                await _until(stall.entered.is_set)
                await asyncio.sleep(0.1)  # the other polls queue behind the stall
                started = time.perf_counter()
                await asyncio.to_thread(lambda: None)
                free_s = time.perf_counter() - started
                started = time.perf_counter()
                sent = await client.post(
                    "/api/claude-code/sessions/s1/input", json={"data": "hi", "submit": True}
                )
                send_s = time.perf_counter() - started
            finally:
                stall.release.set()
                await asyncio.gather(*polls, return_exceptions=True)
        return free_s, send_s, sent.status_code

    free_s, send_s, status = asyncio.run(run())
    assert free_s < _FREE_S, (
        f"a to_thread call waited {free_s:.2f}s behind stalled {name} polls: "
        "they are holding the shared default thread pool"
    )
    assert status == 200
    assert send_s < _SEND_BOUND_S, f"a message send took {send_s:.2f}s behind stalled {name} polls"


def test_a_stalled_context_poll_answers_stale_and_a_fresh_one_is_unchanged(
    chat_app, monkeypatch, tmp_path
):
    """The first answer is the route's payload exactly; once the read hangs,
    the next poll gets that answer back within the bound, marked stale."""
    from src.session_view import plan_picker

    calls = {"n": 0}
    stall = _Stall(None)

    def read_once_then_hang(path):
        calls["n"] += 1
        return None if calls["n"] == 1 else stall(path)

    monkeypatch.setattr(plan_picker, "read_capture_tail", read_once_then_hang)

    async def run():
        transport = httpx.ASGITransport(app=chat_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = (await client.get("/api/claude-code/sessions/s1/context")).json()
            started = time.perf_counter()
            second = (await client.get("/api/claude-code/sessions/s1/context")).json()
            elapsed = time.perf_counter() - started
            stall.release.set()
        return first, second, elapsed

    first, second, elapsed = asyncio.run(run())
    assert first == {"available": False, "percent": None, "reason": "no_screen"}
    assert elapsed < 2.0, f"the context poll took {elapsed:.1f}s behind a hung read"
    assert second["stale"] is True
    assert second["age_seconds"] >= 0
    assert {k: second[k] for k in first} == first


def test_session_polls_share_a_build_only_for_the_same_request(webapp_client):
    _, app, _ = webapp_client
    a = hot_poll.session_poll(app.state, "GET transcript", "s1", None, 10, 20, 50, None)
    assert hot_poll.session_poll(app.state, "GET transcript", "s1", None, 10, 20, 50, None) is a
    assert hot_poll.session_poll(app.state, "GET transcript", "s1", None, 20, 30, 50, None) is not a
    assert hot_poll.session_poll(app.state, "GET transcript", "s2", None, 10, 20, 50, None) is not a
    assert hot_poll.session_poll(app.state, "GET context", "s1") is not a
    assert a.pool is hot_poll.SESSION_READS_POOL


def test_idle_session_polls_are_dropped_past_the_cap(webapp_client, monkeypatch):
    _, app, _ = webapp_client
    monkeypatch.setattr(hot_poll, "SESSION_POLLS_MAX", 3)
    for n in range(3):
        hot_poll.session_poll(app.state, "GET transcript", "s1", n)
    hot_poll.session_poll(app.state, "GET transcript", "s1", 99)
    assert list(app.state.session_polls) == [("GET transcript", "s1", 99)]


def test_a_session_poll_builds_off_the_default_executor():
    poll = hot_poll.HotPoll("test", pool=hot_poll.SESSION_READS_POOL, mark_fresh=False)

    async def build(steps):
        return {"thread": await steps.run("name", lambda: threading.current_thread().name)}

    body = asyncio.run(poll.get(build))
    assert body == {"thread": body["thread"]}, "a fresh answer carries no stale marker"
    assert body["thread"].startswith("session-reads")
