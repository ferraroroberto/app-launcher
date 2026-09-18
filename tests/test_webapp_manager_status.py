"""Webapp health is a three-state fact, not a boolean (#1005).

A wedged uvicorn still LISTENs. ``status()`` used to read
``is_reachable() or is_port_in_use()``, so a port that accepted connections
and never answered ``/healthz`` reported ``running=True,
ownership="external"`` - health that could not be established, folded into
the passing state. ``start()`` then adopted the wedge and announced it
ready; ``restart()`` refused it with the wrong reason.

These tests use a **real listening socket that accepts and never replies** -
the actual wedge, at the level the probes see it - rather than faking the
probe methods, because the whole point of the bug is what a real probe can
and cannot establish. The one fake here is a ``Popen`` stand-in for the
"we own the wedged process" case, where spawning a real uvicorn would prove
nothing extra.
"""

from __future__ import annotations

import socket
import threading
from contextlib import contextmanager
from typing import Optional

import pytest

from app.webapp.manager import (
    HEALTH_ANSWERING,
    HEALTH_BOUND_NOT_ANSWERING,
    HEALTH_DOWN,
    OWNERSHIP_EXTERNAL,
    OWNERSHIP_OURS,
    WebappManager,
    WebappManagerConfig,
)

_RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"


@contextmanager
def port_holder(ready: Optional[threading.Event] = None):
    """A real server that holds the port and answers only once `ready` is set.

    `ready=None` is the wedge: connections are accepted and the socket is
    held open forever without a byte in reply, which is what a hung uvicorn
    looks like to both probes - `connect_ex` succeeds, `/healthz` times out.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    srv.settimeout(0.1)
    port = srv.getsockname()[1]
    stop = threading.Event()
    held = []

    def _serve():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            held.append(conn)
            if ready is not None and ready.is_set():
                try:
                    conn.recv(4096)
                    conn.sendall(_RESPONSE)
                    conn.close()
                except OSError:
                    pass

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        stop.set()
        thread.join(timeout=3)
        for conn in held:
            try:
                conn.close()
            except OSError:
                pass
        srv.close()


def _manager(port: int, **overrides) -> WebappManager:
    cfg = dict(
        host="127.0.0.1",
        port=port,
        request_timeout_seconds=0.3,
        startup_timeout_seconds=0.6,
        poll_interval_seconds=0.1,
    )
    cfg.update(overrides)
    return WebappManager(WebappManagerConfig(**cfg))


class _FakeProc:
    """A live process handle - the only fake here, for the ownership cases."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.terminated = False
        self.killed = False

    def poll(self):
        return None

    def send_signal(self, sig):  # noqa: ARG002
        pass

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):  # noqa: ARG002
        return 0


class TestStatusTellsTheThreeStatesApart:
    def test_a_wedged_port_is_not_reported_as_running(self):
        """The finding: bound + silent used to read as running external."""
        with port_holder() as port:
            status = _manager(port).status()
        assert status.health == HEALTH_BOUND_NOT_ANSWERING
        assert status.running is False, "health that could not be established is not a pass"
        assert status.ownership == OWNERSHIP_EXTERNAL
        assert "not answering" in status.detail
        assert "unknown" in status.detail

    def test_an_answering_port_is_still_adopted(self):
        ready = threading.Event()
        ready.set()
        with port_holder(ready) as port:
            status = _manager(port).status()
        assert status.health == HEALTH_ANSWERING
        assert status.running is True
        assert status.ownership == OWNERSHIP_EXTERNAL

    def test_nothing_listening_is_down(self):
        with port_holder() as port:
            pass  # the port is free again out here
        status = _manager(port).status()
        assert status.health == HEALTH_DOWN
        assert status.running is False

    def test_a_wedge_we_spawned_is_still_ours(self):
        with port_holder() as port:
            manager = _manager(port)
            manager._proc = _FakeProc()
            status = manager.status()
        assert status.health == HEALTH_BOUND_NOT_ANSWERING
        assert status.ownership == OWNERSHIP_OURS
        assert status.pid == 4242


class TestCallersBranchOnTheUnknownState:
    def test_start_refuses_to_adopt_a_wedge_and_does_not_spawn(self, monkeypatch):
        import app.webapp.manager as manager_mod

        spawned = []

        def _record(*args, **kwargs):
            spawned.append(args)
            return _FakeProc()

        monkeypatch.setattr(manager_mod.subprocess, "Popen", _record)
        with port_holder() as port:
            with pytest.raises(RuntimeError) as exc:
                _manager(port).start(wait=True)
        message = str(exc.value)
        assert "bound but did not answer" in message
        assert "health could not be established" in message
        assert "adopted" in message
        assert not spawned, "must not spawn uvicorn onto a port something else holds"

    def test_start_adopts_a_port_that_answers_within_the_grace_window(self):
        """The grace is a real re-probe, not a pause before a fixed verdict."""
        ready = threading.Event()
        with port_holder(ready) as port:
            threading.Timer(0.25, ready.set).start()
            status = _manager(port, startup_timeout_seconds=5.0).start(wait=True)
        assert status.health == HEALTH_ANSWERING
        assert status.running is True

    def test_restart_names_the_wedge_instead_of_blaming_ownership(self):
        with port_holder() as port:
            with pytest.raises(RuntimeError) as exc:
                _manager(port).restart(wait=True)
        message = str(exc.value)
        assert "not answering" in message
        assert "health could not be established" in message
        assert "cannot restart from here" not in message, (
            "the old message blamed external ownership for a wedge"
        )

    def test_stop_terminates_a_wedged_process_we_own(self):
        with port_holder() as port:
            manager = _manager(port)
            proc = _FakeProc()
            manager._proc = proc
            manager.stop()
        assert proc.terminated, "a process we spawned must still be stoppable once it wedges"

    def test_stop_leaves_a_wedge_we_do_not_own(self):
        with port_holder() as port:
            status = _manager(port).stop()
        assert status.ownership == OWNERSHIP_EXTERNAL
        assert status.health == HEALTH_BOUND_NOT_ANSWERING


class TestTrayStatusItem:
    def _stub(self, status):
        return type("Stub", (), {"manager": type("M", (), {"status": staticmethod(lambda: status)})()})()

    def test_status_item_flags_a_wedge_in_its_own_title(self, monkeypatch):
        import app.tray.tray as tray_mod
        from app.webapp.manager import WebappStatus

        seen = []
        monkeypatch.setattr(tray_mod, "_notify", lambda t, m: seen.append((t, m)))
        status = WebappStatus(
            running=False,
            ownership=OWNERSHIP_EXTERNAL,
            health=HEALTH_BOUND_NOT_ANSWERING,
            pid=None,
            port=8445,
            base_url="https://127.0.0.1:8445",
            detail="bound on :8445 but not answering /healthz",
        )
        tray_mod.TrayApp.show_status(self._stub(status), None, None)
        assert len(seen) == 1
        assert "NOT answering" in seen[0][0]

    def test_status_item_is_unchanged_when_the_webapp_answers(self, monkeypatch):
        import app.tray.tray as tray_mod
        from app.webapp.manager import WebappStatus

        seen = []
        monkeypatch.setattr(tray_mod, "_notify", lambda t, m: seen.append((t, m)))
        status = WebappStatus(
            running=True,
            ownership=OWNERSHIP_OURS,
            health=HEALTH_ANSWERING,
            pid=1,
            port=8445,
            base_url="https://127.0.0.1:8445",
            detail="running (started by this process)",
        )
        tray_mod.TrayApp.show_status(self._stub(status), None, None)
        assert seen[0][0] == "Launcher status"
