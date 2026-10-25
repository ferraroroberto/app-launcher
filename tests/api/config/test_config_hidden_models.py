"""POST /api/config hidden_models: the model dropdowns' visibility setting."""

from __future__ import annotations

from src.model_visibility import DEFAULT_HIDDEN_MODELS


class TestHiddenModels:
    def test_default_offers_codex_luna_only(self, webapp_client):
        client, _, _ = webapp_client
        body = client.get("/api/config").json()
        assert body["hidden_models"] == list(DEFAULT_HIDDEN_MODELS)
        codex = [m["value"] for m in body["model_catalog"]["codex"]]
        shown = [v for v in codex if f"codex:{v}" not in body["hidden_models"]]
        assert shown == ["gpt-6-luna"]

    def test_patch_round_trips_a_clean_string_list(self, webapp_client):
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"hidden_models": [" codex:gpt-5.6-sol ", "", 3]})
        assert resp.status_code == 200, resp.text
        assert app.state.webapp_config.hidden_models == ["codex:gpt-5.6-sol", "3"]
        assert client.get("/api/config").json()["hidden_models"] == ["codex:gpt-5.6-sol", "3"]

    def test_hiding_the_saved_default_moves_it_to_a_visible_model(self, webapp_client):
        client, app, _ = webapp_client
        client.post("/api/config", json={"hidden_models": []})
        client.post("/api/config", json={"coding_model_choice": "codex:gpt-5.6-terra"})
        assert app.state.webapp_config.codex_model == "gpt-5.6-terra"
        resp = client.post("/api/config", json={"hidden_models": ["codex:gpt-5.6-terra"]})
        assert resp.status_code == 200, resp.text
        cfg = app.state.webapp_config
        assert cfg.coding_model_choice == "codex:gpt-6-luna"
        assert cfg.codex_model == "gpt-6-luna"
        assert cfg.codex_effort == cfg.codex_model_efforts["gpt-6-luna"]
        assert "--model gpt-6-luna" in client.get("/api/config").json()["codex"]["computed_flags"]
