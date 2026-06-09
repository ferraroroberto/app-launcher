"""src.agents — coding-agent registry + PATH detection (issue #45)."""

from __future__ import annotations

import pytest

from src import agents


class TestCommandFor:
    def test_claude_resolves(self):
        assert agents.command_for("claude") == "claude"

    def test_codex_resolves(self):
        assert agents.command_for("codex") == "codex"

    def test_antigravity_resolves(self):
        assert agents.command_for("antigravity") == "agy"

    def test_copilot_resolves(self):
        assert agents.command_for("copilot") == "copilot"

    def test_unknown_agent_raises(self):
        with pytest.raises(ValueError):
            agents.command_for("bogus")


class TestQuitCommandFor:
    def test_claude_quits_with_slash_quit(self):
        assert agents.quit_command_for("claude") == "/quit"

    def test_copilot_quits_with_slash_exit(self):
        assert agents.quit_command_for("copilot") == "/exit"

    def test_unknown_agent_falls_back_to_default(self):
        # A bad id must never block a stop — fall back, don't raise.
        assert agents.quit_command_for("bogus") == "/quit"


class TestResumeCommandFor:
    def test_claude_resumes_with_flag(self):
        # Claude's --resume opens its native interactive session picker.
        assert agents.resume_command_for("claude") == "--resume"

    def test_copilot_resumes_with_flag(self):
        assert agents.resume_command_for("copilot") == "--resume"

    def test_codex_resumes_with_subcommand(self):
        # Codex's resume is a subcommand, not a flag — it must sit right
        # after `codex` and before any flags.
        assert agents.resume_command_for("codex") == "resume"

    def test_antigravity_continues_most_recent(self):
        # agy has no picker flag; --continue reopens the most recent.
        assert agents.resume_command_for("antigravity") == "--continue"

    def test_unknown_agent_returns_empty(self):
        # A bad id is "not resumable", never a raise.
        assert agents.resume_command_for("bogus") == ""


class TestIsInstalled:
    def test_true_when_command_on_path(self, monkeypatch):
        monkeypatch.setattr(agents.shutil, "which", lambda cmd: f"C:\\bin\\{cmd}")
        assert agents.is_installed("antigravity") is True

    def test_false_when_command_missing(self, monkeypatch):
        monkeypatch.setattr(agents.shutil, "which", lambda cmd: None)
        assert agents.is_installed("claude") is False

    def test_false_for_unknown_agent(self):
        assert agents.is_installed("bogus") is False


class TestIsFullscreen:
    def test_claude_is_inline(self):
        # Claude Code scrolls inline — keeps the raw-ring replay path.
        assert agents.is_fullscreen("claude") is False

    def test_codex_antigravity_copilot_are_fullscreen(self):
        # The differential-TUI agents skip replay + get a forced repaint.
        assert agents.is_fullscreen("codex") is True
        assert agents.is_fullscreen("antigravity") is True
        assert agents.is_fullscreen("copilot") is True

    def test_unknown_agent_defaults_to_inline(self):
        # Unknown id → safe inline default (matches Claude), never raises.
        assert agents.is_fullscreen("bogus") is False


class TestDetectAgents:
    def test_shape_and_keys(self, monkeypatch):
        monkeypatch.setattr(agents.shutil, "which", lambda cmd: None)
        detected = agents.detect_agents()
        # One entry per known agent, each with the SPA-facing keys.
        assert {d["id"] for d in detected} == set(agents.AGENTS)
        for d in detected:
            assert set(d) == {"id", "label", "available"}
            assert isinstance(d["available"], bool)

    def test_availability_reflects_path(self, monkeypatch):
        # Only `claude` resolves; `agy` does not.
        monkeypatch.setattr(
            agents.shutil,
            "which",
            lambda cmd: "C:\\bin\\claude" if cmd == "claude" else None,
        )
        by_id = {d["id"]: d["available"] for d in agents.detect_agents()}
        assert by_id["claude"] is True
        assert by_id["antigravity"] is False
