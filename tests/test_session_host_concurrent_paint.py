"""Issue #610 AC1 — five-concurrent-session load probe for "terminal opens
blank and never paints".

#610 was filed off a phone screenshot taken during a five-worker
orchestration run: one session's terminal painted nothing at all. Three
mitigations shipped (#624's client-side "no response yet" watchdog, #639's
``POST /sessions`` offload, #660's WS-proxy audit-write offload), but AC1 —
*"a reproduction, or an explicit statement that it could not be reproduced
under a five-session load with the steps that were tried"* — stayed open
across all three, because every one of them was proven with a synthetic
single-call probe rather than a real concurrent load.

This is that missing load test, and the steps it tries are the ones the
original report describes:

* a **real** session-host ASGI app on a **real** single uvicorn event loop
  (the whole failure class is same-loop contention, so a ``TestClient`` —
  which runs the app on a separate portal thread — cannot show it);
* five **real** ConPTY spawns issued as a concurrent burst through the real
  ``POST /sessions`` handler;
* five **real** WebSocket clients, each attaching the instant its own create
  returns, so late creates in the burst race already-attached pumps exactly
  as they did in the reported run;
* a per-session unique sentinel, so "painted" means *that session's own*
  bytes arrived — the assertion covers the cross-wiring failure mode from
  the issue's follow-up comment (a session's exchange returning another
  session's content), not just emptiness.

The child (``_pty_paint_child.py``) prints its sentinel immediately instead
of being a real agent: a 3-5 s ``claude`` startup would swamp the very
latency this measures, and the transport under test is agent-agnostic.

An event-loop ticker runs alongside the burst. It does not gate the test —
the user-visible contract is "every terminal paints its own first frame" —
but its worst observed gap is reported in every failure message, because
that number is what distinguishes a loop-contention regression (the #639 /
#660 class) from a spawn or transport failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import connect as ws_connect

from src import audit, session_host as session_host_lib
from src.session_host import PtyProcess

_CHILD = Path(__file__).parent / "_pty_paint_child.py"

pytestmark = [
    pytest.mark.skipif(
        PtyProcess is None, reason="pywinpty (Windows ConPTY) is required"
    ),
    # PtyProcess.spawn re-tokenises its command string with shlex, which
    # mangles quoted Windows paths — so the interpreter and child paths must
    # be space-free. True for a normal checkout; skip loudly rather than fail
    # cryptically for someone who cloned under e.g. C:\My Projects\.
    pytest.mark.skipif(
        " " in str(_CHILD) or " " in sys.executable,
        reason="checkout or interpreter path contains a space (shlex-hostile)",
    ),
]

# Five concurrent sessions — the load the issue reports ("five concurrent PTY
# workers plus chief"), not a round number picked for symmetry.
_SESSION_COUNT = 5
# Generous: a ConPTY spawn plus a Python interpreter start is ~0.5 s on an
# idle box, and this must not go red merely because the machine is busy. The
# symptom under investigation is "never paints", so a deadline an order of
# magnitude above the expected latency still catches it.
_PAINT_DEADLINE_S = 25.0
_TICK_INTERVAL_S = 0.02


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _create_and_attach(
    client: httpx.AsyncClient, port: int, index: int, project_dir: str
) -> dict[str, Any]:
    """Spawn one session and attach a WS to it the moment it exists.

    Returns a result row: whether this session's own sentinel arrived, how
    long it took, and whatever text did arrive (so a failure names what the
    terminal actually showed instead of just "nothing").
    """
    tag = f"probe{index}"
    sentinel = f"<<<PAINT:{tag}>>>"
    started = time.perf_counter()
    resp = await client.post(
        f"http://127.0.0.1:{port}/sessions",
        json={
            "project_dir": project_dir,
            "name": f"probe-{index}",
            # The monkeypatched command_for() turns flags into the child's
            # argv, so this doubles as the session's unique tag.
            "flags": tag,
            "agent": "claude",
            "rows": 40,
            "cols": 120,
        },
        timeout=_PAINT_DEADLINE_S,
    )
    resp.raise_for_status()
    sid = str(resp.json()["session_id"])
    created = time.perf_counter()

    buffer = ""
    painted_at: float | None = None
    url = f"ws://127.0.0.1:{port}/sessions/{sid}/ws?role=phone"
    async with ws_connect(url) as websocket:
        deadline = created + _PAINT_DEADLINE_S
        while painted_at is None:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            try:
                buffer += await asyncio.wait_for(websocket.recv(), timeout=remaining)
            except (asyncio.TimeoutError, TimeoutError):
                break
            if sentinel in buffer:
                painted_at = time.perf_counter()
    return {
        "index": index,
        "tag": tag,
        "sentinel": sentinel,
        "session_id": sid,
        "spawn_s": created - started,
        "paint_s": None if painted_at is None else painted_at - created,
        "buffer": buffer,
    }


async def test_five_concurrent_sessions_each_paint_their_own_first_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.session_host import server as server_mod

    # Transcripts and audit logs land in tmp, never in the repo's
    # webapp/sessions/ (which the live tray is using).
    monkeypatch.setattr(audit, "_SESSIONS_DIR", tmp_path / "sessions")
    # Run the deterministic child instead of the real `claude` CLI. Bare
    # (unquoted) paths: PtyProcess.spawn re-tokenises the command string with
    # shlex, which mangles quoted Windows paths — the same constraint
    # test_session_host_pty_realpty.py works within. Both paths are
    # space-free here.
    monkeypatch.setattr(
        session_host_lib,
        "command_for",
        lambda agent: f"{sys.executable} {_CHILD}",
    )

    port = _free_tcp_port()
    config = uvicorn.Config(
        server_mod.create_app(),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())

    tick_gaps: list[float] = []
    stop_ticking = asyncio.Event()

    async def ticker() -> None:
        """Measures how badly the burst starves the shared event loop."""
        last = time.perf_counter()
        while not stop_ticking.is_set():
            await asyncio.sleep(_TICK_INTERVAL_S)
            now = time.perf_counter()
            tick_gaps.append(now - last)
            last = now

    tick_task = asyncio.create_task(ticker())
    results: list[dict[str, Any]] = []
    try:
        for _ in range(400):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started, "the session-host under test never came up"

        async with httpx.AsyncClient() as client:
            # One coroutine per session, all launched together: each attaches
            # its WS as soon as its own create returns, so the later spawns in
            # the burst overlap the earlier sessions' live pumps.
            results = list(
                await asyncio.gather(
                    *(
                        _create_and_attach(client, port, i, str(tmp_path))
                        for i in range(_SESSION_COUNT)
                    )
                )
            )
    finally:
        stop_ticking.set()
        tick_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tick_task
        server.should_exit = True
        # The lifespan's finally force-kills every PTY the probe spawned.
        # Suppressed: a slow shutdown must not raise out of `finally` and
        # mask the assertion failure this test exists to report.
        with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
            await asyncio.wait_for(serve_task, timeout=30)

    worst_gap = max(tick_gaps) if tick_gaps else 0.0
    diagnosis = (
        f"worst event-loop tick gap during the burst: {worst_gap * 1000:.0f} ms "
        f"(interval {_TICK_INTERVAL_S * 1000:.0f} ms); "
        "per-session spawn/paint: "
        + ", ".join(
            f"#{r['index']} spawn={r['spawn_s'] * 1000:.0f}ms paint="
            + ("never" if r["paint_s"] is None else f"{r['paint_s'] * 1000:.0f}ms")
            for r in results
        )
    )

    # The measurement is the point of this test, not a side effect: AC1 asks
    # for what a five-session load actually does, so the numbers go to stdout
    # (and to the gate's progress log) on a pass, not only on a failure.
    print(f"\n[#610 five-session load probe] {diagnosis}")

    blank = [r for r in results if r["paint_s"] is None]
    assert not blank, (
        f"{len(blank)} of {_SESSION_COUNT} concurrently-spawned sessions never "
        f"painted within {_PAINT_DEADLINE_S:.0f}s — the #610 symptom reproduced. "
        "Blank sessions: "
        + ", ".join(f"#{r['index']} (got {r['buffer']!r})" for r in blank)
        + f". {diagnosis}"
    )

    # Cross-wiring: a session must never receive another session's output
    # (#537's failure mode, re-reported on #610's exchange endpoint).
    for row in results:
        foreign = [
            other["sentinel"]
            for other in results
            if other["index"] != row["index"] and other["sentinel"] in row["buffer"]
        ]
        assert not foreign, (
            f"session #{row['index']} ({row['session_id'][:8]}) received another "
            f"session's output {foreign} — terminals are cross-wired under a "
            f"{_SESSION_COUNT}-session burst. {diagnosis}"
        )
