"""The model a live session runs, as a short display name (#1383).

Derived on the webapp side from fields the session-host already reports --
the spawn ``flags`` and, for Codex, its OSC ``live_title`` -- never from a new
session-host field: a change to the session-host's import closure is not live
until ``:8446`` restarts, and that restart kills every live session.

Never guesses. A session launched on the agent's default model carries no
``--model`` flag and no model in its title, so it gets ``None`` (no pill), not a
plausible-looking wrong name.
"""

from __future__ import annotations

import shlex
from typing import Any, Dict, List, Mapping, Optional

from src.model_catalog import CLAUDE_MODEL_SPECS, CODEX_MODEL_SPECS, PI_MODEL_SPECS


def _flag_value(flags: str, name: str) -> Optional[str]:
    """The value following ``name`` in a flags string, or ``None``."""
    try:
        tokens = shlex.split(flags or "", posix=False)
    except ValueError:
        tokens = (flags or "").split()
    for index, token in enumerate(tokens[:-1]):
        if token == name:
            return tokens[index + 1].strip("\"'")
    return None


def _catalog_label(specs: Mapping[str, Mapping[str, Any]], value: str) -> Optional[str]:
    spec = specs.get(value.strip().lower())
    return str(spec["label"]) if spec else None


def _codex_title_label(live_title: str) -> Optional[str]:
    """Codex titles its window ``<folder> | <model>``; the model is the live
    truth (an in-session ``/model`` switch lands here, the spawn flags don't)."""
    if "|" not in (live_title or ""):
        return None
    tokens = live_title.rsplit("|", 1)[1].split()
    return _catalog_label(CODEX_MODEL_SPECS, tokens[0]) if tokens else None


def session_model_label(session: Mapping[str, Any]) -> Optional[str]:
    """Display name ("Opus", "Sol") of the session's model, or ``None``."""
    agent = str(session.get("agent") or "claude").lower()
    flags = str(session.get("flags") or "")
    value = _flag_value(flags, "--model")
    if agent == "claude":
        return _catalog_label(CLAUDE_MODEL_SPECS, value) if value else None
    if agent == "codex":
        from_title = _codex_title_label(str(session.get("live_title") or ""))
        if from_title:
            return from_title
        return _catalog_label(CODEX_MODEL_SPECS, value) if value else None
    if agent == "pi" and value:
        for spec in PI_MODEL_SPECS.values():
            if spec["model_arg"] == value:
                return str(spec["label"])
    return None


def attach_models(sessions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Each session plus a ``model`` field (a display name or ``None``).

    Returns new dicts; the inputs are never mutated.
    """
    return [{**sess, "model": session_model_label(sess)} for sess in sessions]
