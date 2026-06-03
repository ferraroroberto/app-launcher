"""Coding-agent registry — the CLIs the Coding tab can launch.

The Coding tab launches one of several interactive terminal agents in
a project folder, all hosted by the same session-host PTY/remote
machinery:

- ``claude`` — Claude Code (the launcher's original agent).
- ``codex`` — OpenAI's Codex CLI (the Rust terminal agent; runs on the
  user's ChatGPT-plan login, not API-key billing).
- ``agy`` — Google's Antigravity CLI (the Go-based terminal agent that
  replaced Gemini CLI).
- ``copilot`` — GitHub Copilot CLI (GitHub's terminal-native agentic
  coding agent; authenticates in-session via ``/login``).

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
    ``PATH`` when the agent is spawned; ``quit_command`` is the
    interactive slash command typed into the PTY for a graceful stop
    (each agent uses its own — Claude's is ``/quit``, Copilot's is
    ``/exit``).
    """

    id: str
    label: str
    command: str
    quit_command: str


# id → Agent. The order here is the order the Coding tab renders the
# per-tile launch buttons in.
AGENTS: Dict[str, Agent] = {
    "claude": Agent(
        id="claude", label="Claude Code", command="claude",
        quit_command="/quit",
    ),
    "codex": Agent(
        id="codex", label="Codex CLI", command="codex",
        quit_command="/quit",
    ),
    "antigravity": Agent(
        id="antigravity", label="Antigravity CLI", command="agy",
        quit_command="/quit",
    ),
    "copilot": Agent(
        id="copilot", label="GitHub Copilot CLI", command="copilot",
        quit_command="/exit",
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


def quit_command_for(agent_id: str) -> str:
    """Return the interactive quit command for ``agent_id``.

    Typed into the PTY for a graceful "Stop" (the terminal window stays
    open while the agent exits cleanly). Falls back to the default
    agent's command for an unknown id rather than raising — a bad id
    must never block a stop.
    """
    agent = AGENTS.get(agent_id) or AGENTS[DEFAULT_AGENT]
    return agent.quit_command


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
