"""Coding-agent registry — the CLIs the Coding tab can launch.

The Coding tab launches one of two interactive terminal agents in a
project folder, both hosted by the same session-host PTY/remote
machinery:

- ``claude`` — Claude Code (the launcher's original agent).
- ``agy`` — Google's Antigravity CLI (the Go-based terminal agent that
  replaced Gemini CLI; installed via ``winget install -e --id
  Google.Antigravity``).

This module is the single source of truth for the agent id → command
mapping. It is imported by *both* long-lived processes — the webapp
(detection + launch routing) and the session-host (spawning the PTY) —
so the two never disagree on what ``agent="antigravity"`` actually runs.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class Agent:
    """One launchable coding agent.

    ``id`` is the stable key threaded through the launch API; ``label``
    is the display name; ``command`` is the executable resolved off
    ``PATH`` when the agent is spawned.
    """

    id: str
    label: str
    command: str


# id → Agent. The order here is the order the Coding tab renders the
# per-tile launch buttons in.
AGENTS: Dict[str, Agent] = {
    "claude": Agent(id="claude", label="Claude Code", command="claude"),
    "antigravity": Agent(
        id="antigravity", label="Antigravity CLI", command="agy"
    ),
}

DEFAULT_AGENT = "claude"


def command_for(agent_id: str) -> str:
    """Return the PATH command for ``agent_id``.

    Raises :class:`ValueError` for an unknown id so a bad value can
    never silently fall through to spawning the wrong process.
    """
    agent = AGENTS.get(agent_id)
    if agent is None:
        raise ValueError(f"unknown agent: {agent_id!r}")
    return agent.command


def is_installed(agent_id: str) -> bool:
    """Whether ``agent_id``'s command resolves on ``PATH``."""
    agent = AGENTS.get(agent_id)
    if agent is None:
        return False
    return shutil.which(agent.command) is not None


def detect_agents() -> List[Dict[str, object]]:
    """Detection snapshot for the SPA — one dict per known agent.

    Each dict is ``{"id", "label", "available"}``; ``available`` is the
    live ``PATH`` check. The Coding tab disables an agent's launch
    button (with a hover hint) when ``available`` is ``False``.
    """
    return [
        {"id": agent.id, "label": agent.label, "available": is_installed(agent.id)}
        for agent in AGENTS.values()
    ]
