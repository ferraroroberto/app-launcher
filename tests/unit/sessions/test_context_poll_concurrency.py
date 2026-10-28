"""The Chat pane's screen polls under several live sessions at once.

Each ``/context`` and ``/plan-picker`` poll rendered its session's 96 KiB
capture tail through pyte, about 85 ms of pure-Python work that holds the GIL.
The phone polls one session per open pane and the Board's rings poll them all,
so with ten live sessions a round of polls queued ~0.9 s of render work on one
process, and the slow ones showed up in the phone-timing summary as a ~890 ms
server time. An unchanged capture renders to the same screen, so it is
rendered once and served from memory until the file changes.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Dict

import httpx
import pytest

from src.session_view import plan_picker

_SESSIONS = 8
_ROUNDS = 3
_TAIL_BYTES = 120 * 1024


def _live(sid: str) -> Dict[str, object]:
    return {
        "session_id": sid, "kind": "pty", "agent": "claude", "alive": True,
        "rows": 40, "cols": 120, "project_dir": "C:/stub/proj",
    }


def _capture(percent: int) -> str:
    body = "".join(
        f"\x1b[38;5;246m{n:05d} " + "x" * 80 + "\x1b[0m\r\n" for n in range(1400)
    )
    return body + f"  {percent}%c 10%s 79%w | opus | proj (main)\r\n"


@pytest.fixture
def screens(webapp_client, monkeypatch, tmp_path):
    from app.webapp import middleware
    from src import audit

    monkeypatch.setattr(middleware, "LOOPBACK_HOSTS",
                        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}))
    _, app, overrides = webapp_client
    sids = [f"sess{n:04d}" for n in range(_SESSIONS)]
    overrides["session"].list_sessions.return_value = [_live(sid) for sid in sids]
    captures: Dict[str, Path] = {}
    for n, sid in enumerate(sids):
        captures[sid] = tmp_path / f"{sid}.transcript"
        captures[sid].write_text(_capture(10 + n), encoding="utf-8")
    assert captures[sids[0]].stat().st_size > _TAIL_BYTES
    monkeypatch.setattr(audit, "transcript_path", lambda sid: captures[sid])
    renders = []
    real = plan_picker.screen_lines

    def counting(text, rows, cols):
        renders.append(time.perf_counter())
        return real(text, rows, cols)

    monkeypatch.setattr(plan_picker, "screen_lines", counting)
    plan_picker._screens.clear()
    yield app, sids, captures, renders
    plan_picker._screens.clear()


async def _poll_round(client: httpx.AsyncClient, sids, kinds=("context", "plan-picker")):
    return await asyncio.gather(*(
        client.get(f"/api/claude-code/sessions/{sid}/{kind}") for sid in sids for kind in kinds
    ))


def test_concurrent_polls_render_each_unchanged_capture_once(screens):
    app, sids, _captures, renders = screens

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return [await _poll_round(client, sids) for _ in range(_ROUNDS)]

    rounds = asyncio.run(run())

    for answers in rounds:
        assert all(a.status_code == 200 for a in answers)
        contexts = [a.json()["percent"] for a in answers[0::2]]
        assert contexts == [10 + n for n in range(_SESSIONS)]
    assert len(renders) == _SESSIONS, (
        f"{len(renders)} renders for {_SESSIONS} unchanged captures over {_ROUNDS} rounds "
        "of /context + /plan-picker polls: each poll re-rendered the same screen"
    )


def test_a_capture_that_grows_is_rendered_again(screens):
    app, sids, captures, renders = screens
    grown = sids[3]

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await _poll_round(client, sids, kinds=("context",))
            before = len(renders)
            with open(captures[grown], "a", encoding="utf-8", newline="") as fh:
                fh.write("\x1b[2J\x1b[H  55%c 10%s 79%w | opus | proj (main)\r\n")
            answers = await _poll_round(client, sids, kinds=("context",))
            return before, answers

    before, answers = asyncio.run(run())

    assert len(renders) - before == 1
    assert {sid: a.json()["percent"] for sid, a in zip(sids, answers)}[grown] == 55
