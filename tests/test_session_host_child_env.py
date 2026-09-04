"""``agent_child_env`` — scrub the parent agent's env off a hosted session.

When the tray is (re)started from inside a coding agent's own tool
subprocess, the whole tray -> webapp -> session-host chain inherits that
agent's environment, and the session-host used to hand it verbatim to every
agent it spawned. Two of those inherited vars break the hosted session
outright: ``NO_COLOR`` renders it monochrome, and
``CLAUDE_CODE_CHILD_SESSION`` makes Claude Code disable transcript saving —
no ``.jsonl``, no ``--resume``, no history.

Observed live on 2026-07-28: session-host pid 20272 running with
``NO_COLOR=1`` + ``CLAUDE_CODE_CHILD_SESSION=1`` + a dead parent's
``CLAUDE_CODE_SESSION_ID``, every session it spawned inheriting all three.
"""

from __future__ import annotations

import os

from src.session_host import agent_child_env

# The full block captured off the polluted live session-host.
_POLLUTED = {
    "AI_AGENT": "claude-code_2-1-220_agent",
    "CLAUDECODE": "1",
    "CLAUDE_CODE_BRIDGE_SESSION_ID": "session_01YE4xgL2CkzZFRnALUFVSE3",
    "CLAUDE_CODE_CHILD_SESSION": "1",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "CLAUDE_CODE_SESSION_ID": "3a771101-3663-419c-a3d8-be781adb8c8f",
    "CLAUDE_PID": "33568",
    "NO_COLOR": "1",
}


def test_inherited_agent_markers_are_scrubbed(monkeypatch):
    """None of the parent's markers reach the spawned agent."""
    for key, value in _POLLUTED.items():
        monkeypatch.setenv(key, value)

    env = agent_child_env("abc123", "claude")

    for key in _POLLUTED:
        assert key not in env, f"{key} leaked into the child environment"


def test_color_and_transcript_markers_are_scrubbed(monkeypatch):
    """The two vars that actually break a hosted session are gone."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "0")
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")

    env = agent_child_env("abc123", "claude")

    assert "NO_COLOR" not in env
    assert "FORCE_COLOR" not in env
    assert "CLAUDE_CODE_CHILD_SESSION" not in env


def test_session_stamp_is_applied(monkeypatch):
    """The child still carries this session's own identity."""
    monkeypatch.setenv("APP_LAUNCHER_SESSION_ID", "stale-parent-session")
    monkeypatch.setenv("APP_LAUNCHER_AGENT", "codex")

    env = agent_child_env("abc123", "claude")

    assert env["APP_LAUNCHER_SESSION_ID"] == "abc123"
    assert env["APP_LAUNCHER_AGENT"] == "claude"


def test_pty_terminal_type_overrides_ambient_dumb_terminal(monkeypatch):
    """A parent coding shell's TERM=dumb must not disable a hosted TUI."""
    monkeypatch.setenv("TERM", "dumb")

    env = agent_child_env("abc123", "codex")

    assert env["TERM"] == "xterm-256color"


def test_user_scope_settings_survive(monkeypatch):
    """settings.json vars are meant for every agent run — don't scrub them."""
    monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "70")
    monkeypatch.setenv("CLAUDE_CODE_ENABLE_TELEMETRY", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("PATH", "C:\\Windows\\System32")

    env = agent_child_env("abc123", "claude")

    assert env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "70"
    assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    # PATH is the one variable deliberately *not* passed through verbatim
    # (issue #668): the child gets the effective path — inherited plus the
    # registry values — so a CLI installed after this host started still
    # resolves. The inherited entry keeps its precedence at the front.
    assert env["PATH"].split(os.pathsep)[0] == "C:\\Windows\\System32"
