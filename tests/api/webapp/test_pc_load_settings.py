"""Settings > PC load: the night auto-restart switch and its window, saved through /api/config."""
from __future__ import annotations

import pytest


def test_the_defaults_are_off_and_21_to_05(webapp_client):
    client, _, _ = webapp_client
    body = client.get("/api/config").json()
    assert body["process_auto_restart"] is False
    assert (body["process_auto_restart_start"], body["process_auto_restart_end"]) == ("21:00", "05:00")


def test_the_switch_and_a_window_crossing_midnight_persist(webapp_client):
    client, app, _ = webapp_client
    assert client.post("/api/config", json={
        "process_auto_restart": True, "process_auto_restart_start": "22:30", "process_auto_restart_end": "06:15",
    }).status_code == 200
    cfg = app.state.webapp_config
    assert (cfg.process_auto_restart, cfg.process_auto_restart_start, cfg.process_auto_restart_end) == (
        True, "22:30", "06:15")
    from src.webapp_config import load_webapp_config
    assert load_webapp_config().process_auto_restart_end == "06:15"


@pytest.mark.parametrize("patch", [
    {"process_auto_restart_start": "9pm"},
    {"process_auto_restart_end": 5},
    {"process_auto_restart_start": "05:00"},  # equals the default end
])
def test_a_bad_window_is_refused_and_nothing_is_saved(webapp_client, patch):
    client, app, _ = webapp_client
    assert client.post("/api/config", json=patch).status_code == 400
    assert app.state.webapp_config.process_auto_restart_start == "21:00"


@pytest.mark.parametrize("value", ["true", 1, None])
def test_the_switch_takes_only_a_real_boolean(webapp_client, value):
    client, app, _ = webapp_client
    assert client.post("/api/config", json={"process_auto_restart": value}).status_code == 400
    assert app.state.webapp_config.process_auto_restart is False


def test_the_health_payload_carries_the_automatic_restart_line(webapp_client, monkeypatch):
    from src import process_auto_restart, process_restart
    from src.clients import hub_health

    monkeypatch.setattr(hub_health, "read", lambda url: {"verdict": "ok", "why": None, "reason": None, "fix": None})
    monkeypatch.setattr(process_restart, "_live", lambda name: [])
    monkeypatch.setattr(process_auto_restart, "last_auto_restart",
                        lambda: {"owner": "Elgato Wave Link", "at": "02:14", "outcome": "launched"})
    client, _, _ = webapp_client
    assert client.get("/api/system/health").json()["auto_restarted"]["at"] == "02:14"
