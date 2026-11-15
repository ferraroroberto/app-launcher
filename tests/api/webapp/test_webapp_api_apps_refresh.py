"""POST /api/apps/{id}/refresh: stop every tracked instance, relaunch hidden.

In-process TestClient with ``psutil`` faked in ``src.diagnostics`` and the
spawn / tray-restart helpers replaced: no real process starts or stops.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from typing import Iterator, List

import pytest

from src import app_runtime
from src.launching import app_refresh
from src.registry import AppEntry, Registry, save_registry
from tests.api.webapp.test_webapp_api_apps_running import FakeProc, FakePsutil


def _seed(path, *entries: AppEntry) -> None:
    save_registry(Registry(scan_root="C:\\stub", apps=list(entries)), path=path)


def _app(app_id="vt", kind="webapp", bat="C:\\stub\\vt\\run.bat") -> AppEntry:
    return AppEntry(
        id=app_id, name="Voice", kind=kind, bat_path=bat,
        added_at=datetime.now().isoformat(),
    )


@pytest.fixture
def fake_psutil(monkeypatch) -> Iterator[FakePsutil]:
    fake = FakePsutil()
    import src.diagnostics as diagnostics_mod

    monkeypatch.setattr(diagnostics_mod, "psutil", fake)
    monkeypatch.setattr(app_refresh, "STOP_WAIT_SECONDS", 0.3)
    app_runtime._instances.clear()
    yield fake
    app_runtime._instances.clear()


@pytest.fixture
def spawned(monkeypatch) -> List[tuple]:
    """The relaunch: records ``(bat, stealth)`` and returns a fresh pid."""
    from app.webapp.routers import apps as apps_router

    calls: List[tuple] = []

    def fake_spawn(bat, stealth=False):
        calls.append((str(bat), stealth))
        return 7000 + len(calls)

    monkeypatch.setattr(apps_router, "spawn_bat", fake_spawn)
    return calls


class _MortalProc(FakeProc):
    """A live process that leaves the process table once it is terminated."""

    def __init__(self, fake: FakePsutil, pid: int, create_time: float) -> None:
        super().__init__(pid, create_time=create_time)
        self._fake = fake

    def terminate(self) -> None:
        super().terminate()
        self._fake._exists.discard(self.pid)


def _track(fake: FakePsutil, app_id: str, pid: int, *, dies: bool) -> None:
    """A live tracked instance; ``dies`` False is one the kill cannot end."""
    inst = app_runtime.record_spawn(app_id, "Voice", "webapp", pid)
    if dies:
        fake.register(_MortalProc(fake, pid, inst.started_at))
    else:
        fake.register(FakeProc(pid, create_time=inst.started_at))


class TestRefresh:
    def test_stops_every_instance_then_launches_hidden(
        self, webapp_client, fake_psutil, spawned
    ):
        client, _, overrides = webapp_client
        _seed(overrides["tmp_registry_path"], _app())
        _track(fake_psutil, "vt", 101, dies=True)
        _track(fake_psutil, "vt", 102, dies=True)
        _track(fake_psutil, "other", 103, dies=True)

        resp = client.post("/api/apps/vt/refresh")

        assert resp.status_code == 200
        body = resp.json()
        assert body["refreshed"] == "vt"
        assert body["stops"] == [
            {"pid": 101, "stopped": True},
            {"pid": 102, "stopped": True},
        ]
        assert body["launched"] == {"stealth": True}
        assert spawned == [("C:\\stub\\vt\\run.bat", True)]
        # The old instances are gone and the new one is tracked.
        assert sorted(i.pid for i in app_runtime.list_running() if i.app_id == "vt") == [7001]

    def test_nothing_running_just_launches(self, webapp_client, fake_psutil, spawned):
        client, _, overrides = webapp_client
        _seed(overrides["tmp_registry_path"], _app())
        resp = client.post("/api/apps/vt/refresh")
        assert resp.status_code == 200
        assert resp.json()["stops"] == []
        assert spawned == [("C:\\stub\\vt\\run.bat", True)]

    def test_failed_stop_is_named_and_skips_the_relaunch(
        self, webapp_client, fake_psutil, spawned
    ):
        client, _, overrides = webapp_client
        _seed(overrides["tmp_registry_path"], _app())
        _track(fake_psutil, "vt", 101, dies=False)

        resp = client.post("/api/apps/vt/refresh")

        assert resp.status_code == 409
        assert "pid 101 did not stop" in resp.json()["detail"]
        assert spawned == []
        # Still tracked: the Running panel keeps showing what is still up.
        assert app_runtime.is_tracked("vt", 101)

    def test_tray_restarts_through_its_own_bat(
        self, webapp_client, fake_psutil, spawned, monkeypatch
    ):
        from app.webapp.routers import apps as apps_router

        client, _, overrides = webapp_client
        _seed(overrides["tmp_registry_path"],
              _app("sister-tray", "tray", "C:\\stub\\sister\\tray.bat"))
        ran: List[str] = []
        monkeypatch.setattr(
            apps_router, "restart_tray_bat", lambda bat: ran.append(str(bat)) or 0
        )

        resp = client.post("/api/apps/sister-tray/refresh")

        assert resp.status_code == 200
        assert resp.json()["restart"] == {"exit_code": 0}
        assert ran == ["C:\\stub\\sister\\tray.bat"]
        assert spawned == []  # no plain launch: the tray's own recipe is the restart

    @pytest.mark.parametrize(
        "outcome, status",
        [(3, 502), (subprocess.TimeoutExpired("tray.bat", 1), 504)],
    )
    def test_tray_restart_failure_is_its_own_error(
        self, webapp_client, fake_psutil, monkeypatch, outcome, status
    ):
        from app.webapp.routers import apps as apps_router

        client, _, overrides = webapp_client
        _seed(overrides["tmp_registry_path"],
              _app("sister-tray", "tray", "C:\\stub\\sister\\tray.bat"))

        def fake_restart(bat):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(apps_router, "restart_tray_bat", fake_restart)
        assert client.post("/api/apps/sister-tray/refresh").status_code == status


class TestRefreshRefusals:
    def test_unknown_app_is_404(self, webapp_client, fake_psutil):
        client, _, _ = webapp_client
        assert client.post("/api/apps/ghost/refresh").status_code == 404

    def test_launcher_own_bat_is_refused_and_nothing_stops(
        self, webapp_client, fake_psutil, spawned
    ):
        client, _, overrides = webapp_client
        own = str(app_refresh.PROJECT_ROOT / "tray.bat")
        _seed(overrides["tmp_registry_path"], _app("launcher", "tray", own))
        _track(fake_psutil, "launcher", 101, dies=False)

        resp = client.post("/api/apps/launcher/refresh")

        assert resp.status_code == 403
        assert "launcher itself" in resp.json()["detail"]
        assert app_runtime.is_tracked("launcher", 101) and spawned == []

    def test_instance_on_a_launcher_port_is_refused(
        self, webapp_client, fake_psutil, spawned
    ):
        client, app, overrides = webapp_client
        _seed(overrides["tmp_registry_path"], _app())
        port = app.state.webapp_config.session_host_port
        inst = app_runtime.record_spawn("vt", "Voice", "webapp", 101)
        fake_psutil.register(FakeProc(101, create_time=inst.started_at, listen_port=port))

        resp = client.post("/api/apps/vt/refresh")

        assert resp.status_code == 403
        assert f":{port}" in resp.json()["detail"]
        assert app_runtime.is_tracked("vt", 101) and spawned == []

    def test_rows_without_a_bat_are_refused(self, webapp_client, fake_psutil):
        client, _, overrides = webapp_client
        _seed(overrides["tmp_registry_path"],
              AppEntry(id="nobat", name="No bat", kind="webapp"))
        assert client.post("/api/apps/nobat/refresh").status_code == 403

    def test_a_refresh_already_running_is_refused(self, webapp_client, fake_psutil, spawned):
        from app.webapp.routers import apps as apps_router

        client, _, overrides = webapp_client
        _seed(overrides["tmp_registry_path"], _app())
        apps_router._refreshing.add("vt")
        try:
            assert client.post("/api/apps/vt/refresh").status_code == 409
        finally:
            apps_router._refreshing.discard("vt")
        assert spawned == []

    def test_requires_token(self, webapp_client, fake_psutil):
        client, app, _ = webapp_client
        app.state.webapp_config.auth_token = "secret-token"
        assert client.post("/api/apps/vt/refresh").status_code == 401


class TestRefreshableFlag:
    def test_listing_marks_the_launchers_own_bat_not_refreshable(self, webapp_client):
        client, _, overrides = webapp_client
        own = str(app_refresh.PROJECT_ROOT / "tray.bat")
        _seed(overrides["tmp_registry_path"], _app("launcher", "tray", own), _app())
        rows = {a["id"]: a for a in client.get("/api/apps").json()["apps"]}
        assert rows["launcher"]["refreshable"] is False
        assert rows["vt"]["refreshable"] is True
