"""The build readout's restart sheet: its two targets, the live count, the gate.

Every test here goes through the real app (``webapp_client``), whose fixture
replaces the vendored detached spawn with one that fails the test: nothing
here ever starts the tray recipe or the session-host's. A test that needs a
restart to start swaps in a recorder.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.webapp import restart_targets
from src import self_restart
from src.middleware import terminal_gate, trust
from src.runtime_data import runtime_data_dir

ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture(autouse=True)
def _no_earlier_run():
    """The run record lives in the session-wide test data dir: start each test without one."""
    shutil.rmtree(runtime_data_dir("app-launcher") / "restart", ignore_errors=True)


@pytest.fixture(autouse=True)
def _counted_sessions(webapp_client, monkeypatch):
    """The live count reads the fixture's session-host mock, never a real host."""
    _client, _app, overrides = webapp_client
    monkeypatch.setattr(restart_targets, "session_client", overrides["session"])


@pytest.fixture
def spawned(monkeypatch) -> list:
    """Record each restart the route would have started, run nothing."""
    calls: list = []

    def _record(spec_path: Path, root: Path) -> None:
        calls.append(json.loads(Path(spec_path).read_text(encoding="utf-8")))

    monkeypatch.setattr(self_restart, "spawn_detached", _record)
    return calls


@pytest.fixture
def on_tailnet(monkeypatch):
    monkeypatch.setattr(trust, "client_in_tailnet", lambda host, allowlist: True)


@pytest.fixture
def passkey(monkeypatch, webapp_client):
    """The passkey gate configured; ``valid`` decides the presented token."""
    _client, app, _ = webapp_client
    gate_cls = MagicMock()
    gate_cls.configured.return_value = True
    monkeypatch.setattr(terminal_gate, "WebAuthnGate", gate_cls)
    gate = MagicMock()
    gate.valid_terminal_token.side_effect = lambda tok: tok == "good-token"
    app.state.webauthn_gate = gate
    return gate


def _session_host(body: dict) -> dict:
    return next(t for t in body["targets"] if t["id"] == "session-host")


def test_the_sheet_offers_the_app_and_the_session_host(webapp_client):
    client, _app, overrides = webapp_client
    overrides["session"].list_sessions.return_value = []
    body = client.get("/api/restart").json()
    assert [(t["id"], t["label"]) for t in body["targets"]] == [
        ("app", "Restart app"), ("session-host", "Restart session-host"),
    ]
    assert body["targets"][0]["warning"] == ""
    assert body["last"] is None


def test_the_session_host_warning_counts_live_sessions_on_every_read(webapp_client):
    client, _app, overrides = webapp_client
    sessions = overrides["session"].list_sessions
    sessions.return_value = [{"alive": True}, {"alive": True}, {"alive": False}]
    assert "ends 2 live terminal sessions" in _session_host(client.get("/api/restart").json())["warning"]
    sessions.return_value = [{"alive": True}]
    assert "ends 1 live terminal session " in _session_host(client.get("/api/restart").json())["warning"]


def test_an_unreadable_session_host_says_the_count_is_unknown(webapp_client):
    client, _app, overrides = webapp_client
    overrides["session"].list_sessions.side_effect = overrides["session"].SessionHostError("down")
    warning = _session_host(client.get("/api/restart").json())["warning"]
    assert "could not be read" in warning and "every live terminal session" in warning


def test_the_list_needs_the_apps_bearer_token(webapp_client):
    client, app, _ = webapp_client
    app.state.webapp_config.auth_token = "secret"
    assert client.get("/api/restart").status_code == 401
    assert client.post("/api/restart", json={"target": "app"}, headers=ORIGIN).status_code == 401


def test_off_the_tailnet_a_restart_is_refused(webapp_client):
    client, _app, _ = webapp_client
    res = client.post("/api/restart", json={"target": "session-host"}, headers=ORIGIN)
    assert res.status_code == 403
    assert client.get("/api/restart").json()["last"] is None


def test_without_the_passkey_a_restart_is_refused(webapp_client, on_tailnet, passkey):
    client, _app, _ = webapp_client
    res = client.post("/api/restart", json={"target": "session-host"}, headers=ORIGIN)
    assert res.status_code == 401
    res = client.post(
        "/api/restart", json={"target": "session-host"},
        headers={**ORIGIN, "X-Terminal-Token": "stale-token"},
    )
    assert res.status_code == 401
    assert client.get("/api/restart").json()["last"] is None


def test_the_confirmed_session_host_restart_runs_its_script(webapp_client, on_tailnet, passkey, spawned):
    client, _app, _ = webapp_client
    res = client.post(
        "/api/restart", json={"target": "session-host"},
        headers={**ORIGIN, "X-Terminal-Token": "good-token"},
    )
    assert res.status_code == 202
    (spec,) = spawned
    assert spec["target"] == "session-host"
    assert spec["command"][1:] == [
        "-m", "src.launching.ps_script", "scripts\\restart-session-host.ps1", "-Confirm",
    ]
    last = client.get("/api/restart").json()["last"]
    assert (last["id"], last["state"]) == (res.json()["restart_id"], "running")


def test_a_second_restart_waits_for_the_first(webapp_client, on_tailnet, passkey, spawned):
    client, _app, _ = webapp_client
    headers = {**ORIGIN, "X-Terminal-Token": "good-token"}
    assert client.post("/api/restart", json={"target": "app"}, headers=headers).status_code == 202
    assert client.post("/api/restart", json={"target": "session-host"}, headers=headers).status_code == 409
    assert [s["target"] for s in spawned] == ["app"]


def test_another_sites_post_is_refused_even_with_the_passkey(webapp_client, on_tailnet, passkey):
    client, _app, _ = webapp_client
    res = client.post(
        "/api/restart", json={"target": "app"},
        headers={"Origin": "https://elsewhere.example", "X-Terminal-Token": "good-token"},
    )
    assert res.status_code == 403


def test_the_pc_itself_needs_no_passkey(webapp_client, passkey, spawned, monkeypatch):
    client, _app, _ = webapp_client
    monkeypatch.setattr(trust, "LOOPBACK_HOSTS", frozenset({"testclient", "127.0.0.1", "::1"}))
    res = client.post("/api/restart", json={"target": "app"}, headers=ORIGIN)
    assert res.status_code == 202
    assert spawned[0]["command"] == list(self_restart.TRAY_RESTART)


def test_the_target_walk_at_mount_never_waits_on_the_session_host():
    calls: list = []
    targets = restart_targets.LiveRestartTargets(lambda: calls.append(1) or 4, ("x",))
    assert [t.id for t in targets] == ["app", "session-host"]
    assert calls == []
    targets.start_counting()
    assert "ends 4 live" in targets[1].warning and calls == [1]
