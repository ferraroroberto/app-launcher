"""Life OS API: the path jail, recap staleness, the skill list, skill launch and the content gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.webapp.routers.life_os import _recap_staleness
from app.webapp.routers.life_os_files import resolve_within


# --------------------------------------------------------------- path jail
class TestResolveWithin:
    def test_accepts_simple_relative_path(self, tmp_path: Path):
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "f.md").write_text("hi", encoding="utf-8")
        out = resolve_within(tmp_path, "a/f.md")
        assert out is not None and out.name == "f.md"

    def test_rejects_parent_traversal(self, tmp_path: Path):
        root = tmp_path / "life-os"
        root.mkdir()
        (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
        assert resolve_within(root, "../secret.txt") is None

    def test_rejects_absolute_path(self, tmp_path: Path):
        root = tmp_path / "life-os"
        root.mkdir()
        # An absolute path joined under the root resolves outside it.
        assert resolve_within(root, str(tmp_path / "secret.txt")) is None

    def test_rejects_empty(self, tmp_path: Path):
        assert resolve_within(tmp_path, "") is None


# ------------------------------------------------------- recap staleness (#167)
class TestRecapStaleness:
    """Pure threshold mapping — amber past 7 days, red past 14."""

    def test_never_when_no_ledger(self):
        assert _recap_staleness(None) == "never"

    def test_fresh_inclusive_of_7_days(self):
        assert _recap_staleness(0.0) == "fresh"
        assert _recap_staleness(7.0) == "fresh"

    def test_due_just_past_7(self):
        assert _recap_staleness(7.01) == "due"
        assert _recap_staleness(14.0) == "due"

    def test_overdue_past_14(self):
        assert _recap_staleness(14.01) == "overdue"
        assert _recap_staleness(99.0) == "overdue"


# --------------------------------------------------------------- list
class TestListSkills:
    def test_lists_skills(self, life_os_client):
        client, _, _ = life_os_client
        resp = client.get("/api/life-os/skills")
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is True
        ids = [s["id"] for s in body["skills"]]
        assert ids == ["journal-daily"]
        assert body["skills"][0]["command"] == "journal-daily"

    def test_unavailable_when_dir_missing(self, webapp_client, tmp_path):
        client, app, _ = webapp_client
        app.state.webapp_config.life_os_dir = str(tmp_path / "nope")
        resp = client.get("/api/life-os/skills")
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is False
        assert body["skills"] == []


# --------------------------------------------------------------- launch
class TestLaunchSkill:

    @pytest.fixture(autouse=True)
    def _bypass_gate(self, monkeypatch):
        """Treat the TestClient host as loopback: the launch is passkey-gated
        since #1036, and the gate itself is covered by
        test_launch_routes_are_passkey_gated."""
        from app.webapp import middleware
        monkeypatch.setattr(
            middleware.trust,
            "LOOPBACK_HOSTS",
            frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
        )

    def test_launch_pty_sonnet_appends_skill_command(
        self, life_os_client, monkeypatch
    ):
        client, _, _ = life_os_client
        from app.webapp.routers import life_os_spawn

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            captured.update(
                project_dir=str(project_dir), flags=flags, kind=kind, agent=agent
            )
            return {"session_id": "s1", "kind": kind}

        monkeypatch.setattr(life_os_spawn, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={"mode": "pty", "opus": False},
        )
        assert resp.status_code == 200, resp.text
        # cwd is the life-os root; bare /skill is the positional prompt;
        # opus off → sonnet; agent is always claude.
        assert captured["agent"] == "claude"
        assert captured["kind"] == "pty"
        assert captured["flags"].endswith(" /journal-daily")
        assert "--model sonnet" in captured["flags"]
        assert "--remote-control" in captured["flags"]

    # ---- the launch audit+mirror tail (#1003) -------------------------
    #
    # Nothing pinned these three behaviours before, so routing Life OS
    # through the shared _helpers tail could have changed any of them and
    # the suite would still have been green. They are the whole difference
    # between the old private copy and the shared helper, so they are what
    # the parameters (`kind`, `resume_sid`) have to carry.

    def _spawn_and_capture_tail(self, monkeypatch, body):
        """POST a Life OS launch, returning (audit_mock, mirror_consulted)."""
        from unittest.mock import MagicMock

        from app.webapp.routers import _helpers, life_os_spawn

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            return {"session_id": "s1", "kind": kind}

        monkeypatch.setattr(life_os_spawn, "spawn_claude_session", fake_spawn)
        audit_mock = MagicMock()
        monkeypatch.setattr(life_os_spawn, "audit", audit_mock)
        # `kind == "pty" and should_mirror_to_pc(...)` short-circuits, so
        # whether this is consulted at all *is* the kind guard — and it is
        # deterministic, unlike the mirror's own fire-and-forget task.
        consulted = []

        def _probe(*args, **kwargs):
            consulted.append(True)
            return False

        # Patch it wherever it is *looked up* from, so this pins the
        # behaviour and not the module the call currently lives in — that is
        # what lets the same test run against both the pre- and post-#1003
        # implementations and prove they agree.
        for mod in (_helpers, life_os_spawn):
            if hasattr(mod, "should_mirror_to_pc"):
                monkeypatch.setattr(mod, "should_mirror_to_pc", _probe)
        return audit_mock, consulted

    def _audit_event_call(self, audit_mock):
        for call in audit_mock.audit_event.call_args_list:
            if call.args and call.args[0] in ("session_start", "remote_launch"):
                return call
        raise AssertionError(
            f"no launch audit event recorded: {audit_mock.audit_event.call_args_list}"
        )

    def test_pty_launch_audits_session_start_and_checks_the_mirror(
        self, life_os_client, monkeypatch
    ):
        client, _, _ = life_os_client
        audit_mock, consulted = self._spawn_and_capture_tail(monkeypatch, None)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch", json={"mode": "pty"},
        )
        assert resp.status_code == 200, resp.text
        call = self._audit_event_call(audit_mock)
        assert call.args[0] == "session_start", call
        assert consulted, "a PTY launch must consult should_mirror_to_pc"

    def test_remote_launch_audits_remote_launch_and_never_mirrors(
        self, life_os_client, monkeypatch
    ):
        """A detached session has no window to mirror, and its audit line is
        ``remote_launch``, not ``session_start``."""
        client, _, _ = life_os_client
        audit_mock, consulted = self._spawn_and_capture_tail(monkeypatch, None)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch", json={"mode": "remote"},
        )
        assert resp.status_code == 200, resp.text
        call = self._audit_event_call(audit_mock)
        assert call.args[0] == "remote_launch", call
        assert not consulted, (
            "a remote launch must not reach should_mirror_to_pc — the "
            "`kind == \"pty\"` guard short-circuits before it"
        )

    def test_launch_audit_line_carries_resume_sid(
        self, life_os_client, monkeypatch
    ):
        """``resume_sid`` (#727) records which conversation was reattached;
        it is the second field the shared tail had to grow to absorb this
        call site."""
        client, _, _ = life_os_client
        audit_mock, _ = self._spawn_and_capture_tail(monkeypatch, None)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch", json={"mode": "pty"},
        )
        assert resp.status_code == 200, resp.text
        call = self._audit_event_call(audit_mock)
        assert "resume_sid" in call.kwargs, call

    def test_launch_threads_phone_terminal_size(
        self, life_os_client, monkeypatch
    ):
        """Issue #374: the phone's rows/cols must size the PTY at spawn.

        A skill streams output the moment the PTY exists; spawning at the
        legacy 40×120 poured 120-col text that re-wrapped into garble when
        the overlay's first fit() shrank the PTY to phone width. Same
        contract as the Coding-tab launch route (issue #126).
        """
        client, _, _ = life_os_client
        from app.webapp.routers import life_os_spawn

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            captured.update(rows=rows, cols=cols)
            return {"session_id": "s1", "kind": kind}

        monkeypatch.setattr(life_os_spawn, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={"mode": "pty", "opus": False, "rows": 44, "cols": 54},
        )
        assert resp.status_code == 200, resp.text
        assert captured["rows"] == 44
        assert captured["cols"] == 54

    def test_launch_defaults_size_when_omitted(
        self, life_os_client, monkeypatch
    ):
        """Desktop launches send no size — the legacy 40×120 still applies."""
        client, _, _ = life_os_client
        from app.webapp.routers import life_os_spawn

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            captured.update(rows=rows, cols=cols)
            return {"session_id": "s1", "kind": kind}

        monkeypatch.setattr(life_os_spawn, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={"mode": "pty", "opus": False},
        )
        assert resp.status_code == 200, resp.text
        assert captured["rows"] == 40
        assert captured["cols"] == 120

    def test_launch_opus_overrides_model(self, life_os_client, monkeypatch):
        client, _, _ = life_os_client
        from app.webapp.routers import life_os_spawn

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            captured["flags"] = flags
            return {"session_id": "s1", "kind": kind}

        monkeypatch.setattr(life_os_spawn, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={"opus": True},
        )
        assert resp.status_code == 200, resp.text
        assert "--model opus" in captured["flags"]

    @pytest.mark.parametrize("model", ["sonnet", "opus", "fable"])
    def test_launch_model_field_sets_model_flag(
        self, life_os_client, monkeypatch, model
    ):
        """#540: the model combo sends an explicit ``model`` — each of the three
        offered Claude tiers maps to its ``--model`` flag, and the response
        echoes it back."""
        client, _, _ = life_os_client
        from app.webapp.routers import life_os_spawn

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            captured["flags"] = flags
            return {"session_id": "s1", "kind": kind}

        monkeypatch.setattr(life_os_spawn, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={"mode": "pty", "model": model},
        )
        assert resp.status_code == 200, resp.text
        assert f"--model {model}" in captured["flags"]
        assert resp.json()["model"] == model

    def test_launch_codex_astra_reads_project_skill(self, life_os_client, monkeypatch):
        """The Skills selector can launch the same skill through Codex/Astra."""
        client, _, _ = life_os_client
        from app.webapp.routers import life_os_spawn

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            captured.update(flags=flags, agent=agent)
            return {"session_id": "s1", "kind": kind}

        monkeypatch.setattr(life_os_spawn, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={"mode": "pty", "model": "codex:gpt-6-astra"},
        )
        assert resp.status_code == 200, resp.text
        assert captured["agent"] == "codex"
        assert "--model gpt-6-astra" in captured["flags"]
        assert "Use the journal-daily skill" in captured["flags"]
        assert ".claude/skills/journal-daily/SKILL.md" in captured["flags"]
        assert resp.json()["model"] == "gpt-6-astra"

    def test_launch_model_field_wins_over_legacy_opus(
        self, life_os_client, monkeypatch
    ):
        """When both are sent, the explicit ``model`` takes precedence over the
        legacy ``opus`` bool (#540 back-compat resolution order)."""
        client, _, _ = life_os_client
        from app.webapp.routers import life_os_spawn

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            captured["flags"] = flags
            return {"session_id": "s1", "kind": kind}

        monkeypatch.setattr(life_os_spawn, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={"model": "fable", "opus": True},
        )
        assert resp.status_code == 200, resp.text
        assert "--model fable" in captured["flags"]
        assert "--model opus" not in captured["flags"]

    def test_launch_rejects_unknown_model(self, life_os_client):
        """An out-of-range model is a 400, not a silent fallback (#540)."""
        client, _, _ = life_os_client
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={"model": "gpt5.6"},
        )
        assert resp.status_code == 400, resp.text

    def test_launch_resume_streams_pty_when_detached_off(
        self, life_os_client, monkeypatch
    ):
        """Resume reopens Claude's picker inside a remote-enabled session.

        Issue #526: CLI startup shape ``--resume --remote-control`` can leave
        the selected conversation unavailable on mobile. Launch Remote Control
        normally, then invoke the native picker with positional ``/resume``.
        """
        client, _, _ = life_os_client
        from app.webapp.routers import life_os_spawn

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            captured.update(flags=flags, kind=kind, agent=agent)
            return {"session_id": "s1", "kind": kind}

        monkeypatch.setattr(life_os_spawn, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={"mode": "pty", "resume": True, "opus": True},
        )
        assert resp.status_code == 200, resp.text
        assert captured["kind"] == "pty"
        assert captured["agent"] == "claude"
        assert "--remote-control" in captured["flags"]
        assert captured["flags"].endswith(" /resume")
        assert "--resume" not in captured["flags"]
        assert "/journal-daily" not in captured["flags"]
        # The opus override still applies to the resumed session's model.
        assert "--model opus" in captured["flags"]
        assert resp.json()["resume"] is True

    def test_launch_resume_with_detached_renders_in_remote_console(
        self, life_os_client, monkeypatch
    ):
        """Detached + Resume are orthogonal (issue #157, matching the Coding
        tab): a resume with mode=remote honours the requested mode and spawns
        a detached console (kind=remote), still invoking the positional
        ``/resume`` picker and dropping the /<skill> prompt."""
        client, _, _ = life_os_client
        from app.webapp.routers import life_os_spawn

        captured = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None):
            captured.update(flags=flags, kind=kind, agent=agent)
            return {"session_id": "s1", "kind": kind}

        monkeypatch.setattr(life_os_spawn, "spawn_claude_session", fake_spawn)
        resp = client.post(
            "/api/life-os/skills/journal-daily/launch",
            json={"mode": "remote", "resume": True, "opus": True},
        )
        assert resp.status_code == 200, resp.text
        assert captured["kind"] == "remote"
        assert captured["agent"] == "claude"
        assert "--remote-control" in captured["flags"]
        assert captured["flags"].endswith(" /resume")
        assert "--resume" not in captured["flags"]
        assert "/journal-daily" not in captured["flags"]
        # The opus override still applies to the resumed session's model.
        assert "--model opus" in captured["flags"]
        assert resp.json()["resume"] is True

    def test_launch_unknown_skill_404(self, life_os_client):
        client, _, _ = life_os_client
        resp = client.post("/api/life-os/skills/does-not-exist/launch", json={})
        assert resp.status_code == 404


# --------------------------------------------------------------- gating
class TestContentGate:
    def test_files_refused_over_cloudflare(self, life_os_client):
        client, _, _ = life_os_client
        resp = client.get(
            "/api/life-os/skills/journal-daily/files",
            headers={"Cf-Ray": "abc-123"},
        )
        assert resp.status_code == 403
        assert "public tunnel" in resp.json()["detail"].lower()

    def test_file_refused_off_tailnet(self, life_os_client):
        client, _, _ = life_os_client
        # TestClient connects as host 'testclient' (not loopback, not
        # tailnet) → the terminal gate refuses it.
        resp = client.get("/api/life-os/file?path=identity/who-i-am.md")
        assert resp.status_code == 403
