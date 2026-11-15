"""Settings > Dictation > Auto-stop: ``dictation_auto_stop_seconds`` defaults
off, saves one of its fixed choices and refuses anything else."""

from __future__ import annotations

KEY = "dictation_auto_stop_seconds"


def test_the_setting_defaults_off_saves_a_choice_and_refuses_the_rest(webapp_client):
    from src.webapp_config import load_webapp_config

    client, app, overrides = webapp_client
    assert client.get("/api/config").json()[KEY] == 0
    assert client.post("/api/config", json={KEY: 3}).status_code == 200
    assert client.get("/api/config").json()[KEY] == 3
    assert load_webapp_config(overrides["tmp_webapp_cfg_path"]).dictation_auto_stop_seconds == 3
    for bad in (4, "3", True, 2.5, -1):
        assert client.post("/api/config", json={KEY: bad}).status_code == 400, bad
    assert getattr(app.state.webapp_config, KEY) == 3
