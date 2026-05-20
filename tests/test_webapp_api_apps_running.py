"""/api/apps/running surface — launcher-spawned app tracking (issue #35).

In-process FastAPI ``TestClient``, same pattern as
``test_webapp_api_apps.py``. ``psutil`` is faked in both
``src.app_runtime`` and ``src.diagnostics`` — no real processes spawn.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Iterator, List, Optional

import pytest

from src import app_runtime
from src.diagnostics import listening_port_for_pid_tree


# --------------------------------------------------------------- fake psutil


class _NoSuchProcess(Exception):
    pass


class _AccessDenied(Exception):
    pass


class _TimeoutExpired(Exception):
    pass


class FakeProc:
    """Minimal stand-in for ``psutil.Process``."""

    def __init__(
        self,
        pid: int,
        *,
        running: bool = True,
        create_time: float = 0.0,
        children: Optional[List["FakeProc"]] = None,
        listen_port: Optional[int] = None,
    ) -> None:
        self.pid = pid
        self._running = running
        self._create_time = create_time
        self._children = children or []
        self._listen_port = listen_port
        self.terminated = False
        self.killed = False

    def is_running(self) -> bool:
        return self._running

    def create_time(self) -> float:
        return self._create_time

    def children(self, recursive: bool = False) -> List["FakeProc"]:
        return list(self._children)

    def net_connections(self, kind: str = "inet"):
        if self._listen_port is None:
            return []
        laddr = SimpleNamespace(ip="0.0.0.0", port=self._listen_port)
        return [SimpleNamespace(status="LISTEN", laddr=laddr, pid=self.pid)]

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class FakePsutil:
    """Configurable fake exposing the psutil surface the code touches."""

    NoSuchProcess = _NoSuchProcess
    AccessDenied = _AccessDenied
    TimeoutExpired = _TimeoutExpired
    CONN_LISTEN = "LISTEN"

    def __init__(self) -> None:
        self._procs: dict[int, FakeProc] = {}
        self._exists: set[int] = set()

    def register(self, proc: FakeProc, exists: bool = True) -> FakeProc:
        self._procs[proc.pid] = proc
        if exists:
            self._exists.add(proc.pid)
        return proc

    def pid_exists(self, pid: int) -> bool:
        return pid in self._exists

    def Process(self, pid: int) -> FakeProc:
        if pid not in self._procs:
            raise _NoSuchProcess(pid)
        return self._procs[pid]

    def wait_procs(self, procs, timeout=None):
        return list(procs), []


@pytest.fixture
def fake_psutil(monkeypatch) -> Iterator[FakePsutil]:
    """Swap a fresh FakePsutil into both modules; clear the tracker."""
    fake = FakePsutil()
    monkeypatch.setattr(app_runtime, "psutil", fake)
    import src.diagnostics as diagnostics_mod

    monkeypatch.setattr(diagnostics_mod, "psutil", fake)
    # The spawn tracker is module-level — reset it so tests don't bleed.
    app_runtime._instances.clear()
    yield fake
    app_runtime._instances.clear()


def _alive_proc(fake: FakePsutil, inst, **kw) -> FakeProc:
    """Register a FakeProc that ``prune_dead`` will treat as alive."""
    proc = FakeProc(inst.pid, create_time=inst.started_at, **kw)
    return fake.register(proc)


# --------------------------------------------------------------- GET /running


class TestGetRunningApps:
    def test_empty_when_nothing_launched(self, webapp_client, fake_psutil):
        client, _, _ = webapp_client
        resp = client.get("/api/apps/running")
        assert resp.status_code == 200
        assert resp.json() == {"running": []}

    def test_row_shape_with_no_listener(self, webapp_client, fake_psutil):
        client, app, _ = webapp_client
        app.state.app_config.tailnet_host = "pc.example-tailnet.ts.net"
        inst = app_runtime.record_spawn(
            "voice-transcriber-webapp", "Voice Transcriber", "webapp", 12345
        )
        _alive_proc(fake_psutil, inst)  # alive, but binds no port

        resp = client.get("/api/apps/running")
        assert resp.status_code == 200
        running = resp.json()["running"]
        assert len(running) == 1
        row = running[0]
        assert row["app_id"] == "voice-transcriber-webapp"
        assert row["name"] == "Voice Transcriber"
        assert row["kind"] == "webapp"
        assert row["pid"] == 12345
        assert row["alive"] is True
        assert row["port"] is None
        assert row["url"] is None
        assert isinstance(row["started_at"], int)

    def test_resolves_port_and_https_url(
        self, webapp_client, fake_psutil, monkeypatch
    ):
        client, app, _ = webapp_client
        from app.webapp.routers import apps as apps_router

        # An HTTPS sibling — the scheme probe sees a TLS handshake.
        monkeypatch.setattr(apps_router, "detect_local_scheme", lambda _p: "https")
        app.state.app_config.tailnet_host = "pc.example-tailnet.ts.net"
        inst = app_runtime.record_spawn("vt", "Voice Transcriber", "webapp", 100)
        # The bat's python descendant owns the socket, not the root cmd.
        child = FakeProc(200, listen_port=8501)
        _alive_proc(fake_psutil, inst, children=[child])

        resp = client.get("/api/apps/running")
        row = resp.json()["running"][0]
        assert row["port"] == 8501
        assert row["url"] == "https://pc.example-tailnet.ts.net:8501/"

    def test_resolves_http_url_for_plain_app(
        self, webapp_client, fake_psutil, monkeypatch
    ):
        client, app, _ = webapp_client
        from app.webapp.routers import apps as apps_router

        # A plain-HTTP Streamlit app — the TLS handshake fails.
        monkeypatch.setattr(apps_router, "detect_local_scheme", lambda _p: "http")
        app.state.app_config.tailnet_host = "pc.example-tailnet.ts.net"
        inst = app_runtime.record_spawn("rep", "Reporting", "streamlit", 100)
        child = FakeProc(200, listen_port=8501)
        _alive_proc(fake_psutil, inst, children=[child])

        resp = client.get("/api/apps/running")
        row = resp.json()["running"][0]
        assert row["url"] == "http://pc.example-tailnet.ts.net:8501/"

    def test_url_null_when_tailnet_host_unset(self, webapp_client, fake_psutil):
        client, app, _ = webapp_client
        app.state.app_config.tailnet_host = ""
        inst = app_runtime.record_spawn("vt", "Voice Transcriber", "webapp", 100)
        child = FakeProc(200, listen_port=8501)
        _alive_proc(fake_psutil, inst, children=[child])

        resp = client.get("/api/apps/running")
        row = resp.json()["running"][0]
        assert row["port"] == 8501
        assert row["url"] is None

    def test_prune_removes_dead_pid(self, webapp_client, fake_psutil):
        client, _, _ = webapp_client
        inst = app_runtime.record_spawn("vt", "Voice Transcriber", "webapp", 100)
        # PID no longer exists — register nothing / mark not-exists.
        fake_psutil.register(
            FakeProc(100, create_time=inst.started_at), exists=False
        )
        resp = client.get("/api/apps/running")
        assert resp.json() == {"running": []}

    def test_prune_removes_pid_reuse(self, webapp_client, fake_psutil):
        client, _, _ = webapp_client
        inst = app_runtime.record_spawn("vt", "Voice Transcriber", "webapp", 100)
        # PID exists but create_time is far from started_at → reused PID.
        fake_psutil.register(
            FakeProc(100, create_time=inst.started_at - 500.0)
        )
        resp = client.get("/api/apps/running")
        assert resp.json() == {"running": []}


# --------------------------------------------------------------- POST /stop


class TestStopAppInstance:
    def test_stop_tracked_instance(self, webapp_client, fake_psutil, monkeypatch):
        client, _, _ = webapp_client
        from app.webapp.routers import apps as apps_router

        called: list[int] = []
        monkeypatch.setattr(
            apps_router, "kill_process_tree", lambda pid: called.append(pid)
        )
        app_runtime.record_spawn("vt", "Voice Transcriber", "webapp", 4242)

        resp = client.post("/api/apps/vt/instances/4242/stop")
        assert resp.status_code == 200
        assert resp.json() == {"stopped": 4242}
        assert called == [4242]
        assert not app_runtime.is_tracked("vt", 4242)

    def test_stop_untracked_returns_404(self, webapp_client, fake_psutil):
        client, _, _ = webapp_client
        resp = client.post("/api/apps/ghost/instances/9999/stop")
        assert resp.status_code == 404


# --------------------------------------------------------------- auth gate


class TestAuth:
    def test_running_requires_token(self, webapp_client, fake_psutil):
        client, app, _ = webapp_client
        app.state.webapp_config.auth_token = "secret-token"
        assert client.get("/api/apps/running").status_code == 401
        ok = client.get(
            "/api/apps/running",
            headers={"Authorization": "Bearer secret-token"},
        )
        assert ok.status_code == 200

    def test_stop_requires_token(self, webapp_client, fake_psutil):
        client, app, _ = webapp_client
        app.state.webapp_config.auth_token = "secret-token"
        resp = client.post("/api/apps/vt/instances/1/stop")
        assert resp.status_code == 401


# --------------------------------------------- diagnostics helper unit test


class TestListeningPortForPidTree:
    def test_finds_port_on_descendant(self, fake_psutil):
        child = FakeProc(200, listen_port=8502)
        root = FakeProc(100, children=[child])
        fake_psutil.register(root)
        assert listening_port_for_pid_tree(100) == 8502

    def test_none_when_nothing_listening(self, fake_psutil):
        root = FakeProc(100, children=[FakeProc(200)])
        fake_psutil.register(root)
        assert listening_port_for_pid_tree(100) is None

    def test_none_when_root_missing(self, fake_psutil):
        assert listening_port_for_pid_tree(404) is None
