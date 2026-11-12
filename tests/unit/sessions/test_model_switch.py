"""Switching a live session's model from the Chat view's model chip: the
command each agent gets, and Claude Code's "Switch model?" dialog read off the
screen. The screens are the probed ones (Claude Code 2.1.322), verbatim."""

from __future__ import annotations

import pytest

from src.session_view import model_switch as ms

# Claude Code's dialog after `/model opus` in a conversation with history.
DIALOG = [
    "❯ reply with just the word ok",
    "● ok",
    "✻ Sautéed for 2s · done 9:45 PM",
    "❯ /model opus",
    "─" * 60,
    "  Switch model?",
    "  Your next response will be slower and use more tokens",
    "  This conversation is cached for the current model. Switching to Opus 5.5 means the full history gets re-read.",
    "  ❯ 1. Yes, switch to Opus 5.5",
    "    2. No, go back",
]

# The same terminal once the switch went in: the dialog is gone.
AFTER = [
    "❯ /model opus",
    "  ⎿  Set model to Opus 5.5 and saved as your default for new sessions",
    "─" * 60,
    "❯",
]


@pytest.mark.parametrize("agent,value,command,label", [
    ("claude", "opus", "/model opus", "Opus"),
    ("claude", "sonnet", "/model sonnet", "Sonnet"),
    ("codex", "gpt-5.6-terra", "/model gpt-5.6-terra", "Terra"),
    ("codex", "gpt-6-luna", "/model gpt-6-luna", "Luna"),
])
def test_each_agent_gets_its_own_model_command_and_enter(agent, value, command, label):
    keys, want = ms.switch_keys(agent, value)
    assert keys == [(command, True)]
    assert want == label


@pytest.mark.parametrize("agent", ["pi", "copilot", "grok", "antigravity", ""])
def test_an_agent_with_no_in_session_switch_is_refused(agent):
    with pytest.raises(ms.SwitchRefused):
        ms.switch_keys(agent, "opus")


@pytest.mark.parametrize("agent,value", [
    ("claude", "gpt-5.6-terra"),   # another agent's model
    ("claude", "haiku"),           # legacy, not offered
    ("claude", "opus; rm -rf"),    # never typed
    ("codex", "opus"),
    ("claude", ""),
    ("claude", None),
    ("claude", 3),
])
def test_a_model_the_agent_does_not_offer_is_a_bad_request(agent, value):
    with pytest.raises(ValueError):
        ms.switch_keys(agent, value)


def test_the_reasons_name_the_agent():
    assert ms.no_switch_reason("pi") == "Pi can't switch model inside a session"
    assert ms.no_switch_reason("claude") is None
    assert ms.no_switch_reason("codex") is None


def test_the_dialog_is_answered_with_the_digit_of_its_yes_option():
    assert ms.confirm_keys(DIALOG, "Opus") == [("1", False)]


def test_no_dialog_means_nothing_to_answer():
    assert ms.confirm_keys(AFTER, "Opus") is None
    assert ms.confirm_keys([], "Opus") is None


def test_a_dialog_for_another_model_is_never_confirmed():
    with pytest.raises(ms.SwitchRefused):
        ms.confirm_keys(DIALOG, "Sonnet")


def test_a_dialog_showing_reads_as_showing():
    assert ms.dialog_showing(DIALOG) is True
    assert ms.dialog_showing(AFTER) is False


@pytest.mark.parametrize("content,model_id", [
    # Claude Code 2.1.322 writes the name in backticks...
    ("<local-command-stdout>Set model to `Opus 5.5` and saved as your default for new sessions"
     "</local-command-stdout>", "claude-opus-5-5"),
    # ...older builds in ANSI bold.
    ("<local-command-stdout>Set model to \x1b[1mSonnet 5.5\x1b[22m and saved as your default"
     " for new sessions</local-command-stdout>", "claude-sonnet-5-5"),
    ("<local-command-stdout>Set model to Fable 5.2</local-command-stdout>", "claude-fable-5-2"),
    ("<local-command-stdout>Kept model as Opus</local-command-stdout>", None),
    ("hello", None),
])
def test_the_transcript_line_a_switch_writes_names_the_model(content, model_id):
    assert ms.set_model_id(content) == model_id
