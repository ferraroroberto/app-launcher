"""Switching a live session's model from the Chat view's model chip.

Each agent's own in-session command does the switch, typed into the PTY like
any other prompt. Every switch here applies to that session only: new sessions
keep the default the owner set. Probed live (Claude Code 2.1.323, Codex
0.164.0):

* Claude Code's ``/model <id>`` saves the pick as the default for new sessions
  (the ``model`` key of the user settings file every session shares). Its
  session-only switch is in the bare ``/model`` picker instead::

      Select model
      Switch between Claude models. Your pick becomes the default for new sessions. ...
        1.  Default (recommended)  Opus 5.5 · Best for everyday, complex tasks
      ❯ 2.  Opus 5.5 ✔             For complex work and everyday tasks
        3.  Fable 5.2              For your toughest challenges
        4.  Sonnet 5.5             Most efficient for simpler tasks
        5.  Haiku 5.5              Fastest for quick answers
        ▸   More models
      Enter to set as default · s to use this session only · Esc to cancel

  so the switch types ``/model``, reads the picker off the screen, moves its
  cursor to the picked row and presses ``s`` (:func:`picker_keys`); Enter is
  never pressed there. In a conversation with history Claude then asks,
  because the whole conversation is re-read::

      Switch model?
      Your next response will be slower and use more tokens
      This conversation is cached for the current model. Switching to Opus 5.5 means the full history gets re-read.
      ❯ 1. Yes, switch to Opus 5.5
        2. No, go back

  Once applied it writes ``Set model to `Opus 5.5` for this session only``
  into the transcript as a local-command line, which is what confirms the
  switch (:func:`set_model_id`).
* Codex never asks. ``/model <id>`` prints ``Model changed to gpt-6-luna medium for this
  session only`` and retitles the window ``<folder> | GPT-6-Luna``, the live
  title :mod:`src.session_model` already reads the model from.

Every other agent has no in-session switch this module can drive.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.launching.model_catalog import CLAUDE_SESSION_MODEL_SPECS, CODEX_MODEL_SPECS

Keystroke = Tuple[str, bool]

SWITCHABLE: Dict[str, Mapping[str, Mapping[str, Any]]] = {
    "claude": CLAUDE_SESSION_MODEL_SPECS,
    "codex": CODEX_MODEL_SPECS,
}

_AGENT_NAMES = {
    "pi": "Pi", "copilot": "Copilot", "grok": "Grok", "antigravity": "Antigravity",
    "agy": "Antigravity",
}

DIALOG_HEADING = "Switch model?"
PICKER_HEADING = "Select model"
# The picker footer's own words for its session-only key. A build without them
# offers only Enter, which saves the default, so nothing is pressed there.
SESSION_ONLY_HINT = "s to use this session only"
SESSION_ONLY_KEY = "s"
ESCAPE = "\x1b"
_DOWN, _UP = "\x1b[B", "\x1b[A"
_PICK_ROW_RE = re.compile(r"^\s*(❯)?\s*(\d+)\.\s+(.+?)(?:\s{2,}.*)?$")
_YES_RE = re.compile(r"^\s*(?:❯\s*)?(\d)\.\s+Yes, switch to (.+?)\s*$")
_SET_MODEL_RE = re.compile(
    r"<local-command-stdout>\s*Set model to\s+(.+?)"
    r"(?:\s+(?:and saved|for this session)\b.*)?\s*</local-command-stdout>",
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
    if str(agent).lower() == "claude":
        # The bare command opens the picker, whose session-only key the route
        # presses (:func:`picker_keys`); ``/model <id>`` would save the default.
        return [("/model", True)], str(spec["label"])
    return [(f"/model {value}", True)], str(spec["label"])


def _picker_rows(lines: List[str]) -> Optional[Tuple[List[Tuple[bool, str]], bool]]:
    """``(rows, session_only)`` for the last ``/model`` picker on the screen,
    or ``None`` when none is showing or its footer has not painted yet. Each
    row is ``(has_cursor, name)``, the name as shown (``Opus 5.5``)."""
    heads = [i for i, line in enumerate(lines) if line.strip() == PICKER_HEADING]
    if not heads:
        return None
    rows: List[Tuple[bool, str]] = []
    for line in lines[heads[-1] + 1:]:
        if line.strip().startswith("Enter to "):
            return rows, SESSION_ONLY_HINT in line
        match = _PICK_ROW_RE.match(line)
        if match:
            rows.append((bool(match.group(1)), match.group(3).replace("✔", "").strip()))
    return None


def picker_keys(lines: List[str], label: str) -> Optional[List[Keystroke]]:
    """The keys that switch to ``label`` for this session only in Claude
    Code's ``/model`` picker, or ``None`` when no picker is showing yet.

    The cursor moves to the row whose name starts with ``label`` and ``s`` is
    pressed. :class:`SwitchRefused` when the picker offers no session-only key
    or no row for ``label``: Enter would save the default, so it is never
    pressed, and the caller closes the picker with :data:`ESCAPE`.
    """
    parsed = _picker_rows(lines or [])
    if parsed is None:
        return None
    rows, session_only = parsed
    if not session_only:
        raise SwitchRefused(
            "This Claude Code's model picker has no session-only switch: "
            "nothing was switched and your default is unchanged"
        )
    families = [name.split()[0].lower() if name else "" for _cursor, name in rows]
    cursor = next((i for i, (has_cursor, _name) in enumerate(rows) if has_cursor), None)
    if label.lower() not in families or cursor is None:
        raise SwitchRefused(f"Claude's model picker shows no {label} row: nothing was switched")
    step = families.index(label.lower()) - cursor
    return [(_DOWN if step > 0 else _UP, False)] * abs(step) + [(SESSION_ONLY_KEY, False)]


def dialog_showing(lines: List[str]) -> bool:
    """Whether Claude Code's "Switch model?" dialog or ``/model`` picker is
    on the screen."""
    return any(line.strip() in (DIALOG_HEADING, PICKER_HEADING) for line in lines or [])


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
