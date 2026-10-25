"""POST /api/board/issues/start: the server-built /issue-<mode> <N> prompt, the dispatch brief and chief-managed marking."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest



# ------------------------------------------------------------- issue start


class TestIssueStart:

    @pytest.fixture
    def _spawn(self, webapp_client, monkeypatch):
        from app.webapp.routers import board as board_router
        captured: dict = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent, rows, cols,
                       history_lines=None):
            captured.update(
                project_dir=project_dir, name=name, flags=flags,
                port=port, kind=kind, agent=agent, rows=rows, cols=cols,
                history_lines=history_lines,
            )
            return {"session_id": "spawned-1", "kind": "pty", "name": name}

        monkeypatch.setattr(board_router, "spawn_claude_session", fake_spawn)
        return captured

    def test_builds_server_side_prompt(self, webapp_client, _bypass_gate, _spawn):
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "MyRepo", "number": 42, "mode": "start"},
        )
        assert resp.status_code == 200
        assert _spawn["flags"].endswith(' "/issue-start 42"')
        assert Path(_spawn["project_dir"]).name == "myrepo"
        assert _spawn["kind"] == "pty" and _spawn["agent"] == "claude"
        assert resp.json()["session"]["session_id"] == "spawned-1"

    def test_yolo_mode(self, webapp_client, _bypass_gate, _spawn):
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 7, "mode": "yolo"},
        )
        assert _spawn["flags"].endswith(' "/issue-yolo 7"')

    def test_rejects_bad_mode_and_number(self, webapp_client, _bypass_gate, _spawn):
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        base = {"repo": "myrepo", "number": 1, "mode": "start"}
        assert client.post(
            "/api/board/issues/start", json={**base, "mode": "add; rm -rf"}
        ).status_code == 400
        assert client.post(
            "/api/board/issues/start", json={**base, "number": "abc"}
        ).status_code == 400
        assert client.post(
            "/api/board/issues/start", json={**base, "number": -3}
        ).status_code == 400

    @pytest.mark.parametrize("model", ["sonnet", "opus", "fable"])
    def test_model_overrides_persisted_coding_model(
        self, webapp_client, _bypass_gate, _spawn, model
    ):
        """#505: the dispatch bar's selector governs one-tap starts too."""
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 9, "mode": "start",
                  "model": model},
        )
        assert f"--model {model}" in _spawn["flags"]
        assert _spawn["flags"].endswith(' "/issue-start 9"')
        assert _spawn["agent"] == "claude"

    def test_absent_model_keeps_persisted_coding_model(
        self, webapp_client, _bypass_gate, _spawn
    ):
        """No ``model`` (stale-cache client) → legacy behaviour: the
        persisted Coding model (opus in the test config), unchanged."""
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 9, "mode": "start"},
        )
        assert "--model opus" in _spawn["flags"]
        assert _spawn["agent"] == "claude"

    def test_explicit_codex_model_starts_with_positional_prompt(
        self, webapp_client, _bypass_gate, _spawn, monkeypatch
    ):
        """#845: an exact Codex model carries shared flags and the same
        server-built ``/issue-*`` positional prompt appended (Codex takes
        ``codex [OPTIONS] [PROMPT]`` like claude)."""
        from app.webapp.routers import board_spawn
        monkeypatch.setattr(
            board_spawn.agents, "is_installed", lambda a: a == "codex"
        )
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 7, "mode": "yolo",
                  "model": "codex:gpt-5.6-sol"},
        )
        assert resp.status_code == 200
        assert _spawn["agent"] == "codex" and _spawn["kind"] == "pty"
        assert "model_reasoning_effort=" in _spawn["flags"]
        assert "--model gpt-5.6-sol" in _spawn["flags"]
        assert _spawn["flags"].endswith(' "/issue-yolo 7"')

    def test_gpt56_without_codex_installed_400s(
        self, webapp_client, _bypass_gate, _spawn, monkeypatch
    ):
        from app.webapp.routers import board_spawn
        monkeypatch.setattr(
            board_spawn.agents, "is_installed", lambda a: False
        )
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 7, "mode": "start",
                  "model": "codex:gpt-5.6-sol"},
        )
        assert resp.status_code == 400
        assert "not installed" in resp.json()["detail"]
        assert not _spawn

    def test_unknown_model_400s(self, webapp_client, _bypass_gate, _spawn):
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 7, "mode": "start",
                  "model": "haiku"},
        )
        assert resp.status_code == 400
        assert "unknown model" in resp.json()["detail"]
        assert not _spawn

    def test_unknown_repo_is_404(self, webapp_client, _bypass_gate, _spawn):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "not-checked-out", "number": 1, "mode": "start"},
        )
        assert resp.status_code == 404

    def test_title_auto_names_the_spawned_session(
        self, webapp_client, _bypass_gate, _spawn
    ):
        """#467: a Board start carrying the issue title renames the spawned
        session after it, via the #458 manual-override path (a launcher-side
        ``manual_title`` set — no PTY typing, so no readiness wait)."""
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        resp = client.post(
            "/api/board/issues/start",
            json={
                "repo": "myrepo", "number": 42, "mode": "start",
                "title": "Board tab: auto-name a started session",
            },
        )
        assert resp.status_code == 200
        assert '--name "Board tab: auto-name a started session"' in _spawn["flags"]
        overrides["session"].rename.assert_called_once_with(
            8446, "spawned-1", "Board tab: auto-name a started session"
        )

    def test_unsafe_title_remains_launcher_only(
        self, webapp_client, _bypass_gate, _spawn
    ):
        """A shell-sensitive issue title never reaches a native CLI flag."""
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        title = "docs & release"
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 42, "mode": "start", "title": title},
        )
        assert resp.status_code == 200
        assert "--name" not in _spawn["flags"]
        overrides["session"].rename.assert_called_once_with(8446, "spawned-1", title)

    def test_codex_title_remains_launcher_only(
        self, webapp_client, _bypass_gate, _spawn, monkeypatch
    ):
        """Codex exposes no verified spawn-time session-name interface."""
        from app.webapp.routers import board_spawn

        monkeypatch.setattr(
            board_spawn.agents, "is_installed", lambda agent: agent == "codex"
        )
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        resp = client.post(
            "/api/board/issues/start",
            json={
                "repo": "myrepo", "number": 42, "mode": "start",
                "model": "codex:gpt-5.6-luna", "title": "Codex title",
            },
        )
        assert resp.status_code == 200
        assert "--name" not in _spawn["flags"]
        overrides["session"].rename.assert_called_once_with(
            8446, "spawned-1", "Codex title"
        )

    def test_blank_title_skips_rename(
        self, webapp_client, _bypass_gate, _spawn
    ):
        """No title (or whitespace-only) → no rename call; the session keeps
        its automatic title precedence."""
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 42, "mode": "start", "title": "   "},
        )
        assert resp.status_code == 200
        overrides["session"].rename.assert_not_called()

    def test_rename_failure_does_not_fail_the_launch(
        self, webapp_client, _bypass_gate, _spawn
    ):
        """A rename error is best-effort — the launch still succeeds (#467)."""
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        overrides["session"].rename.side_effect = (
            overrides["session"].SessionHostError("boom", 502)
        )
        resp = client.post(
            "/api/board/issues/start",
            json={
                "repo": "myrepo", "number": 42, "mode": "yolo",
                "title": "some issue title",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["session"]["session_id"] == "spawned-1"


class TestIssueStartBrief:
    """#1114: an optional dispatch brief rides the launch command as a
    launcher-generated ``--brief <path>`` — never as client text."""

    _spawn = TestIssueStart._spawn

    @pytest.fixture(autouse=True)
    def _briefs_dir(self, tmp_path, monkeypatch):
        from src import dispatch_brief
        d = tmp_path / "briefs"
        monkeypatch.setattr(dispatch_brief, "BRIEFS_DIR", d)
        return d

    def _start(self, client, overrides, **extra):
        (overrides["tmp_projects_dir"] / "myrepo").mkdir(exist_ok=True)
        return client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 42, "mode": "yolo", **extra},
        )

    def test_hostile_brief_never_reaches_the_command_line(
        self, webapp_client, _bypass_gate, _spawn, _briefs_dir
    ):
        client, _, overrides = webapp_client
        brief = 'ship it"; rm -rf / & echo "pwned\nsecond line ^| %PATH% `x`'
        resp = self._start(client, overrides, brief=brief)
        assert resp.status_code == 200
        flags = _spawn["flags"]
        tail = re.search(r' "/issue-yolo 42 --brief ([^"]+)"$', flags)
        assert tail, flags
        path = Path(tail.group(1))
        assert re.fullmatch(r"[0-9a-f]{32}\.md", path.name)
        assert path.parent == _briefs_dir
        assert re.fullmatch(r"[A-Za-z0-9:/._-]+", tail.group(1))
        for fragment in ('rm -rf', 'pwned', '\n', '^|', '%PATH%', 'ship it'):
            assert fragment not in flags
        assert path.read_text(encoding="utf-8") == brief
        assert resp.json()["launched"] == f"/issue-yolo 42 --brief {tail.group(1)}"

    def test_null_brief_is_absent(self, webapp_client, _bypass_gate, _spawn, _briefs_dir):
        client, _, overrides = webapp_client
        assert self._start(client, overrides, brief=None).status_code == 200
        assert _spawn["flags"].endswith(' "/issue-yolo 42"')
        assert not _briefs_dir.exists()

    @pytest.mark.parametrize(
        "brief,detail",
        [
            ("", "brief is empty"),
            ("  \n\t ", "brief is empty"),
            (123, "brief must be a string"),
            ("x" * 20_001, "brief too large: 20001 chars (max 20000)"),
        ],
        ids=["empty", "whitespace", "not-a-string", "oversize"],
    )
    def test_bad_brief_400s_before_spawning(
        self, webapp_client, _bypass_gate, _spawn, _briefs_dir, brief, detail
    ):
        client, _, overrides = webapp_client
        resp = self._start(client, overrides, brief=brief)
        assert resp.status_code == 400
        assert resp.json()["detail"] == detail
        assert not _spawn
        assert not _briefs_dir.exists()

    def test_failed_spawn_discards_its_brief(
        self, webapp_client, _bypass_gate, monkeypatch, _briefs_dir
    ):
        from app.webapp.routers import board as board_router

        def boom(*a, **k):
            raise OSError("no such executable")

        monkeypatch.setattr(board_router, "spawn_claude_session", boom)
        client, _, overrides = webapp_client
        resp = self._start(client, overrides, brief="scope: just #42")
        assert resp.status_code == 400
        assert list(_briefs_dir.glob("*.md")) == []

    def test_audit_records_size_never_content(
        self, webapp_client, _bypass_gate, _spawn
    ):
        from app.webapp.routers import board as board_router
        client, _, overrides = webapp_client
        brief = "SECRET-SCOPE-MARKER only #42"
        assert self._start(client, overrides, brief=brief).status_code == 200
        call = board_router.audit.audit_event.call_args
        assert call.kwargs["brief_chars"] == len(brief)
        assert "SECRET-SCOPE-MARKER" not in repr(board_router.audit.mock_calls)


# ---------------------------------------------- chief-managed marking (#474)
#
# `start_issue` is the path chief actually calls over loopback -- never
# through `chief_ops.py dispatch`'s own CLI-side marking, so without this
# the worker it spawns never got a `chief-managed.json` entry. Gated on a
# live chief PTY session being present so a human driving the Board on this
# same machine never gets their own dispatch marked.


class TestChiefManagedMarking:

    @pytest.fixture
    def _spawn(self, webapp_client, monkeypatch):
        from app.webapp.routers import board as board_router
        captured: dict = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent, rows, cols,
                       history_lines=None):
            captured.update(name=name)
            return {"session_id": "spawned-1", "kind": "pty", "name": name}

        monkeypatch.setattr(board_router, "spawn_claude_session", fake_spawn)
        return captured

    @pytest.fixture
    def _fleet_config_repo(self, webapp_client):
        """A resolvable ``fleet-config`` project dir with a fake venv python
        and `chief_managed.py`, so `_resolve_repo_entry` + the two
        `.exists()` checks in `_mark_chief_managed` succeed without ever
        really invoking a subprocess (`subprocess.run` is faked below)."""
        _, _, overrides = webapp_client
        repo = overrides["tmp_projects_dir"] / "fleet-config"
        (repo / ".venv" / "Scripts").mkdir(parents=True)
        (repo / ".venv" / "Scripts" / "python.exe").touch()
        (repo / "skills" / "_lib").mkdir(parents=True)
        (repo / "skills" / "_lib" / "chief_managed.py").touch()
        return repo

    @pytest.fixture
    def _fake_run(self, monkeypatch):
        from app.webapp.routers import board_chief
        calls: list = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return MagicMock(returncode=0)

        monkeypatch.setattr(board_chief.subprocess, "run", fake_run)
        return calls

    def _set_live_sessions(self, overrides, sessions):
        overrides["session"].list_sessions.return_value = sessions

    @pytest.fixture
    def _mirror_asked(self, monkeypatch):
        """Each launch that got as far as the PC-window decision (#1283).
        The probe answers no, so no window is ever spawned by a test."""
        from app.webapp.routers import _helpers
        asked: list = []
        monkeypatch.setattr(
            _helpers, "should_mirror_to_pc",
            lambda show, request, body: asked.append(dict(body)) or False,
        )
        return asked

    def test_marks_when_chief_alive_and_loopback(
        self, webapp_client, _bypass_gate, _spawn, _fleet_config_repo, _fake_run,
        _mirror_asked,
    ):
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        self._set_live_sessions(overrides, [
            {"session_id": "chief-1", "kind": "pty", "alive": True, "label": "chief"},
        ])
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 42, "mode": "start"},
        )
        assert resp.status_code == 200
        assert len(_fake_run) == 1
        cmd = _fake_run[0]
        assert cmd[2:] == ["mark", "spawned-1", "myrepo", "42"]
        assert cmd[0].endswith("python.exe")
        assert cmd[1].endswith("chief_managed.py")
        # #1283: `chief_ops.py dispatch` lands here, and opens no PC window.
        assert _mirror_asked == []
        # Roberto's own ▶ from a browser still gets the usual rule.
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 43, "mode": "start", "in_page": True},
        )
        assert resp.status_code == 200
        assert [b.get("in_page") for b in _mirror_asked] == [True]

    def test_does_not_mark_without_a_live_chief_session(
        self, webapp_client, _bypass_gate, _spawn, _fleet_config_repo, _fake_run,
        _mirror_asked,
    ):
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        self._set_live_sessions(overrides, [])  # no chief session alive
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 42, "mode": "start"},
        )
        assert resp.status_code == 200
        assert _fake_run == []
        # No chief behind it: the usual PC-window rule still decides.
        assert len(_mirror_asked) == 1

    def test_marking_failure_does_not_fail_the_launch(
        self, webapp_client, _bypass_gate, _spawn, _fleet_config_repo, monkeypatch,
    ):
        """Mirrors `test_rename_failure_does_not_fail_the_launch` -- a
        subprocess error while marking is best-effort, never fatal to the
        dispatch (matches `chief_ops.py cmd_dispatch`'s own try/except)."""
        from app.webapp.routers import board_chief

        def raising_run(cmd, **kwargs):
            raise OSError("boom")

        monkeypatch.setattr(board_chief.subprocess, "run", raising_run)
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        self._set_live_sessions(overrides, [
            {"session_id": "chief-1", "kind": "pty", "alive": True, "label": "chief"},
        ])
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 42, "mode": "start"},
        )
        assert resp.status_code == 200
        assert resp.json()["session"]["session_id"] == "spawned-1"

    # #1579: a chief-dispatched lane is named after its issue, not after the
    # launch command the first-prompt capture would otherwise read.

    def _dispatch(self, webapp_client, monkeypatch, *, chief: bool, cached,
                  body_extra=None):
        from src import github_client

        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir(exist_ok=True)
        monkeypatch.setattr(
            github_client, "snapshot", lambda: {"issues": cached}
        )
        self._set_live_sessions(overrides, [
            {"session_id": "chief-1", "kind": "pty", "alive": True,
             "label": "chief"},
        ] if chief else [])
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 42, "mode": "start",
                  **(body_extra or {})},
        )
        assert resp.status_code == 200
        return overrides["session"].rename

    _CACHED = [
        {"repo": "other", "number": 42, "title": "Wrong repo"},
        {"repo": "MyRepo", "number": 42, "title": "Stop: vendored recipe"},
    ]

    def test_chief_dispatch_is_named_repo_number_and_cached_title(
        self, webapp_client, _bypass_gate, _spawn, _fleet_config_repo,
        _fake_run, _mirror_asked, monkeypatch,
    ):
        rename = self._dispatch(
            webapp_client, monkeypatch, chief=True, cached=self._CACHED)
        rename.assert_called_once_with(
            8446, "spawned-1", "myrepo#42 Stop: vendored recipe")

    def test_chief_dispatch_without_a_cached_title_falls_back_to_repo_number(
        self, webapp_client, _bypass_gate, _spawn, _fleet_config_repo,
        _fake_run, _mirror_asked, monkeypatch,
    ):
        rename = self._dispatch(
            webapp_client, monkeypatch, chief=True, cached=[])
        rename.assert_called_once_with(8446, "spawned-1", "myrepo#42")

    def test_manual_start_keeps_todays_naming(
        self, webapp_client, _bypass_gate, _spawn, _fleet_config_repo,
        _fake_run, _mirror_asked, monkeypatch,
    ):
        # No chief alive: no title -> no rename at all; a title -> the title.
        rename = self._dispatch(
            webapp_client, monkeypatch, chief=False, cached=self._CACHED)
        rename.assert_not_called()
        rename = self._dispatch(
            webapp_client, monkeypatch, chief=False, cached=self._CACHED,
            body_extra={"title": "Card title"})
        rename.assert_called_once_with(8446, "spawned-1", "Card title")
