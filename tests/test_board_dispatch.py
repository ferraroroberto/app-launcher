"""Board dispatch (issue #302) — the spawn-then-type endpoint.

The injection-safety contract under test: the free-text goal must reach the
agent **only** through the PTY input path (bracketed-paste framed, CR as its
own second write — the #64/#166 two-frame rule) and must never appear in the
flags string that the session-host interpolates into its unquoted ``cmd /c``
line. Plus the readiness probe: no typing until the agent painted its first
output (``output_chars``), a clean 504 + kill of the half-spawned session on
timeout or death, the fixed-grace fallback for a live session-host old enough
to not report ``output_chars`` yet, and (#549) the shared PTY-quiescence wait
that stops the submitting CR from being typed — and swallowed — while the
freshly spawned agent's boot output is still growing.
"""

from __future__ import annotations

import pytest

from app.webapp.routers import board as board_router


@pytest.fixture
def _bypass_gate(monkeypatch):
    """Treat the TestClient host as loopback so the gated endpoint logic is
    exercised (the gate itself is covered by TestDispatchGate)."""
    from app.webapp import middleware
    monkeypatch.setattr(
        middleware,
        "LOOPBACK_HOSTS",
        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )


@pytest.fixture
def _fast_probe(monkeypatch):
    """Shrink the readiness + quiescence constants so no test ever really
    waits (#549: dispatch now also awaits PTY quiescence before typing)."""
    monkeypatch.setattr(board_router, "DISPATCH_READY_CAP_S", 0.3)
    monkeypatch.setattr(board_router, "DISPATCH_SETTLE_S", 0.0)
    monkeypatch.setattr(board_router, "DISPATCH_POLL_S", 0.01)
    monkeypatch.setattr(board_router, "DISPATCH_LEGACY_GRACE_S", 0.0)
    monkeypatch.setattr(board_router, "PTY_QUIESCENT_STABLE_S", 0.02)
    monkeypatch.setattr(board_router, "PTY_QUIESCENT_CAP_S", 0.3)
    monkeypatch.setattr(board_router, "PTY_QUIESCENT_POLL_S", 0.01)


@pytest.fixture
def _spawn(webapp_client, monkeypatch):
    """Capture the spawn call; the goal must never show up in ``flags``."""
    captured: dict = {}

    def fake_spawn(project_dir, name, flags, port, kind, agent, rows, cols,
                    history_lines=None):
        captured.update(
            project_dir=project_dir, name=name, flags=flags,
            port=port, kind=kind, agent=agent, rows=rows, cols=cols,
            history_lines=history_lines,
        )
        return {"session_id": "disp-1", "kind": "pty", "name": name}

    monkeypatch.setattr(board_router, "spawn_claude_session", fake_spawn)
    return captured


def _dispatch(client, overrides, **kwargs):
    (overrides["tmp_projects_dir"] / "myrepo").mkdir(exist_ok=True)
    payload = {"repo": "myrepo", "goal": "add dark mode", "mode": "add"}
    payload.update(kwargs)
    return client.post("/api/board/dispatch", json=payload)


@pytest.fixture
def _ready_session(webapp_client):
    """Session-host says the spawned agent is alive and already painting."""
    _, _, overrides = webapp_client
    overrides["session"].get_session.return_value = {
        "alive": True, "output_chars": 64,
    }
    return overrides


class TestDispatchGate:

    def test_dispatch_classified_passkey(self):
        from app.webapp.middleware import _terminal_guard_level
        assert _terminal_guard_level("/api/board/dispatch") == "passkey"

    def test_dispatch_refused_off_tailnet(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/board/dispatch",
            json={"repo": "x", "goal": "g", "mode": "add"},
        )
        assert resp.status_code == 403


class TestSpawnThenType:

    def test_goal_rides_pty_never_flags(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn, _ready_session
    ):
        client, _, overrides = webapp_client
        goal = 'wire the & thing | pipe > "quoted" fast'
        resp = _dispatch(client, overrides, goal=goal)
        assert resp.status_code == 200
        # The spawn is prompt-free: no goal fragment, no trailing quoted
        # positional prompt (the issues/start shape) in the flags string.
        assert "wire the" not in _spawn["flags"]
        assert not _spawn["flags"].rstrip().endswith('"')
        assert _spawn["kind"] == "pty" and _spawn["agent"] == "claude"
        # The goal arrives byte-identical, in a single call — framing and
        # the submit CR are now the session-host's own job (#611).
        calls = overrides["session"].send_input.call_args_list
        assert len(calls) == 1
        assert calls[0].args == (8446, "disp-1", "/issue-add " + goal, True)
        assert resp.json()["launched"] == "/issue-add " + goal

    @pytest.mark.parametrize("mode,command", [
        ("add", "/issue-add"),
        ("build", "/issue-add now"),
        ("yolo", "/issue-yolo"),
    ])
    def test_mode_mapping(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _ready_session, mode, command,
    ):
        client, _, overrides = webapp_client
        _dispatch(client, overrides, goal="ship it", mode=mode)
        first_write = overrides["session"].send_input.call_args_list[0].args[2]
        assert first_write == command + " ship it"

    @pytest.mark.parametrize("model", ["sonnet", "opus", "fable"])
    def test_claude_models_map_to_model_flag(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _ready_session, model,
    ):
        client, _, overrides = webapp_client
        _dispatch(client, overrides, model=model)
        assert f"--model {model}" in _spawn["flags"]
        assert _spawn["agent"] == "claude"

    def test_model_defaults_to_sonnet(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn, _ready_session
    ):
        client, _, overrides = webapp_client
        _dispatch(client, overrides)
        assert "--model sonnet" in _spawn["flags"]
        assert _spawn["agent"] == "claude"

    def test_gpt56_spawns_codex_with_coding_tab_flags(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _ready_session, monkeypatch,
    ):
        """#500: gpt5.6 = the Coding tab's Codex launch — agent codex,
        effort-only flags (Codex has no --model), same /issue-* typing."""
        monkeypatch.setattr(
            board_router.agents, "is_installed", lambda a: a == "codex"
        )
        client, _, overrides = webapp_client
        resp = _dispatch(client, overrides, model="gpt5.6")
        assert resp.status_code == 200
        assert _spawn["agent"] == "codex" and _spawn["kind"] == "pty"
        assert "model_reasoning_effort=" in _spawn["flags"]
        assert "--model" not in _spawn["flags"]
        # The goal still rides the PTY path, one call, submit=True.
        calls = overrides["session"].send_input.call_args_list
        assert len(calls) == 1
        assert calls[0].args[2].startswith("/issue-add ")
        assert calls[0].args[3] is True

    def test_gpt56_without_codex_installed_400s_before_spawn(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn, monkeypatch
    ):
        monkeypatch.setattr(
            board_router.agents, "is_installed", lambda a: False
        )
        client, _, overrides = webapp_client
        resp = _dispatch(client, overrides, model="gpt5.6")
        assert resp.status_code == 400
        assert "not installed" in resp.json()["detail"]
        assert not _spawn  # nothing ever spawned

    def test_unknown_model_400s_before_spawn(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn
    ):
        client, _, overrides = webapp_client
        resp = _dispatch(client, overrides, model="haiku")
        assert resp.status_code == 400
        assert "unknown model" in resp.json()["detail"]
        assert not _spawn

    def test_validation_rejects_before_spawn(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn
    ):
        client, _, overrides = webapp_client
        assert _dispatch(
            client, overrides, mode="add; rm -rf"
        ).status_code == 400
        assert _dispatch(client, overrides, goal="   ").status_code == 400
        assert _dispatch(
            client, overrides, repo="not-checked-out"
        ).status_code == 404
        assert not _spawn  # nothing ever spawned

    def test_legacy_host_without_output_chars_still_dispatches(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn
    ):
        """A live :8446 running pre-#302 code omits ``output_chars`` — the
        endpoint degrades to the fixed grace instead of refusing."""
        client, _, overrides = webapp_client
        overrides["session"].get_session.return_value = {"alive": True}
        resp = _dispatch(client, overrides)
        assert resp.status_code == 200
        assert len(overrides["session"].send_input.call_args_list) == 1

    def test_boot_never_quiescent_still_dispatches(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn
    ):
        """#549: dispatch now shares the chief's quiescence wait (#245) — a
        goal typed while the freshly spawned agent's boot output is still
        growing had its submitting CR swallowed, leaving the worker idle
        with the goal typed but never submitted. Ever-growing output must
        still cap out and dispatch rather than hang or drop the CR."""
        client, _, overrides = webapp_client
        counter = {"n": 0}

        def _growing(port, sid):
            counter["n"] += 1
            return {"alive": True, "output_chars": counter["n"]}

        overrides["session"].get_session.side_effect = _growing
        resp = _dispatch(client, overrides)
        assert resp.status_code == 200
        calls = overrides["session"].send_input.call_args_list
        assert len(calls) == 1
        assert calls[0].args[3] is True


class TestDispatchFailure:

    def test_timeout_kills_half_spawn(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn
    ):
        client, _, overrides = webapp_client
        overrides["session"].get_session.return_value = {
            "alive": True, "output_chars": 0,
        }
        resp = _dispatch(client, overrides)
        assert resp.status_code == 504
        assert "no output" in resp.json()["detail"]
        overrides["session"].send_input.assert_not_called()
        assert overrides["session"].stop.call_args.args == (
            8446, "disp-1", "kill"
        )

    def test_dead_session_kills_and_504s(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn
    ):
        client, _, overrides = webapp_client
        overrides["session"].get_session.return_value = {"alive": False}
        resp = _dispatch(client, overrides)
        assert resp.status_code == 504
        assert "died" in resp.json()["detail"]
        overrides["session"].send_input.assert_not_called()
        assert overrides["session"].stop.called
