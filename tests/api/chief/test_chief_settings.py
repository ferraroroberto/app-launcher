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
            "worker_cap_scope": "chief",
            "grokbot_enabled": True,
            "auto_drain": True,
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

    def test_worker_cap_scope_persists_and_survives_other_puts(self, webapp_client, _bypass_gate):
        client, app, _ = webapp_client
        resp = client.put("/api/board/chief/settings", json={"worker_cap_scope": "all"})
        assert resp.status_code == 200
        assert resp.json()["settings"]["worker_cap_scope"] == "all"
        assert app.state.webapp_config.chief_worker_cap_scope == "all"
        # A later PUT of another field leaves the scope alone, and the stored
        # value is what the loopback reader gets.
        client.put("/api/board/chief/settings", json={"worker_cap": 6})
        assert client.get(
            "/api/board/chief/settings"
        ).json()["settings"]["worker_cap_scope"] == "all"
        assert client.get("/api/config").json()["chief_worker_cap_scope"] == "all"
        resp = client.put("/api/board/chief/settings", json={"worker_cap_scope": "chief"})
        assert resp.json()["settings"]["worker_cap_scope"] == "chief"

    def test_grokbot_enabled_follows_the_config_switch(self, webapp_client, _bypass_gate):
        """The fleet-config grokbot readers poll this block over loopback, so
        a Settings flip (POST /api/config) must show here at once, and a
        chief PUT of other fields must not reset it."""
        client, _, _ = webapp_client
        assert client.post("/api/config", json={"grokbot_enabled": False}).status_code == 200
        assert client.get(
            "/api/board/chief/settings"
        ).json()["settings"]["grokbot_enabled"] is False
        resp = client.put("/api/board/chief/settings", json={"worker_cap": 4})
        assert resp.json()["settings"] == {
            "model": "fable",
            "worker_cap": 4,
            "worker_cap_scope": "chief",
            "grokbot_enabled": False,
            "auto_drain": True,
        }

    def test_auto_drain_follows_the_config_switch(self, webapp_client, _bypass_gate):
        """A fresh config reports auto_drain true. Turning the switch off
        persists, and a later cap save does not turn it back on."""
        client, _, _ = webapp_client
        assert client.get(
            "/api/board/chief/settings"
        ).json()["settings"]["auto_drain"] is True
        assert client.post(
            "/api/config", json={"chief_auto_drain": False}
        ).status_code == 200
        assert client.get(
            "/api/board/chief/settings"
        ).json()["settings"]["auto_drain"] is False
        resp = client.put("/api/board/chief/settings", json={"worker_cap": 4})
        assert resp.json()["settings"]["auto_drain"] is False

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
        {"worker_cap_scope": "everyone"},
        {"worker_cap_scope": ""},
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


class TestChiefCapRaisedEvent:
    """Saving a higher worker cap (or freeing slots by switching the scope to
    the chief's own sessions) drops one cap-raised event in the chief mailbox;
    a lower or unchanged save drops nothing, and a failed write never fails
    the save."""

    @pytest.fixture
    def inbox(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        from src import instance_role

        monkeypatch.setattr(
            instance_role, "canonical_instance", lambda *a, **k: (True, "canonical")
        )
        return tmp_path / "chief-inbox"

    @staticmethod
    def _events(inbox):
        import json

        return [json.loads(p.read_text("utf-8")) for p in sorted(inbox.glob("*.json"))]

    def test_a_higher_cap_writes_one_event_and_rings_the_doorbell(self, webapp_client, inbox):
        import time

        from app.webapp.chief_doorbell import count_pending

        client, _, _ = webapp_client
        assert client.put("/api/board/chief/settings", json={"worker_cap": 5}).status_code == 200
        (event,) = self._events(inbox)
        assert (event["kind"], event["event"], event["old"], event["new"]) == (
            "cap-raised", "CapRaised", 3, 5,
        )
        assert count_pending(inbox, int(time.time() * 1000)) == 1

    @pytest.mark.parametrize("cap", [3, 2])
    def test_an_unchanged_or_lower_cap_writes_nothing(self, webapp_client, inbox, cap):
        client, app, _ = webapp_client
        assert client.put("/api/board/chief/settings", json={"worker_cap": cap}).status_code == 200
        assert app.state.webapp_config.chief_worker_cap == cap
        assert self._events(inbox) == []

    def test_a_scope_switch_to_chief_writes_one_event(self, webapp_client, inbox):
        client, _, _ = webapp_client
        client.put("/api/board/chief/settings", json={"worker_cap_scope": "all"})
        assert self._events(inbox) == []
        client.put("/api/board/chief/settings", json={"worker_cap_scope": "chief"})
        (event,) = self._events(inbox)
        assert (event["old"], event["new"]) == (3, 3)

    def test_a_failed_mailbox_write_does_not_fail_the_save(
        self, webapp_client, inbox, monkeypatch
    ):
        from src.chief import cap_raised

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(cap_raised, "publish_event", boom)
        client, app, _ = webapp_client
        resp = client.put("/api/board/chief/settings", json={"worker_cap": 6})
        assert resp.status_code == 200
        assert resp.json()["settings"]["worker_cap"] == 6
        assert app.state.webapp_config.chief_worker_cap == 6
