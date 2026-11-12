"""Switching a live session's model from the Chat view's model chip.

Each agent's own in-session command does the switch, typed into the PTY like
any other prompt: ``/model <id>`` for both Claude Code and Codex. Probed live
(Claude Code 2.1.322, Codex 0.164.0):

* Claude Code applies it at once in a conversation with no history. With
  history it first asks, because the whole conversation is re-read::

      Switch model?
      Your next response will be slower and use more tokens
      This conversation is cached for the current model. Switching to Opus 5.5 means the full history gets re-read.
      ❯ 1. Yes, switch to Opus 5.5
        2. No, go back

  Once applied it writes ``Set model to `Opus 5.5` and saved as your default
  for new sessions`` into the transcript as a local-command line, which is
  what confirms the switch (:func:`set_model_id`). It also saves the model as
  the user's default for new sessions: that is Claude Code's behaviour, not
  something this module can opt out of.
* Codex never asks. It prints ``Model changed to gpt-6-luna medium for this
  session only`` and retitles the window ``<folder> | GPT-6-Luna``, the live
  title :mod:`src.session_model` already reads the model from.

Every other agent has no in-session switch this module can drive.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.launching.model_catalog import CLAUDE_MODEL_SPECS, CODEX_MODEL_SPECS

Keystroke = Tuple[str, bool]

SWITCHABLE: Dict[str, Mapping[str, Mapping[str, Any]]] = {
    "claude": CLAUDE_MODEL_SPECS,
    "codex": CODEX_MODEL_SPECS,
}

_AGENT_NAMES = {
    "pi": "Pi", "copilot": "Copilot", "grok": "Grok", "antigravity": "Antigravity",
    "agy": "Antigravity",
}

DIALOG_HEADING = "Switch model?"
_YES_RE = re.compile(r"^\s*(?:❯\s*)?(\d)\.\s+Yes, switch to (.+?)\s*$")
_SET_MODEL_RE = re.compile(
    r"<local-command-stdout>\s*Set model to\s+(.+?)(?:\s+and saved\b.*)?\s*</local-command-stdout>",
    re.DOTALL,
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class SwitchRefused(Exception):
    """The switch cannot be driven here; the message is worded for the reader."""


def no_switch_reason(agent: Any) -> Optional[str]:
    """Why ``agent`` has no in-session switch, or ``None`` when it has one."""
    key = str(agent or "").lower()
    if key in SWITCHABLE:
        return None
    name = _AGENT_NAMES.get(key, key.capitalize() or "This agent")
    return f"{name} can't switch model inside a session"


def switch_keys(agent: Any, value: Any) -> Tuple[List[Keystroke], str]:
    """``(keys, label)``: the keystrokes that switch ``agent`` to ``value``
    and the display name the switch is confirmed against.

    :class:`SwitchRefused` for an agent with no in-session switch;
    :class:`ValueError` for a model the agent's catalog does not offer, so
    nothing outside the catalog is ever typed into a terminal.
    """
    reason = no_switch_reason(agent)
    if reason:
        raise SwitchRefused(reason)
    specs = SWITCHABLE[str(agent).lower()]
    spec = specs.get(value) if isinstance(value, str) else None
    if not spec or not spec.get("available"):
        raise ValueError(f"model must be one this agent offers: {', '.join(sorted(specs))}")
    return [(f"/model {value}", True)], str(spec["label"])


def dialog_showing(lines: List[str]) -> bool:
    """Whether Claude Code's "Switch model?" dialog is on the screen."""
    return any(line.strip() == DIALOG_HEADING for line in lines or [])


def confirm_keys(lines: List[str], label: str) -> Optional[List[Keystroke]]:
    """The keys that confirm the dialog, or ``None`` when none is showing.

    Only the dialog's own "Yes, switch to <name>" option is pressed, and only
    while ``<name>`` is the model asked for: a dialog for any other model is
    :class:`SwitchRefused`, and nothing is pressed.
    """
    lines = lines or []
    heads = [i for i, line in enumerate(lines) if line.strip() == DIALOG_HEADING]
    if not heads:
        return None
    for line in lines[heads[-1] + 1:]:
        match = _YES_RE.match(line)
        if not match:
            continue
        shown = match.group(2).strip()
        if shown.split()[0].lower() != label.lower():
            raise SwitchRefused(
                f"The terminal asks to switch to {shown}, not {label}: answer it on the terminal"
            )
        return [(match.group(1), False)]
    return None


def set_model_id(content: Any) -> Optional[str]:
    """A Claude model id for the ``Set model to <name>`` line a switch writes
    into the transcript, or ``None`` when ``content`` is not one.

    The line names the model, not its id (``Opus 5.5``), so the id is built
    from the name (``claude-opus-5-5``), which
    :func:`src.session_model.session_model_label` reads by its family segment.
    """
    if not isinstance(content, str) or "Set model to" not in content:
        return None
    match = _SET_MODEL_RE.search(_ANSI_RE.sub("", content))
    if not match:
        return None
    name = match.group(1).strip().strip("`").strip()
    if not name:
        return None
    return "claude-" + "-".join(name.lower().replace(".", "-").split())
