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

    def test_pi_resolves(self):
        assert agents.command_for("pi") == "pi"

    def test_grok_resolves(self):
        assert agents.command_for("grok") == "grok"

    def test_ssh_resolves_for_loopback_session_host(self):
        assert agents.command_for("ssh") == "ssh"

    def test_unknown_agent_raises(self):
        with pytest.raises(ValueError):
            agents.command_for("bogus")


class TestQuitCommandFor:
    def test_claude_quits_with_slash_quit(self):
        assert agents.quit_command_for("claude") == "/quit"

    def test_copilot_quits_with_slash_exit(self):
        assert agents.quit_command_for("copilot") == "/exit"

    def test_pi_quits_with_slash_quit(self):
        assert agents.quit_command_for("pi") == "/quit"

    def test_grok_quits_with_slash_quit(self):
        assert agents.quit_command_for("grok") == "/quit"

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

    def test_pi_resumes_with_flag(self):
        # pi's -r renders its own session picker over the PTY.
        assert agents.resume_command_for("pi") == "-r"

    def test_grok_resumes_most_recent_with_flag(self):
        # grok's bare --resume reopens the cwd's most recent session
        # (Antigravity's --continue shape, not a Claude-style picker).
        assert agents.resume_command_for("grok") == "--resume"

    def test_unknown_agent_returns_empty(self):
        # A bad id is "not resumable", never a raise.
        assert agents.resume_command_for("bogus") == ""


class TestNativeSessionNameFlags:
    @pytest.mark.parametrize("agent", ["claude", "copilot", "pi"])
    def test_supported_agents_receive_spawn_time_name(self, agent: str):
        assert (
            agents.native_session_name_flags_for(agent, "Issue 556: sync picker")
            == '--name "Issue 556: sync picker"'
        )

    @pytest.mark.parametrize("agent", ["codex", "antigravity", "grok", "bogus"])
    def test_unsupported_agents_skip_native_name(self, agent: str):
        assert agents.native_session_name_flags_for(agent, "Issue 556") == ""

    def test_shell_unsafe_title_skips_native_name(self):
        assert agents.native_session_name_flags_for("claude", "name & command") == ""

    def test_native_name_matches_launcher_title_cap(self):
        expected = "x" * 60
        assert (
            agents.native_session_name_flags_for("claude", "x" * 70)
            == f'--name "{expected}"'
        )


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

    def test_pi_is_fullscreen(self):
        # Pi uses no alternate-screen buffer but is still a differential TUI —
        # it repaints its chrome in place via synchronized output, so its ring
        # is replay-unsafe and it takes Codex's forced-repaint path (#291).
        assert agents.is_fullscreen("pi") is True

    def test_grok_is_fullscreen(self):
        # Empirical (issue #626): a ConPTY probe of grok 0.2.112 showed an
        # alt-screen enter + DEC 2026 synchronized-output writes at startup.
        assert agents.is_fullscreen("grok") is True

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
            assert set(d) == {"id", "label", "available", "fullscreen"}
            assert isinstance(d["available"], bool)
            assert isinstance(d["fullscreen"], bool)
        # The SPA pans (vs reflows) the phone terminal off this flag, so the
        # fullscreen TUIs must report True and inline Claude False (#264).
        by_id = {d["id"]: d["fullscreen"] for d in detected}
        assert by_id["claude"] is False
        assert by_id["codex"] is True
        assert by_id["pi"] is True  # differential TUI, forced-repaint path (#291)
        assert "ssh" not in by_id  # service integration, not a Coding-tab button

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
