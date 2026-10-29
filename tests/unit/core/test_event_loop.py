"""Selector event-loop shim (issue #388) — root cause of the #386 wedge.

asyncio's default Windows proactor event loop closes its listening socket
on any aborted client connection (WinError 64); the selector loop's accept
path doesn't. These tests cover the wiring (every launch of
``app.webapp.server:app`` goes through ``app.webapp.serve``, which runs on the
shim) and the actual accept-loop resilience the shim buys.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from app.webapp.event_loop import (
    GIL_SWITCH_INTERVAL_S,
    selector_loop_factory,
    tune_gil_switch_interval,
)
from app.webapp.manager import WebappManager, WebappManagerConfig

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def test_selector_loop_factory_returns_selector_instance_on_win32(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    sentinel = object()
    monkeypatch.setattr(asyncio, "SelectorEventLoop", lambda: sentinel)
    assert selector_loop_factory() is sentinel


def test_selector_loop_factory_defers_on_other_platforms(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    sentinel = object()
    monkeypatch.setattr(asyncio, "new_event_loop", lambda: sentinel)
    assert selector_loop_factory() is sentinel


def test_selector_loop_factory_is_zero_arg_and_returns_an_instance():
    """Regression pin: ``asyncio.Runner(loop_factory=...)`` calls it as a bare
    Callable[[], AbstractEventLoop], and it must return an instantiated loop,
    not a loop class (#388's original bug: returning the class left Runner
    calling unbound methods)."""
    loop = selector_loop_factory()
    try:
        assert isinstance(loop, asyncio.AbstractEventLoop)
    finally:
        loop.close()


@pytest.fixture
def default_switch_interval():
    """Run with CPython's stock 5 ms switch interval, restoring it after."""
    saved = sys.getswitchinterval()
    sys.setswitchinterval(0.005)
    try:
        yield
    finally:
        sys.setswitchinterval(saved)


def test_loop_factory_lowers_the_gil_switch_interval(default_switch_interval):
    """#1492: the factory is the one hook every webapp launch path shares, so
    it carries the GIL tuning that keeps a pyte render from stalling the loop."""
    loop = selector_loop_factory()
    try:
        assert sys.getswitchinterval() == pytest.approx(GIL_SWITCH_INTERVAL_S)
    finally:
        loop.close()


def test_tune_never_raises_an_interval_already_lower(default_switch_interval):
    sys.setswitchinterval(0.0001)
    assert tune_gil_switch_interval() == pytest.approx(0.0001)


# Pure-Python CPU-bound threads stand in for the Board/exchange ``pyte``
# renders (~0.85 s of GIL-holding CPU per call). At the default 5 ms switch
# interval one request's thread hops waited out the whole render: measured
# 6.3-6.9 s for a ~15 ms ``GET /api/board`` beside three of them on a test
# instance, 15-48 s on the live one (#1492).
_HOG_SECONDS = 2.0
_HOPS = 8
_HOP_BUDGET_S = 0.75


def _busy_for(seconds: float) -> None:
    end = time.perf_counter() + seconds
    acc = 0
    while time.perf_counter() < end:
        for i in range(200):
            acc += i * i


def _request_like() -> None:
    for _ in range(40):
        Path(__file__).stat()


async def _hops_beside_hogs() -> float:
    hogs = [asyncio.create_task(asyncio.to_thread(_busy_for, _HOG_SECONDS)) for _ in range(3)]
    await asyncio.sleep(0.1)  # let all three threads take the GIL
    started = time.perf_counter()
    for _ in range(_HOPS):
        await asyncio.to_thread(_request_like)
    elapsed = time.perf_counter() - started
    await asyncio.gather(*hogs)
    return elapsed


def test_request_hops_stay_fast_beside_cpu_bound_threads(default_switch_interval):
    """Pins the bound: the loop factory is what keeps a request's thread hops
    under ``_HOP_BUDGET_S`` while three CPU-bound threads hold the GIL. Fails
    against the pre-fix factory (the hops take ~the full ``_HOG_SECONDS``)."""
    runner = asyncio.Runner(loop_factory=selector_loop_factory)
    try:
        elapsed = runner.run(_hops_beside_hogs())
    finally:
        runner.close()
    assert elapsed < _HOP_BUDGET_S, (
        f"{_HOPS} thread hops took {elapsed:.2f}s beside CPU-bound threads "
        f"(budget {_HOP_BUDGET_S}s) — the GIL switch interval is not tuned"
    )


SERVE_ARGV = ["-m", "app.webapp.serve"]


def test_manager_build_command_launches_serve():
    mgr = WebappManager(WebappManagerConfig(port=18445))
    cmd = mgr._build_command()
    assert cmd[1:3] == SERVE_ARGV
    assert "uvicorn" not in cmd


def test_webapp_cmd_runs_serve(monkeypatch):
    import argparse

    from app.cli.commands.webapp_cmd import WebappCommand

    captured = {}

    def fake_run(host, port, **kwargs):
        captured.update(host=host, port=port, **kwargs)

    monkeypatch.setattr("app.webapp.serve.run", fake_run)
    monkeypatch.setattr(
        "app.webapp.manager.cert_paths", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "src.webapp_config.load_webapp_config",
        lambda: type("Cfg", (), {"host": "127.0.0.1", "port": 18445})(),
    )

    WebappCommand(config=None).execute(argparse.Namespace(host=None, port=None))
    assert captured == {"host": "127.0.0.1", "port": 18445, "log_level": "info"}


@pytest.mark.parametrize(
    "relpath",
    ["tests/e2e/fixtures/autoboot.py", "scripts/design_review_synthetic.py"],
)
def test_disposable_webapp_spawns_launch_serve(relpath):
    """The e2e autoboot and the design-review boot spawn the webapp inside
    modules with subprocess side effects, so a static check of their argv
    catches drift: each launches ``app.webapp.serve``, never uvicorn."""
    src = (_REPO_ROOT / relpath).read_text(encoding="utf-8")
    assert re.search(r'"-m",\s*"app\.webapp\.serve"', src), relpath
    assert '"uvicorn"' not in src, relpath


def test_webapp_bat_launches_serve():
    src = (_REPO_ROOT / "webapp.bat").read_text(encoding="utf-8")
    assert src.count("-m app.webapp.serve") == 2
    assert "uvicorn" not in src


def test_documented_webapp_commands_launch_serve():
    """A command a reader copies is a spawn site too (#1008). A documented
    ``uvicorn app.webapp.server:app`` would skip the selector loop, HTTP/2
    and the loopback-only proxy-header trust that ``app.webapp.serve`` owns."""
    docs = [_REPO_ROOT / "README.md", *sorted((_REPO_ROOT / "docs").glob("*.md"))]
    offenders = []
    for doc in docs:
        for lineno, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            if "uvicorn app.webapp.server:app" in line:
                offenders.append(f"{doc.name}:{lineno}")
    assert not offenders, (
        "these documented commands bypass app.webapp.serve: "
        f"{offenders}"
    )


def test_named_tunnel_script_builds_the_managers_argv(monkeypatch):
    """The documented no-tray path (``webapp_tunnel_named.bat``) once composed
    its own argv and drifted from the app's (#1003, #1007). Unlike conftest.py
    this script imports cleanly, so pin the real argv rather than its source
    text."""
    spec = importlib.util.spec_from_file_location(
        "_rnt_under_test", _REPO_ROOT / "scripts" / "run_named_tunnel.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return object()

    # Keep the cert-renewal probe and the spawn itself off the real system.
    monkeypatch.setattr(module, "check_tailscale_cert", lambda: None)
    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    module._spawn_uvicorn(18445)

    cmd = captured["cmd"]
    assert cmd[1:3] == SERVE_ARGV
    expected = WebappManager(
        WebappManagerConfig(host="127.0.0.1", port=18445)
    )._build_command()
    assert cmd == expected, (
        "named-tunnel argv diverged from WebappManager._build_command() "
        f"(#1003):\n  script: {cmd}\n  app:    {expected}"
    )


async def _noop_handler(reader, writer):
    writer.close()


def _abort_connect_sync(port: int) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
    try:
        s.settimeout(0.5)
        s.connect(("127.0.0.1", port))
    except OSError:
        pass
    finally:
        s.close()  # SO_LINGER(1, 0) forces an RST instead of a FIN


async def _still_accepting(port: int) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=1.0
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def _bombard_with_aborts(rounds: int, burst: int) -> None:
    server = await asyncio.start_server(_noop_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        for _ in range(rounds):
            threads = [
                threading.Thread(target=_abort_connect_sync, args=(port,))
                for _ in range(burst)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            await asyncio.sleep(0.02)
            assert await _still_accepting(
                port
            ), "listener died on an aborted client connection (issue #388)"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.skipif(sys.platform != "win32", reason="proactor-loop bug is Windows-only")
def test_selector_loop_survives_aborted_connections():
    asyncio.run(_bombard_with_aborts(rounds=10, burst=20), loop_factory=asyncio.SelectorEventLoop)


@pytest.mark.skipif(sys.platform != "win32", reason="proactor-loop bug is Windows-only")
def test_proactor_loop_dies_on_aborted_connections():
    """Documents the bug this issue fixes — the shim exists because this
    fails. If a future CPython release fixes the proactor loop
    itself, this test (not the shim) is what should be revisited."""
    with pytest.raises(AssertionError):
        asyncio.run(
            _bombard_with_aborts(rounds=10, burst=20), loop_factory=asyncio.ProactorEventLoop
        )
