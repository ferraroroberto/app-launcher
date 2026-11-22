"""Settings > Sessions > Window browser: ``mirror_browser`` defaults to Chrome, saves one of
its choices and refuses anything else."""

from __future__ import annotations

KEY = "mirror_browser"


def test_the_setting_defaults_to_chrome_saves_a_choice_and_refuses_the_rest(webapp_client):
    from src.webapp_config import load_webapp_config

    client, app, overrides = webapp_client
    assert client.get("/api/config").json()[KEY] == "chrome"
    assert client.post("/api/config", json={KEY: "edge"}).status_code == 200
    assert client.get("/api/config").json()[KEY] == "edge"
    assert load_webapp_config(overrides["tmp_webapp_cfg_path"]).mirror_browser == "edge"
    for bad in ("firefox", "", None, 1, ["edge"]):
        assert client.post("/api/config", json={KEY: bad}).status_code == 400, bad
    assert getattr(app.state.webapp_config, KEY) == "edge"
