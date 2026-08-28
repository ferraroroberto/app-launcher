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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.webapp.routers import board_chief as chief_router
from app.webapp.routers import board_spawn
from src import chief_pointer


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
        healed = chief_router._reconcile_chief_label(row, shared_name=None)
        assert healed["label"] == "chief"

    def test_resumed_chief_healed_via_shared_name_despite_wrong_prompt(self):
        """The live-observed case: prompt_title is whatever the human typed
        first into the resumed PTY, never "/chief" — only shared_name (from
        Claude's own persisted identity) carries the signal."""
        row = self._unlabelled_row(prompt_title="ok restarted, check all is good")
        healed = chief_router._reconcile_chief_label(row, shared_name="chief")
        assert healed["label"] == "chief"

    def test_already_labelled_row_is_untouched(self):
        row = self._unlabelled_row(label="chief", prompt_title="whatever")
        assert chief_router._reconcile_chief_label(row, shared_name=None) is row

    def test_wrong_prompt_title_and_shared_name_is_not_healed(self):
        row = self._unlabelled_row(prompt_title="/chief please help")
        healed = chief_router._reconcile_chief_label(row, shared_name="fleet-config")
        assert healed["label"] == ""

    def test_wrong_project_dir_is_not_healed(self):
        row = self._unlabelled_row(
            prompt_title="/chief", project_dir="E:/automation/app-launcher"
        )
        healed = chief_router._reconcile_chief_label(row, shared_name="chief")
        assert healed["label"] == ""

    def test_remote_kind_is_not_healed(self):
        row = self._unlabelled_row(prompt_title="/chief", kind="remote")
        healed = chief_router._reconcile_chief_label(row, shared_name="chief")
        assert healed["label"] == ""

    def test_shared_name_match_is_case_insensitive(self):
        row = self._unlabelled_row(prompt_title="hi")
        healed = chief_router._reconcile_chief_label(row, shared_name="Chief")
        assert healed["label"] == "chief"

    def test_source_dict_is_never_mutated(self):
        row = self._unlabelled_row(prompt_title="/chief")
        healed = chief_router._reconcile_chief_label(row, shared_name=None)
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
        healed = chief_router._reconcile_chief_label(row, shared_name=None)
        assert healed["label"] == "chief"

    def test_live_title_match_is_case_insensitive_and_tolerates_prefix(self):
        row = self._unlabelled_row(prompt_title="hi", live_title="🔥 CHIEF")
        healed = chief_router._reconcile_chief_label(row, shared_name=None)
        assert healed["label"] == "chief"

    def test_unrelated_live_title_is_not_healed(self):
        row = self._unlabelled_row(prompt_title="hi", live_title="fleet-config")
        healed = chief_router._reconcile_chief_label(row, shared_name=None)
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
        healed = chief_router._reconcile_chief_labels(live, state_rows)
        assert healed[0]["label"] == "chief"

    def test_no_matching_state_row_leaves_label_empty(self):
        live = [{
            "session_id": "manual-chief", "kind": "pty", "label": "",
            "prompt_title": "hi", "project_dir": "E:/automation/fleet-config",
            "alive": True, "started_at": "2026-07-27T07:14:00Z",
        }]
        healed = chief_router._reconcile_chief_labels(live, {})
        assert healed[0]["label"] == ""

    async def test_writes_the_durable_pointer_itself_issue_796(self, tmp_path):
        """Regression for #796: ``GET /api/board`` (``routers/board.py``)
        calls ``_reconcile_chief_labels`` directly, never
        ``_live_sessions_with_chief_label`` — so the pointer write must
        happen here too, or a chief self-healed only via this path (hand-
        typed into a plain Coding-tab session) never gets its conversation
        id persisted to ``webapp/chief-pointer.json``."""
        transcript = _transcript(tmp_path, "board-poll", typed=True)
        rows = {
            "board-poll-conv": _chief_state_row(
                "chief-old", _recent_iso(minutes=1),
                name="fleet-config-79", transcript_path=transcript,
            ),
        }
        import asyncio

        chief_router._reconcile_chief_labels(
            [_chief_row(
                label="", name="fleet-config", live_title="✳ chief",
                project_dir="E:/automation/fleet-config",
            )],
            rows,
        )
        pending = [
            task for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
        ]
        if pending:
            await asyncio.gather(*pending)
        assert chief_pointer.read_chief_pointer()["session_id"] == "board-poll-conv"


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


def _chief_state_row(session_id, updated_at, **extra):
    row = {
        "launcher_session_id": session_id, "agent": "claude",
        "name": "chief", "cwd": "E:/automation/fleet-config",
        "updated_at": updated_at,
    }
    row.update(extra)
    return row


def _transcript(tmp_path: Path, stem: str, *, typed: bool) -> str:
    """A chief transcript JSONL, with or without a typed human prompt (#670).

    The bootstrap lines mirror what a launcher-spawned, never-talked-to chief
    actually writes (verified against the real 2026-07-28 blank chief): the
    slash-command wrapper as a plain string, the skill body it expands to as
    a content *list*, and the post-spawn rename as a ``<system-reminder>`` —
    three user lines, none of them typed by a person.
    """
    lines = [
        {"type": "user", "timestamp": "2026-07-28T19:12:55Z", "message": {
            "role": "user",
            "content": "<command-message>chief</command-message>\n"
                       "<command-name>/chief</command-name>"}},
        {"type": "user", "timestamp": "2026-07-28T19:12:55Z", "message": {
            "role": "user",
            "content": [{"type": "text", "text": "Base directory for this skill: ..."}]}},
        {"type": "assistant", "timestamp": "2026-07-28T19:13:20Z", "message": {
            "id": "m1", "role": "assistant",
            "content": [{"type": "text", "text": "read the handover"}]}},
        {"type": "user", "timestamp": "2026-07-28T19:22:21Z", "message": {
            "role": "user",
            "content": '<system-reminder>The user named this session "chief".'
                       "</system-reminder>"}},
    ]
    if typed:
        lines.append({
            "type": "user", "timestamp": "2026-07-28T19:30:00Z",
            "message": {"role": "user", "content": "how is it going?"}})
    target = tmp_path / f"{stem}.jsonl"
    target.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )
    return str(target)


def _recent_iso(**delta_kwargs) -> str:
    """An ISO-8601 UTC stamp ``delta_kwargs`` in the past from the *real*
    current time (issue #658) — for rows exercised through the HTTP-level
    ``/api/board/chief/ensure`` endpoint, which calls ``board._now()``
    internally with no way to inject a frozen clock. A hardcoded calendar
    date here goes permanently stale the moment real wall-clock time
    crosses ``board.STATE_STALE_AFTER`` (24h) past it — this stays valid
    on any run date. Tests that already control ``now=`` directly (see
    ``test_stale_row_past_24h_is_excluded``) don't need this."""
    stamp = datetime.now(timezone.utc) - timedelta(**delta_kwargs)
    return stamp.isoformat().replace("+00:00", "Z")


class TestFindResumableChiefSessionId:
    """Unit coverage for ``_find_resumable_chief_session_id`` (#633): the
    lookup that turns "the most recent chief conversation" into a concrete
    session id ``claude --resume <id>`` can reattach to."""

    def test_no_rows_returns_empty(self):
        assert chief_router._find_resumable_chief_session_id({}) == ""

    def test_picks_newest_matching_row(self):
        rows = {
            "old-uuid": _chief_state_row("old-sess", _recent_iso(hours=2)),
            "new-uuid": _chief_state_row("new-sess", _recent_iso(hours=1)),
        }
        assert chief_router._find_resumable_chief_session_id(rows) == "new-uuid"

    def test_wrong_project_is_excluded(self):
        rows = {
            "uuid-1": _chief_state_row(
                "s1", "2026-07-27T07:00:00Z",
                cwd="E:/automation/app-launcher",
            ),
        }
        assert chief_router._find_resumable_chief_session_id(rows) == ""

    def test_wrong_name_is_excluded(self):
        rows = {
            "uuid-1": _chief_state_row(
                "s1", "2026-07-27T07:00:00Z", name="fleet-config",
            ),
        }
        assert chief_router._find_resumable_chief_session_id(rows) == ""

    def test_name_match_is_case_insensitive(self):
        rows = {
            "uuid-1": _chief_state_row(
                "s1", _recent_iso(hours=1), name="Chief",
            ),
        }
        assert chief_router._find_resumable_chief_session_id(rows) == "uuid-1"

    def test_stale_row_past_24h_is_excluded(self):
        rows = {"uuid-1": _chief_state_row("s1", "2026-07-26T00:00:00Z")}
        now = chief_router.board._parse_iso("2026-07-27T07:00:00Z")
        assert (
            chief_router._find_resumable_chief_session_id(rows, now=now) == ""
        )

    def test_project_field_used_over_cwd_basename(self):
        rows = {
            "uuid-1": _chief_state_row(
                "s1", _recent_iso(hours=1),
                cwd="E:/automation/app-launcher-wt-42", project="fleet-config",
            ),
        }
        assert chief_router._find_resumable_chief_session_id(rows) == "uuid-1"

    def test_non_dict_row_is_skipped(self):
        rows = {"uuid-1": "not-a-row"}
        assert chief_router._find_resumable_chief_session_id(rows) == ""


class TestResumeRanksBySubstance:
    """#670: recency alone picks the wrong conversation after anything that
    kills the chief's PTY. The real chief's row freezes at the moment of
    death; the blank chief the fallback path spawns seconds later — renamed
    ``"chief"`` by ``ensure_chief`` itself — is newer forever after. These
    pin that substance decides first, and that "couldn't tell" never
    masquerades as "confirmed empty"."""

    def test_bootstrap_only_newest_loses_to_the_substantive_older_one(
        self, tmp_path
    ):
        """The exact 2026-07-28 incident: a 10-minute-old ``/chief``-and-
        handover conversation must not win over the three-day one it
        displaced."""
        rows = {
            "real-uuid": _chief_state_row(
                "real-sess", _recent_iso(hours=3),
                transcript_path=_transcript(tmp_path, "real", typed=True),
            ),
            "blank-uuid": _chief_state_row(
                "blank-sess", _recent_iso(minutes=1),
                transcript_path=_transcript(tmp_path, "blank", typed=False),
            ),
        }
        assert (
            chief_router._find_resumable_chief_session_id(rows) == "real-uuid"
        )

    def test_newest_still_wins_between_two_substantive_conversations(
        self, tmp_path
    ):
        rows = {
            "old-uuid": _chief_state_row(
                "old-sess", _recent_iso(hours=3),
                transcript_path=_transcript(tmp_path, "old", typed=True),
            ),
            "new-uuid": _chief_state_row(
                "new-sess", _recent_iso(minutes=5),
                transcript_path=_transcript(tmp_path, "new", typed=True),
            ),
        }
        assert (
            chief_router._find_resumable_chief_session_id(rows) == "new-uuid"
        )

    def test_confirmed_substance_outranks_unknown_substance(self, tmp_path):
        rows = {
            "known-uuid": _chief_state_row(
                "known-sess", _recent_iso(hours=3),
                transcript_path=_transcript(tmp_path, "known", typed=True),
            ),
            "unknown-uuid": _chief_state_row(
                "unknown-sess", _recent_iso(minutes=5),
                transcript_path="C:/nope/missing.jsonl",
            ),
        }
        assert (
            chief_router._find_resumable_chief_session_id(rows) == "known-uuid"
        )

    def test_unknown_substance_is_still_resumable_when_nothing_better(self):
        """Unknown is not a negative — an unreadable transcript still beats
        throwing the conversation away for a fresh spawn."""
        rows = {"uuid-1": _chief_state_row("s1", _recent_iso(hours=1))}
        assert (
            chief_router._find_resumable_chief_session_id(rows) == "uuid-1"
        )

    def test_only_bootstrap_only_conversations_resumes_nothing(self, tmp_path):
        rows = {
            "blank-uuid": _chief_state_row(
                "blank-sess", _recent_iso(minutes=1),
                transcript_path=_transcript(tmp_path, "blank", typed=False),
            ),
        }
        assert chief_router._find_resumable_chief_session_id(rows) == ""

    def test_preferred_sid_wins_even_with_a_project_derived_name(
        self, tmp_path
    ):
        """A chief the launcher never spawned (Roberto's own ``/resume``, or a
        plain Coding session he typed ``/chief`` into) has a project-derived
        row name, so the name scan can't see it at all — but its own PTY was
        just stopped for this resume, so it is exactly what to reattach."""
        rows = {
            "hand-uuid": _chief_state_row(
                "hand-sess", _recent_iso(minutes=2), name="fleet-config-79",
                transcript_path=_transcript(tmp_path, "hand", typed=True),
            ),
            "other-uuid": _chief_state_row(
                "other-sess", _recent_iso(hours=1),
                transcript_path=_transcript(tmp_path, "other", typed=True),
            ),
        }
        assert chief_router._find_resumable_chief_session_id(
            rows, preferred_sid="hand-uuid"
        ) == "hand-uuid"

    def test_preferred_sid_is_ignored_when_bootstrap_only(self, tmp_path):
        """The ratchet #670 filed over: stopping the live chief first (#640)
        means the blank one is always the preferred candidate, so preference
        must not survive a confirmed-empty transcript."""
        rows = {
            "blank-uuid": _chief_state_row(
                "blank-sess", _recent_iso(minutes=1),
                transcript_path=_transcript(tmp_path, "blank", typed=False),
            ),
            "real-uuid": _chief_state_row(
                "real-sess", _recent_iso(hours=3),
                transcript_path=_transcript(tmp_path, "real", typed=True),
            ),
        }
        assert chief_router._find_resumable_chief_session_id(
            rows, preferred_sid="blank-uuid"
        ) == "real-uuid"

    def test_preferred_sid_outside_fleet_config_is_ignored(self, tmp_path):
        rows = {
            "elsewhere-uuid": _chief_state_row(
                "elsewhere-sess", _recent_iso(minutes=1),
                cwd="E:/automation/app-launcher", name="app-launcher-11",
                transcript_path=_transcript(tmp_path, "elsewhere", typed=True),
            ),
        }
        assert chief_router._find_resumable_chief_session_id(
            rows, preferred_sid="elsewhere-uuid"
        ) == ""

    def test_unknown_preferred_sid_falls_through_to_the_scan(self, tmp_path):
        rows = {
            "real-uuid": _chief_state_row(
                "real-sess", _recent_iso(hours=3),
                transcript_path=_transcript(tmp_path, "real", typed=True),
            ),
        }
        assert chief_router._find_resumable_chief_session_id(
            rows, preferred_sid="ghost-uuid"
        ) == "real-uuid"


class TestEnsureResume:
    """Integration coverage for ``resume`` on ``POST /api/board/chief/ensure``
    (#633): reattach the most recent chief conversation via a direct
    ``claude --resume <id>`` instead of starting fresh, falling back cleanly
    when nothing is resumable."""

    def test_no_resumable_conversation_falls_back_to_fresh_and_types_chief(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir, _ready_session,
    ):
        client, _, overrides = webapp_client
        resp = client.post("/api/board/chief/ensure", json={"resume": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["spawned"] is True
        assert body["resumed"] is False
        assert body["resume_fallback_reason"]
        # Fallback is indistinguishable from a plain fresh spawn: /chief is
        # still typed, and no --resume token rides the flags.
        assert "--resume" not in _spawn["flags"]
        assert overrides["session"].send_input.call_args_list[0].args == (
            8446, "chief-1", "/chief", True,
        )

    def test_resumable_conversation_spawns_direct_resume_and_skips_chief_type(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir, _ready_session,
    ):
        client, app, overrides = webapp_client
        cfg = app.state.webapp_config
        Path(cfg.sessions_state_file).write_text(
            json.dumps({
                "old-chief-sess": _chief_state_row(
                    "chief-1", _recent_iso(minutes=5),
                ),
            }),
            encoding="utf-8",
        )
        resp = client.post("/api/board/chief/ensure", json={"resume": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["spawned"] is True
        assert body["resumed"] is True
        assert body["resume_fallback_reason"] == ""
        assert "--resume old-chief-sess" in _spawn["flags"]
        assert _spawn["label"] == "chief"
        # The conversation is already past its own boot — no /chief re-typed.
        overrides["session"].send_input.assert_not_called()

    def test_resume_stops_live_chief_first_then_reattaches_it(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir,
    ):
        """Resume repurposes the same stop-first ordering ``fresh`` uses
        (#633) — deliberately: the chief being stopped is exactly the
        conversation the state row (fresh off its own hook write) points
        back to, so this is a context-preserving restart, not an accident of
        shared plumbing."""
        client, app, overrides = webapp_client
        cfg = app.state.webapp_config
        overrides["session"].list_sessions.return_value = [_chief_row()]
        Path(cfg.sessions_state_file).write_text(
            json.dumps({
                "chief-old": _chief_state_row(
                    "chief-old", _recent_iso(minutes=5),
                ),
            }),
            encoding="utf-8",
        )
        probes = iter([{"alive": False}])

        def _get_session(port, sid):
            try:
                return next(probes)
            except StopIteration:
                return {"alive": True, "output_chars": 64}

        overrides["session"].get_session.side_effect = _get_session
        resp = client.post("/api/board/chief/ensure", json={"resume": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["resumed"] is True
        assert overrides["session"].stop.call_args_list[0].args == (
            8446, "chief-old", "quit"
        )
        assert "--resume chief-old" in _spawn["flags"]

    def test_resume_skips_the_live_blank_chief_for_the_real_conversation(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir, tmp_path,
    ):
        """End-to-end shape of the #670 incident: the chief alive right now is
        the blank one a fallback spawned after a deploy killed the real PTY.
        Stop-first preference must not hand it back — the conversation it
        displaced is the one to reattach."""
        client, app, overrides = webapp_client
        cfg = app.state.webapp_config
        overrides["session"].list_sessions.return_value = [_chief_row()]
        Path(cfg.sessions_state_file).write_text(
            json.dumps({
                "blank-conv": _chief_state_row(
                    "chief-old", _recent_iso(minutes=1),
                    transcript_path=_transcript(tmp_path, "blank", typed=False),
                ),
                "real-conv": _chief_state_row(
                    "dead-sess", _recent_iso(hours=3),
                    transcript_path=_transcript(tmp_path, "real", typed=True),
                ),
            }),
            encoding="utf-8",
        )
        probes = iter([{"alive": False}])

        def _get_session(port, sid):
            try:
                return next(probes)
            except StopIteration:
                return {"alive": True, "output_chars": 64}

        overrides["session"].get_session.side_effect = _get_session
        resp = client.post("/api/board/chief/ensure", json={"resume": True})
        assert resp.status_code == 200
        assert resp.json()["resumed"] is True
        assert "--resume real-conv" in _spawn["flags"]
        assert "blank-conv" not in _spawn["flags"]

    def test_resume_honors_chief_model(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir, _ready_session,
    ):
        client, app, overrides = webapp_client
        app.state.webapp_config.chief_model = "opus"
        cfg = app.state.webapp_config
        Path(cfg.sessions_state_file).write_text(
            json.dumps({
                "hook-row-1": _chief_state_row(
                    "old-chief-sess", "2026-07-27T07:14:05Z",
                ),
            }),
            encoding="utf-8",
        )
        client.post("/api/board/chief/ensure", json={"resume": True})
        assert "--model opus" in _spawn["flags"]


def _write_pointer(session_id, transcript_path, *, age=None, source="live"):
    """Put a pointer on disk with an explicit ``seen_at`` age.

    Writes raw rather than going through ``write_chief_pointer`` because these
    tests care about a pointer that was stamped days ago — the writer always
    stamps *now*. The path comes from the module global the autouse
    ``_isolated_chief_pointer`` fixture has already redirected at ``tmp_path``.
    """
    seen = datetime.now(timezone.utc) - (age or timedelta(minutes=1))
    chief_pointer.CHIEF_POINTER_FILE.write_text(
        json.dumps({
            "session_id": session_id,
            "transcript_path": str(transcript_path),
            "seen_at": seen.isoformat().replace("+00:00", "Z"),
            "source": source,
        }),
        encoding="utf-8",
    )


class TestResumeConsultsTheDurablePointer:
    """#675: the row scan can only see what ``sessions-state.json`` still
    holds. These pin the two conversations it structurally cannot answer for —
    a chief the launcher never spawned once its PTY is gone, and one dead past
    the hook writer's 24h prune — and pin that the pointer never wins where a
    live answer is better."""

    def test_hand_started_chief_is_resumable_after_its_pty_is_gone(
        self, tmp_path
    ):
        """Shape 1: never renamed, so its row reads ``fleet-config-79`` and the
        name scan skips it; no live PTY left to prefer either. The pointer
        written while it was alive is the only thing that can still find it."""
        transcript = _transcript(tmp_path, "hand", typed=True)
        rows = {
            "hand-conv": _chief_state_row(
                "hand-sess", _recent_iso(minutes=20),
                name="fleet-config-79", transcript_path=transcript,
            ),
        }
        _write_pointer("hand-conv", transcript)
        assert (
            chief_router._find_resumable_chief_session_id(rows) == "hand-conv"
        )

    def test_conversation_past_the_24h_row_prune_is_resumable(self, tmp_path):
        """Shape 2: no row left at all — the hook writer pruned it on the same
        24h horizon ``board.STATE_STALE_AFTER`` applies. The transcript is
        intact, so ``claude --resume`` still works and so must the launcher."""
        transcript = _transcript(tmp_path, "old", typed=True)
        _write_pointer("pruned-conv", transcript, age=timedelta(days=3))
        assert (
            chief_router._find_resumable_chief_session_id({}) == "pruned-conv"
        )

    def test_newer_substantive_chief_row_outranks_the_pointer(self, tmp_path):
        """The pointer joins the ranking, it doesn't jump it: a genuinely
        fresher confirmed chief conversation is still the better answer."""
        _write_pointer(
            "pointed-conv", _transcript(tmp_path, "pointed", typed=True),
            age=timedelta(days=2),
        )
        rows = {
            "fresh-conv": _chief_state_row(
                "fresh-sess", _recent_iso(minutes=5),
                transcript_path=_transcript(tmp_path, "fresh", typed=True),
            ),
        }
        assert (
            chief_router._find_resumable_chief_session_id(rows) == "fresh-conv"
        )

    def test_pointer_is_ranked_by_its_row_when_the_row_still_exists(
        self, tmp_path
    ):
        """A long-lived chief is pointed at once and never re-stamped, so its
        ``seen_at`` can be days old while the conversation is minutes fresh.
        Ranking it on the row it still has is what keeps an older
        launcher-spawned conversation from outranking the real current one."""
        transcript = _transcript(tmp_path, "hand", typed=True)
        _write_pointer("hand-conv", transcript, age=timedelta(days=3))
        rows = {
            "hand-conv": _chief_state_row(
                "hand-sess", _recent_iso(minutes=2),
                name="fleet-config-79", transcript_path=transcript,
            ),
            "older-conv": _chief_state_row(
                "older-sess", _recent_iso(hours=6),
                transcript_path=_transcript(tmp_path, "older", typed=True),
            ),
        }
        assert (
            chief_router._find_resumable_chief_session_id(rows) == "hand-conv"
        )

    def test_bootstrap_only_pointer_is_never_resumed(self, tmp_path):
        """#670's rule applies to the pointer too — reattaching a conversation
        holding nothing but its own ``/chief`` is worth less than the fresh
        spawn the caller falls back to."""
        _write_pointer(
            "blank-conv", _transcript(tmp_path, "blank", typed=False)
        )
        assert chief_router._find_resumable_chief_session_id({}) == ""

    def test_live_preferred_sid_still_wins_over_the_pointer(self, tmp_path):
        """Tier 1 is unchanged: the conversation behind the PTY this resume is
        about to stop is the most direct evidence there is."""
        _write_pointer(
            "pointed-conv", _transcript(tmp_path, "pointed", typed=True)
        )
        rows = {
            "live-conv": _chief_state_row(
                "live-sess", _recent_iso(minutes=1),
                name="fleet-config-79",
                transcript_path=_transcript(tmp_path, "live", typed=True),
            ),
        }
        assert chief_router._find_resumable_chief_session_id(
            rows, preferred_sid="live-conv"
        ) == "live-conv"

    def test_expired_pointer_is_ignored(self, tmp_path):
        _write_pointer(
            "old-conv", _transcript(tmp_path, "old", typed=True),
            age=timedelta(days=8),
        )
        assert chief_router._find_resumable_chief_session_id({}) == ""

    def test_pointer_at_a_deleted_transcript_is_ignored(self, tmp_path):
        transcript = Path(_transcript(tmp_path, "gone", typed=True))
        _write_pointer("gone-conv", transcript)
        transcript.unlink()
        assert chief_router._find_resumable_chief_session_id({}) == ""

    def test_missing_pointer_degrades_to_the_scan(self, tmp_path):
        """AC: a missing/corrupt pointer is never an error — the lookup is
        exactly what it was before #675."""
        chief_pointer.CHIEF_POINTER_FILE.write_text("{corrupt", encoding="utf-8")
        rows = {
            "real-conv": _chief_state_row(
                "real-sess", _recent_iso(hours=2),
                transcript_path=_transcript(tmp_path, "real", typed=True),
            ),
        }
        assert (
            chief_router._find_resumable_chief_session_id(rows) == "real-conv"
        )

    def test_pointer_naming_a_row_already_in_the_scan_is_harmless(
        self, tmp_path
    ):
        transcript = _transcript(tmp_path, "same", typed=True)
        _write_pointer("same-conv", transcript)
        rows = {
            "same-conv": _chief_state_row(
                "same-sess", _recent_iso(minutes=5), transcript_path=transcript,
            ),
        }
        assert (
            chief_router._find_resumable_chief_session_id(rows) == "same-conv"
        )


class TestNoteChiefConversation:
    """#675's write half: the pointer is recorded while the chief is alive —
    that is the only moment both shapes above are knowable — without putting
    file IO on ``GET /api/board``'s 5s poll."""

    async def _drain(self):
        """Await the fire-and-forget write task the noter schedules."""
        import asyncio
        pending = [
            task for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
        ]
        if pending:
            await asyncio.gather(*pending)

    async def test_live_chief_is_recorded_with_its_transcript(self, tmp_path):
        transcript = _transcript(tmp_path, "live", typed=True)
        rows = {
            "live-conv": _chief_state_row(
                "chief-old", _recent_iso(minutes=1), transcript_path=transcript,
            ),
        }
        chief_router._note_chief_conversation([_chief_row()], rows)
        await self._drain()
        pointer = chief_pointer.read_chief_pointer()
        assert pointer["session_id"] == "live-conv"
        assert pointer["transcript_path"] == transcript
        assert pointer["source"] == "live"

    async def test_self_healed_chief_is_recorded_too(self, tmp_path):
        """The case the pointer exists for: a chief the launcher never spawned
        carries no ``label`` of its own until ``_reconcile_chief_label`` heals
        it, and its row is named after the project."""
        transcript = _transcript(tmp_path, "hand", typed=True)
        rows = {
            "hand-conv": _chief_state_row(
                "chief-old", _recent_iso(minutes=1),
                name="fleet-config-79", transcript_path=transcript,
            ),
        }
        live = chief_router._reconcile_chief_labels(
            [_chief_row(
                label="", name="fleet-config", live_title="✳ chief",
                project_dir="E:/automation/fleet-config",
            )],
            rows,
        )
        chief_router._note_chief_conversation(live, rows)
        await self._drain()
        assert chief_pointer.read_chief_pointer()["session_id"] == "hand-conv"

    async def test_unchanged_chief_writes_nothing_on_the_next_poll(
        self, tmp_path, monkeypatch
    ):
        """The poll-budget contract: the memo means a steady-state
        ``GET /api/board`` does no pointer IO at all."""
        rows = {
            "live-conv": _chief_state_row(
                "chief-old", _recent_iso(minutes=1),
                transcript_path=_transcript(tmp_path, "live", typed=True),
            ),
        }
        writes = []
        monkeypatch.setattr(
            chief_pointer, "write_chief_pointer",
            lambda *a, **k: writes.append(a[0]),
        )
        for _ in range(3):
            chief_router._note_chief_conversation([_chief_row()], rows)
            await self._drain()
        assert writes == ["live-conv"]

    async def test_a_different_chief_conversation_rewrites_the_pointer(
        self, tmp_path, monkeypatch
    ):
        writes = []
        monkeypatch.setattr(
            chief_pointer, "write_chief_pointer",
            lambda *a, **k: writes.append(a[0]),
        )
        for conv in ("first-conv", "second-conv"):
            rows = {
                conv: _chief_state_row(
                    "chief-old", _recent_iso(minutes=1),
                    transcript_path=_transcript(tmp_path, conv, typed=True),
                ),
            }
            chief_router._note_chief_conversation([_chief_row()], rows)
            await self._drain()
        assert writes == ["first-conv", "second-conv"]

    async def test_no_live_chief_records_nothing(self, tmp_path):
        rows = {
            "some-conv": _chief_state_row(
                "other-sess", _recent_iso(minutes=1),
                transcript_path=_transcript(tmp_path, "some", typed=True),
            ),
        }
        chief_router._note_chief_conversation(
            [_chief_row(label="", name="fleet-config", alive=True)], rows
        )
        await self._drain()
        assert chief_pointer.read_chief_pointer() == {}

    async def test_row_without_a_transcript_records_nothing(self):
        """A pointer that can't be substance-checked or resumed is worth less
        than no pointer."""
        rows = {"live-conv": _chief_state_row("chief-old", _recent_iso(minutes=1))}
        chief_router._note_chief_conversation([_chief_row()], rows)
        await self._drain()
        assert chief_pointer.read_chief_pointer() == {}


class TestEnsureRefreshesThePointer:

    def test_resume_restamps_the_pointer_it_reattached(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir, _ready_session, tmp_path,
    ):
        """A conversation past the row prune has no row to re-derive from, so
        pressing Resume is the moment that restarts its 7-day clock."""
        client, _, _ = webapp_client
        transcript = _transcript(tmp_path, "pruned", typed=True)
        _write_pointer("pruned-conv", transcript, age=timedelta(days=6))
        resp = client.post("/api/board/chief/ensure", json={"resume": True})
        assert resp.status_code == 200
        assert resp.json()["resumed"] is True
        assert "--resume pruned-conv" in _spawn["flags"]
        pointer = chief_pointer.read_chief_pointer()
        assert pointer["session_id"] == "pruned-conv"
        assert pointer["source"] == "ensure-resume"
        # Restamped: it was 6 days old going in, and would expire in one more.
        assert (
            datetime.now(timezone.utc)
            - chief_router.board._parse_iso(pointer["seen_at"])
        ) < timedelta(minutes=5)

    def test_fresh_spawn_does_not_touch_the_pointer(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir, _ready_session, tmp_path,
    ):
        """Nothing was reattached, so nothing is known yet — the brand-new
        conversation's id only exists once a hook fires, and the poll-side
        noter records it then."""
        client, _, _ = webapp_client
        _write_pointer(
            "blank-conv", _transcript(tmp_path, "blank", typed=False)
        )
        resp = client.post("/api/board/chief/ensure", json={})
        assert resp.status_code == 200
        assert chief_pointer.read_chief_pointer().get("session_id") == (
            "blank-conv"
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
