"""Fleet chief (issue #245) — ensure + settings endpoints.

The contracts under test: ensure spawns the chief exactly once (label +
legacy-name matching, lock-serialized), in the fleet-config checkout, on the
configured chief model, typing only ``/chief`` through the two-frame
bracketed-paste path (never the command line); ``fresh`` quits the old chief
first; failures past the spawn kill the half-spawned session (the dispatch
contract, shared via ``_type_into_session``). Settings (model, worker cap)
persist through ``update_webapp_config`` with validation. #616 retired the
daily-respawn setting and its job-resync path — see the closed issue for why.
"""

from __future__ import annotations

import json
from pathlib import Path

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
    monkeypatch.setattr(board_router, "PTY_QUIESCENT_STABLE_S", 0.02)
    monkeypatch.setattr(board_router, "PTY_QUIESCENT_CAP_S", 0.3)
    monkeypatch.setattr(board_router, "PTY_QUIESCENT_POLL_S", 0.01)


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


class TestReconcileChiefLabel:
    """Unit coverage for ``_reconcile_chief_label`` (#617, extended #628): a
    chief PTY started outside ``ensure`` self-heals its ``label`` from any of
    three independent signals — ``prompt_title`` (#266, a fresh ``/chief``
    typed into a brand-new PTY), ``shared_name`` (fleet-config#302, Claude's
    own persisted conversation identity, which survives a Resume into a new
    PTY after a session-host restart — the case actually observed live
    against real session 1c8e6dde…, where the first line submitted into the
    new PTY was Roberto's own chat message, not ``/chief``), or ``live_title``
    (#628, the OSC title session_host parses straight off the PTY's own
    output — available before either of the other two, since it needs no
    hook and no submitted prompt at all)."""

    def _unlabelled_row(self, **extra):
        row = {
            "session_id": "manual-chief",
            "kind": "pty",
            "label": "",
            "prompt_title": "",
            "project_dir": "E:/automation/fleet-config",
            "alive": True,
        }
        row.update(extra)
        return row

    def test_unlabelled_chief_prompt_in_fleet_config_gets_healed(self):
        row = self._unlabelled_row(prompt_title="/chief")
        healed = board_router._reconcile_chief_label(row, shared_name=None)
        assert healed["label"] == "chief"

    def test_resumed_chief_healed_via_shared_name_despite_wrong_prompt(self):
        """The live-observed case: prompt_title is whatever the human typed
        first into the resumed PTY, never "/chief" — only shared_name (from
        Claude's own persisted identity) carries the signal."""
        row = self._unlabelled_row(prompt_title="ok restarted, check all is good")
        healed = board_router._reconcile_chief_label(row, shared_name="chief")
        assert healed["label"] == "chief"

    def test_already_labelled_row_is_untouched(self):
        row = self._unlabelled_row(label="chief", prompt_title="whatever")
        assert board_router._reconcile_chief_label(row, shared_name=None) is row

    def test_wrong_prompt_title_and_shared_name_is_not_healed(self):
        row = self._unlabelled_row(prompt_title="/chief please help")
        healed = board_router._reconcile_chief_label(row, shared_name="fleet-config")
        assert healed["label"] == ""

    def test_wrong_project_dir_is_not_healed(self):
        row = self._unlabelled_row(
            prompt_title="/chief", project_dir="E:/automation/app-launcher"
        )
        healed = board_router._reconcile_chief_label(row, shared_name="chief")
        assert healed["label"] == ""

    def test_remote_kind_is_not_healed(self):
        row = self._unlabelled_row(prompt_title="/chief", kind="remote")
        healed = board_router._reconcile_chief_label(row, shared_name="chief")
        assert healed["label"] == ""

    def test_shared_name_match_is_case_insensitive(self):
        row = self._unlabelled_row(prompt_title="hi")
        healed = board_router._reconcile_chief_label(row, shared_name="Chief")
        assert healed["label"] == "chief"

    def test_source_dict_is_never_mutated(self):
        row = self._unlabelled_row(prompt_title="/chief")
        healed = board_router._reconcile_chief_label(row, shared_name=None)
        assert healed is not row
        assert row["label"] == ""

    def test_resumed_chief_healed_via_live_title_before_hook_or_prompt(self):
        """#628, reproduced from the real resumed chief session (7174c1d2…):
        right after a host reboot, prompt_title is whatever was typed first
        into the new PTY (not "/chief") and shared_name hasn't caught up yet
        (no hook has fired) — but live_title, parsed straight off Claude
        Code's own OSC title re-emitted on Resume, already names the
        conversation. This is the exact "undetectable until its first hook
        fires" window #628 was filed over."""
        row = self._unlabelled_row(
            prompt_title="can I compact now?", live_title="👑 chief"
        )
        healed = board_router._reconcile_chief_label(row, shared_name=None)
        assert healed["label"] == "chief"

    def test_live_title_match_is_case_insensitive_and_tolerates_prefix(self):
        row = self._unlabelled_row(prompt_title="hi", live_title="🔥 CHIEF")
        healed = board_router._reconcile_chief_label(row, shared_name=None)
        assert healed["label"] == "chief"

    def test_unrelated_live_title_is_not_healed(self):
        row = self._unlabelled_row(prompt_title="hi", live_title="fleet-config")
        healed = board_router._reconcile_chief_label(row, shared_name=None)
        assert healed["label"] == ""


class TestReconcileChiefLabels:
    """``_reconcile_chief_labels`` (#617): the batch join — pulls
    ``shared_name`` from the state rows via the same agent-aware claim walk
    every other cross-tab title uses, then applies the per-session heal."""

    def test_joins_shared_name_from_state_row_by_launcher_session_id(self):
        live = [{
            "session_id": "manual-chief", "kind": "pty", "label": "",
            "prompt_title": "ok restarted, check all is good",
            "project_dir": "E:/automation/fleet-config", "alive": True,
            "started_at": "2026-07-27T07:14:00Z",
        }]
        state_rows = {
            "hook-row-1": {
                "launcher_session_id": "manual-chief", "agent": "claude",
                "name": "chief", "updated_at": "2026-07-27T07:14:05Z",
            },
        }
        healed = board_router._reconcile_chief_labels(live, state_rows)
        assert healed[0]["label"] == "chief"

    def test_no_matching_state_row_leaves_label_empty(self):
        live = [{
            "session_id": "manual-chief", "kind": "pty", "label": "",
            "prompt_title": "hi", "project_dir": "E:/automation/fleet-config",
            "alive": True, "started_at": "2026-07-27T07:14:00Z",
        }]
        healed = board_router._reconcile_chief_labels(live, {})
        assert healed[0]["label"] == ""


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
