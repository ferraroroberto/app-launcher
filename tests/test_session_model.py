"""The session model display name (#1383): derived from spawn flags and, for
Codex, its OSC title -- and never guessed."""

from __future__ import annotations

import pytest

from src.session_model import attach_models, session_model_label


@pytest.mark.parametrize("session,expected", [
    # Claude: only the spawn flag knows.
    ({"agent": "claude", "flags": "--permission-mode auto --model opus --effort high"}, "Opus"),
    ({"agent": "claude", "flags": "--model sonnet"}, "Sonnet"),
    ({"agent": "claude", "flags": "--model fable"}, "Fable"),
    # No flag = the agent's default model: nothing to show, not a guess.
    ({"agent": "claude", "flags": "--permission-mode auto"}, None),
    ({"agent": "claude", "flags": ""}, None),
    ({"agent": "claude"}, None),
    # An id the catalog doesn't know is not shown.
    ({"agent": "claude", "flags": "--model claude-opus-9"}, None),
    # The model that last answered (read from the transcript) comes first: it
    # covers a resume / default-model session and follows an in-session /model.
    ({"agent": "claude", "flags": "", "transcript_model": "claude-opus-5-5"}, "Opus"),
    ({"agent": "claude", "flags": "--permission-mode auto", "transcript_model": "claude-fable-5-1"}, "Fable"),
    ({"agent": "claude", "flags": "--model opus", "transcript_model": "claude-sonnet-5-5"}, "Sonnet"),
    # Not answered yet: the spawn flag, then nothing.
    ({"agent": "claude", "flags": "--model opus", "transcript_model": None}, "Opus"),
    ({"agent": "claude", "flags": "", "transcript_model": None}, None),
    # A model the catalog lacks is not shown, and the stale flag does not stand in.
    ({"agent": "claude", "flags": "--model opus", "transcript_model": "claude-haiku-4-5"}, None),
    # A family name must be a whole id segment.
    ({"agent": "claude", "flags": "", "transcript_model": "claude-opusx-5"}, None),
    # Codex and Pi ignore the Claude transcript field.
    ({"agent": "codex", "flags": "--model gpt-5.6-terra", "transcript_model": "claude-opus-5-5"}, "Terra"),
    ({"agent": "pi", "flags": "", "transcript_model": "claude-opus-5-5"}, None),
    # Agent missing from the payload defaults to Claude, like the launcher.
    ({"flags": "--model opus"}, "Opus"),
    # Codex: flag, or the live title, which wins (an in-session /model switch).
    ({"agent": "codex", "flags": "--model gpt-5.6-terra -c x=1"}, "Terra"),
    ({"agent": "codex", "flags": "--model gpt-5.6-terra", "live_title": "proj | gpt-5.6-sol"}, "Sol"),
    ({"agent": "codex", "flags": "", "live_title": "proj | gpt-6-astra high"}, "Astra"),
    ({"agent": "codex", "flags": "", "live_title": "proj"}, None),
    ({"agent": "codex", "flags": "", "live_title": "proj | something-else"}, None),
    ({"agent": "codex", "flags": "--model gpt-5.6-luna", "live_title": "proj | unknown-model"}, "Luna"),
    # Pi: the provider-qualified model arg.
    ({"agent": "pi", "flags": "--provider openai-codex --model openai-codex/gpt-5.6-sol --thinking high"}, "Sol"),
    ({"agent": "pi", "flags": "--provider claude-agent-sdk --model claude-agent-sdk/claude-opus-5"}, "Opus"),
    ({"agent": "pi", "flags": "--model nonsense/x"}, None),
    # Agents with no per-launch model.
    ({"agent": "copilot", "flags": "--model opus"}, None),
    ({"agent": "grok", "flags": ""}, None),
])
def test_session_model_label(session, expected):
    assert session_model_label(session) == expected


def test_attach_models_returns_new_dicts():
    live = [{"agent": "claude", "flags": "--model opus"}]
    out = attach_models(live)
    assert out == [{"agent": "claude", "flags": "--model opus", "model": "Opus"}]
    assert "model" not in live[0]
