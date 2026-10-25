"""Fixtures for the fleet-chief tests: the passkey gate bypassed, a fast readiness probe and a recorded spawn."""

from __future__ import annotations


import pytest

from app.webapp.routers import board_chief as chief_router
from app.webapp.routers import board_spawn


@pytest.fixture
def _bypass_gate(monkeypatch):
    """Treat the TestClient host as loopback so the endpoint logic is
    exercised (the gate itself is covered by TestChiefGate)."""
    from app.webapp import middleware
    monkeypatch.setattr(
        middleware,
        "LOOPBACK_HOSTS",
        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )


@pytest.fixture
def _fast_probe(monkeypatch):
    """Shrink every wait so no test ever really sleeps."""
    monkeypatch.setattr(board_spawn, "DISPATCH_READY_CAP_S", 0.3)
    monkeypatch.setattr(board_spawn, "DISPATCH_SETTLE_S", 0.0)
    monkeypatch.setattr(board_spawn, "DISPATCH_POLL_S", 0.01)
    monkeypatch.setattr(board_spawn, "DISPATCH_LEGACY_GRACE_S", 0.0)
    monkeypatch.setattr(chief_router, "CHIEF_STOP_WAIT_S", 0.1)
    monkeypatch.setattr(chief_router, "CHIEF_STOP_POLL_S", 0.01)
    monkeypatch.setattr(board_spawn, "PTY_QUIESCENT_STABLE_S", 0.02)
    monkeypatch.setattr(board_spawn, "PTY_QUIESCENT_CAP_S", 0.3)
    monkeypatch.setattr(board_spawn, "PTY_QUIESCENT_POLL_S", 0.01)


@pytest.fixture
def _spawn(webapp_client, monkeypatch):
    """Capture the spawn call; ensure passes label='chief' and no prompt."""
    captured: dict = {}

    def fake_spawn(project_dir, name, flags, port, kind, agent, rows, cols,
                   history_lines=None, label=""):
        captured.update(
            project_dir=project_dir, name=name, flags=flags,
            port=port, kind=kind, agent=agent, rows=rows, cols=cols,
            history_lines=history_lines, label=label,
        )
        return {"session_id": "chief-1", "kind": "pty", "name": name}

    monkeypatch.setattr(chief_router, "spawn_claude_session", fake_spawn)
    return captured


@pytest.fixture
def _fleet_config_dir(webapp_client):
    _, _, overrides = webapp_client
    (overrides["tmp_projects_dir"] / "fleet-config").mkdir(exist_ok=True)
    return overrides


@pytest.fixture
def _ready_session(webapp_client):
    """Session-host says the spawned agent is alive and already painting."""
    _, _, overrides = webapp_client
    overrides["session"].get_session.return_value = {
        "alive": True, "output_chars": 64,
    }
    return overrides
