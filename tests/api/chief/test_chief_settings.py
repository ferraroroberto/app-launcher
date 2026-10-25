"""The chief's settings: auto-compact threshold, model and worker cap."""

from __future__ import annotations


import pytest



class TestChiefAutoCompactThreshold:
    """#1298: the chief's auto-compact threshold (fleet-config#1052), set in
    Settings through the ordinary /api/config save and read by
    ``chief_ops.py self-compact`` from GET /api/board/chief-settings. 0 is
    off; otherwise 10-90 percent; 30 when nothing is stored."""

    def test_endpoint_returns_the_default_when_nothing_is_stored(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/api/board/chief-settings")
        assert resp.status_code == 200
        assert resp.json() == {"auto_compact_threshold": 30}

    @pytest.mark.parametrize("value", [45, 10, 90, 0])
    def test_settings_save_persists_and_the_endpoint_reads_it(self, webapp_client, value):
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"chief_auto_compact_threshold": value})
        assert resp.status_code == 200, resp.text
        assert app.state.webapp_config.chief_auto_compact_threshold == value
        assert client.get("/api/config").json()["chief_auto_compact_threshold"] == value
        assert client.get("/api/board/chief-settings").json() == {"auto_compact_threshold": value}
        # Persisted to the config file, not just process state.
        from src.webapp_config import load_webapp_config
        assert load_webapp_config().chief_auto_compact_threshold == value

    @pytest.mark.parametrize("bad", [5, 9, 91, 100, -1])
    def test_out_of_range_is_rejected_with_a_clear_message(self, webapp_client, bad):
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"chief_auto_compact_threshold": bad})
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "chief_auto_compact_threshold" in detail and "0 (off)" in detail, detail
        assert "10" in detail and "90" in detail, detail
        assert app.state.webapp_config.chief_auto_compact_threshold == 30

    @pytest.mark.parametrize("bad", ["lots", None, 30.5, True])
    def test_a_non_integer_is_rejected(self, webapp_client, bad):
        client, _, _ = webapp_client
        resp = client.post("/api/config", json={"chief_auto_compact_threshold": bad})
        assert resp.status_code == 400
        assert "integer" in resp.json()["detail"]

    def test_endpoint_sits_behind_the_board_token(self, webapp_client):
        client, app, _ = webapp_client
        app.state.webapp_config.auth_token = "secret-token"
        assert client.get("/api/board/chief-settings").status_code == 401
        ok = client.get(
            "/api/board/chief-settings", headers={"Authorization": "Bearer secret-token"}
        )
        assert ok.status_code == 200 and ok.json() == {"auto_compact_threshold": 30}


class TestChiefSettings:

    def test_get_returns_defaults(self, webapp_client, _bypass_gate):
        client, _, _ = webapp_client
        resp = client.get("/api/board/chief/settings")
        assert resp.status_code == 200
        assert resp.json()["settings"] == {
            "model": "fable",
            "worker_cap": 3,
        }

    def test_put_persists_and_reloads(self, webapp_client, _bypass_gate):
        client, app, _ = webapp_client
        resp = client.put(
            "/api/board/chief/settings",
            json={"model": "opus", "worker_cap": 5},
        )
        assert resp.status_code == 200
        assert resp.json()["settings"]["model"] == "opus"
        assert resp.json()["settings"]["worker_cap"] == 5
        assert app.state.webapp_config.chief_model == "opus"
        # Round-trips through the persisted file, not just process state.
        assert client.get(
            "/api/board/chief/settings"
        ).json()["settings"]["worker_cap"] == 5

    def test_put_ignores_unknown_keys(self, webapp_client, _bypass_gate):
        """#616: a stale client still sending the retired respawn keys must
        not error or resurrect them — they're simply not recognized patch
        keys, so a request carrying only those (plus nothing else valid)
        falls through to the generic empty-patch 400, same as any other
        unrecognized body."""
        client, _, _ = webapp_client
        resp = client.put(
            "/api/board/chief/settings",
            json={"respawn_enabled": False, "respawn_at": "06:30"},
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize("bad", [
        {"model": "haiku"},
        {"model": "gpt5.6"},
        {"worker_cap": 0},
        {"worker_cap": 99},
        {"worker_cap": "lots"},
        {},
    ])
    def test_put_rejects_bad_values(self, webapp_client, _bypass_gate, bad):
        client, _, _ = webapp_client
        assert client.put(
            "/api/board/chief/settings", json=bad
        ).status_code == 400

    def test_put_accepts_raised_ceiling(self, webapp_client, _bypass_gate):
        """#547: the ceiling was raised 8 -> 10 on direct request; 10 must
        now persist (it 400'd against the old ceiling) and 11 must still
        400 (the ceiling moved, it didn't disappear)."""
        client, _, _ = webapp_client
        resp = client.put(
            "/api/board/chief/settings", json={"worker_cap": 10}
        )
        assert resp.status_code == 200
        assert resp.json()["settings"]["worker_cap"] == 10
        assert client.put(
            "/api/board/chief/settings", json={"worker_cap": 11}
        ).status_code == 400
