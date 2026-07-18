"""Fleet chief (issue #245) — ensure + settings endpoints.

The contracts under test: ensure spawns the chief exactly once (label +
legacy-name matching, lock-serialized), in the fleet-config checkout, on the
configured chief model, typing only ``/chief`` through the two-frame
bracketed-paste path (never the command line); ``fresh`` quits the old chief
first; failures past the spawn kill the half-spawned session (the dispatch
contract, shared via ``_type_into_session``). Settings persist through
``update_webapp_config`` with validation, and a respawn-time change resyncs
the registered daily job best-effort.
"""

from __future__ import annotations

import pytest

from app.webapp.routers import board as board_router


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
    monkeypatch.setattr(board_router, "DISPATCH_READY_CAP_S", 0.3)
    monkeypatch.setattr(board_router, "DISPATCH_SETTLE_S", 0.0)
    monkeypatch.setattr(board_router, "DISPATCH_POLL_S", 0.01)
    monkeypatch.setattr(board_router, "DISPATCH_LEGACY_GRACE_S", 0.0)
    monkeypatch.setattr(board_router, "CHIEF_STOP_WAIT_S", 0.1)
    monkeypatch.setattr(board_router, "CHIEF_STOP_POLL_S", 0.01)
    monkeypatch.setattr(board_router, "CHIEF_QUIESCENT_STABLE_S", 0.02)
    monkeypatch.setattr(board_router, "CHIEF_QUIESCENT_CAP_S", 0.3)
    monkeypatch.setattr(board_router, "CHIEF_QUIESCENT_POLL_S", 0.01)


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

    monkeypatch.setattr(board_router, "spawn_claude_session", fake_spawn)
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


def _chief_row(**extra):
    row = {
        "session_id": "chief-old",
        "kind": "pty",
        "agent": "claude",
        "label": "chief",
        "name": "chief",
        "alive": True,
    }
    row.update(extra)
    return row


class TestChiefGate:

    def test_chief_routes_classified_passkey(self):
        from app.webapp.middleware import _terminal_guard_level
        assert _terminal_guard_level("/api/board/chief/ensure") == "passkey"
        assert _terminal_guard_level("/api/board/chief/settings") == "passkey"

    def test_ensure_refused_off_tailnet(self, webapp_client):
        client, _, _ = webapp_client
        assert client.post("/api/board/chief/ensure").status_code == 403


class TestEnsureSpawn:

    def test_absent_chief_spawns_with_label_and_types_only_chief(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir, _ready_session,
    ):
        client, _, overrides = webapp_client
        resp = client.post("/api/board/chief/ensure", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["spawned"] is True and body["session_id"] == "chief-1"
        assert _spawn["label"] == "chief" and _spawn["name"] == "chief"
        assert _spawn["agent"] == "claude" and _spawn["kind"] == "pty"
        # Default chief model is fable; no positional prompt in flags.
        assert "--model fable" in _spawn["flags"]
        assert not _spawn["flags"].rstrip().endswith('"')
        assert str(_spawn["project_dir"]).endswith("fleet-config")
        # /chief rides the PTY input path: bracketed paste + its own CR.
        calls = overrides["session"].send_input.call_args_list
        assert len(calls) == 2
        assert calls[0].args == (8446, "chief-1", "\x1b[200~/chief\x1b[201~")
        assert calls[1].args == (8446, "chief-1", "\r")
        # Friendly display name via the manual-override rename path — and
        # it must land BEFORE /chief (#245 review): the rename forwards the
        # agent-native /rename into the PTY, which the agent rejects if it
        # interleaves with /chief's processing.
        assert overrides["session"].rename.call_args.args == (
            8446, "chief-1", "chief"
        )
        ordered = [
            name for name, _a, _k in overrides["session"].mock_calls
            if name in ("rename", "send_input")
        ]
        assert ordered == ["rename", "send_input", "send_input"]

    def test_model_honors_chief_settings(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir, _ready_session,
    ):
        client, app, _ = webapp_client
        app.state.webapp_config.chief_model = "opus"
        client.post("/api/board/chief/ensure", json={})
        assert "--model opus" in _spawn["flags"]

    def test_alive_chief_via_label_is_kept(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir,
    ):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_chief_row()]
        resp = client.post("/api/board/chief/ensure", json={})
        assert resp.status_code == 200
        assert resp.json() == {"session_id": "chief-old", "spawned": False}
        assert not _spawn
        overrides["session"].stop.assert_not_called()

    def test_alive_chief_via_legacy_name_is_kept(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir,
    ):
        """A legacy session-host that didn't echo ``label`` still can't be
        double-spawned — the name fallback finds the chief."""
        client, _, overrides = webapp_client
        row = _chief_row()
        del row["label"]
        overrides["session"].list_sessions.return_value = [row]
        resp = client.post("/api/board/chief/ensure", json={})
        assert resp.json()["spawned"] is False
        assert not _spawn

    def test_dead_or_nonpty_chief_rows_are_ignored(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir, _ready_session,
    ):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [
            _chief_row(alive=False),
            _chief_row(session_id="chief-remote", kind="remote"),
        ]
        resp = client.post("/api/board/chief/ensure", json={})
        assert resp.json()["spawned"] is True

    @pytest.mark.parametrize("via", ["query", "body"])
    def test_fresh_quits_old_chief_then_spawns(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir, via,
    ):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_chief_row()]
        # First get_session: the stop wait sees the old chief already dead;
        # every later probe (rename ready-wait + typing ready-wait) sees the
        # fresh one painting.
        probes = iter([{"alive": False}])

        def _get_session(port, sid):
            try:
                return next(probes)
            except StopIteration:
                return {"alive": True, "output_chars": 64}

        overrides["session"].get_session.side_effect = _get_session
        if via == "query":
            resp = client.post("/api/board/chief/ensure?fresh=1")
        else:
            resp = client.post(
                "/api/board/chief/ensure", json={"fresh": True}
            )
        assert resp.status_code == 200
        assert resp.json()["spawned"] is True
        assert overrides["session"].stop.call_args_list[0].args == (
            8446, "chief-old", "quit"
        )
        assert _spawn["label"] == "chief"

    def test_boot_never_quiescent_caps_and_still_types(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir,
    ):
        """The quiescence wait (#245 review: don't type while boot output is
        still streaming — a CR mid-boot is swallowed and merges the rename
        with the /chief paste) is best-effort: output that never settles
        caps out and the spawn still completes rather than failing."""
        client, _, overrides = webapp_client
        counter = {"n": 0}

        def _growing(port, sid):
            counter["n"] += 1
            return {"alive": True, "output_chars": counter["n"]}

        overrides["session"].get_session.side_effect = _growing
        resp = client.post("/api/board/chief/ensure", json={})
        assert resp.status_code == 200
        assert resp.json()["spawned"] is True
        assert len(overrides["session"].send_input.call_args_list) == 2

    def test_missing_fleet_config_checkout_404s(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn
    ):
        client, _, _ = webapp_client
        resp = client.post("/api/board/chief/ensure", json={})
        assert resp.status_code == 404
        assert "fleet-config" in resp.json()["detail"]
        assert not _spawn

    def test_readiness_timeout_kills_half_spawn(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir,
    ):
        client, _, overrides = webapp_client
        overrides["session"].get_session.return_value = {
            "alive": True, "output_chars": 0,
        }
        resp = client.post("/api/board/chief/ensure", json={})
        assert resp.status_code == 504
        overrides["session"].send_input.assert_not_called()
        assert overrides["session"].stop.call_args.args == (
            8446, "chief-1", "kill"
        )


class TestChiefSettings:

    def test_get_returns_defaults(self, webapp_client, _bypass_gate):
        client, _, _ = webapp_client
        resp = client.get("/api/board/chief/settings")
        assert resp.status_code == 200
        assert resp.json()["settings"] == {
            "model": "fable",
            "respawn_enabled": True,
            "respawn_at": "05:00",
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

    def test_put_respawn_change_resyncs_job(
        self, webapp_client, _bypass_gate, monkeypatch
    ):
        client, _, _ = webapp_client
        synced: dict = {}

        def fake_sync(enabled, at):
            synced.update(enabled=enabled, at=at)
            return ""

        monkeypatch.setattr(
            board_router, "_sync_chief_respawn_job", fake_sync
        )
        resp = client.put(
            "/api/board/chief/settings", json={"respawn_at": "06:30"}
        )
        assert resp.status_code == 200
        assert synced == {"enabled": True, "at": "06:30"}

    def test_put_without_respawn_change_skips_job_sync(
        self, webapp_client, _bypass_gate, monkeypatch
    ):
        client, _, _ = webapp_client
        monkeypatch.setattr(
            board_router, "_sync_chief_respawn_job",
            lambda *a: pytest.fail("job sync must not run"),
        )
        resp = client.put(
            "/api/board/chief/settings", json={"worker_cap": 4}
        )
        assert resp.status_code == 200

    def test_put_unregistered_job_is_warning_not_failure(
        self, webapp_client, _bypass_gate
    ):
        """jobs.json (tmp, empty) has no chief-daily-respawn — the save must
        still land, with the warning surfaced in the response."""
        client, _, _ = webapp_client
        resp = client.put(
            "/api/board/chief/settings", json={"respawn_at": "07:15"}
        )
        assert resp.status_code == 200
        assert "not registered" in resp.json()["job_warning"]
        assert resp.json()["settings"]["respawn_at"] == "07:15"

    @pytest.mark.parametrize("bad", [
        {"model": "haiku"},
        {"model": "gpt5.6"},
        {"respawn_at": "5:00"},
        {"respawn_at": "24:00"},
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
