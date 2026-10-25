"""POST /api/board/chief/ensure: the chief spawns exactly once, in the fleet-config checkout, typing only /chief."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


from tests.api.chief._support import _chief_row


class TestEnsureSpawn:

    def test_unreadable_session_list_is_not_an_absent_chief(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir, _ready_session,
    ):
        """#1308: a list the host would not answer must not be read as "no
        chief", or a live chief gets a duplicate spawned beside it."""
        from src.clients.session import SessionHostError

        client, _, overrides = webapp_client
        overrides["session"].list_sessions.side_effect = SessionHostError("timed out")
        resp = client.post("/api/board/chief/ensure", json={})
        assert resp.status_code == 503
        assert "unreachable" in resp.json()["detail"]
        assert _spawn == {}

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
        # /chief rides the PTY input path: framing + the submit CR are the
        # session-host's own job now (#611) — one call, submit=True.
        calls = overrides["session"].send_input.call_args_list
        assert len(calls) == 1
        assert calls[0].args == (8446, "chief-1", "/chief", True)
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
        assert ordered == ["rename", "send_input"]

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

    def test_alive_chief_via_self_heal_is_kept(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir,
    ):
        """A chief typed by hand outside ``ensure`` (#617) — no label, but
        its first submitted line was ``/chief`` in the fleet-config
        checkout — is recognized as already running, not double-spawned."""
        client, _, overrides = webapp_client
        row = _chief_row()
        del row["label"]
        row["name"] = "fleet-config"
        row["prompt_title"] = "/chief"
        row["project_dir"] = "E:/automation/fleet-config"
        overrides["session"].list_sessions.return_value = [row]
        resp = client.post("/api/board/chief/ensure", json={})
        assert resp.json()["spawned"] is False
        assert not _spawn

    def test_alive_chief_via_resumed_shared_name_is_kept(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir,
    ):
        """The case actually observed live (#617), verified against the real
        session (1c8e6dde…): a session-host restart kills chief's PTY, and
        Roberto re-attaches the same Claude Code conversation via Resume —
        no label, and the first line submitted into the *new* PTY is his own
        chat message, never "/chief". Only Claude's own persisted conversation
        name (``shared_name``, joined from the hook state file) identifies it."""
        client, app, overrides = webapp_client
        row = _chief_row()
        del row["label"]
        row["name"] = "fleet-config"
        row["prompt_title"] = "ok restarted, check all is good"
        row["project_dir"] = "E:/automation/fleet-config"
        row["started_at"] = "2026-07-27T07:14:00Z"
        overrides["session"].list_sessions.return_value = [row]

        cfg = app.state.webapp_config
        Path(cfg.sessions_state_file).write_text(
            json.dumps({
                "hook-row-1": {
                    "launcher_session_id": row["session_id"], "agent": "claude",
                    "name": "chief", "updated_at": "2026-07-27T07:14:05Z",
                },
            }),
            encoding="utf-8",
        )

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
        assert len(overrides["session"].send_input.call_args_list) == 1

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
