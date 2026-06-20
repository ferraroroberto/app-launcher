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

    ``fullscreen`` marks a full-screen *differential* TUI (Codex's
    ratatui, and the other terminal agents) that repaints in place rather
    than scrolling inline like Claude Code. The session-host streams these
    differently: it must **not** replay the raw scrollback ring on
    (re)connect — doing so dumps stale move-cursor/clear deltas into a
    fresh xterm and re-answers the agent's startup terminal queries (the
    ``[?1;2c`` DA leak, issue #128) — and instead forces a clean repaint.

    ``resume_token`` is the agent's *native* resume invocation, spliced
    between the command and the flags for a Resume launch (issue #151) so
    the agent renders its own session picker over the PTY — the launcher
    never builds a session list of its own. It is a flag for the
    flag-shaped agents (Claude/Copilot ``--resume``) and a subcommand for
    Codex (``resume``). Antigravity has no picker flag, so it maps to
    ``--continue`` (reopen the most recent conversation — the closest
    native behaviour). Empty means the agent has no resume path.
    """

    id: str
    label: str
    command: str
    quit_command: str
    fullscreen: bool = False
    resume_token: str = ""


# id → Agent. The order here is the order the Coding tab renders the
# per-tile launch buttons in.
AGENTS: Dict[str, Agent] = {
    "claude": Agent(
        id="claude", label="Claude Code", command="claude",
        quit_command="/quit", fullscreen=False, resume_token="--resume",
    ),
    "codex": Agent(
        id="codex", label="Codex CLI", command="codex",
        quit_command="/quit", fullscreen=True, resume_token="resume",
    ),
    "antigravity": Agent(
        id="antigravity", label="Antigravity CLI", command="agy",
        quit_command="/quit", fullscreen=True, resume_token="--continue",
    ),
    "copilot": Agent(
        id="copilot", label="GitHub Copilot CLI", command="copilot",
        quit_command="/exit", fullscreen=True, resume_token="--resume",
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


def resume_command_for(agent_id: str) -> str:
    """Return the agent's native resume token (issue #151).

    Spliced between the command and the flags for a Resume launch so the
    agent shows its own session picker (Claude/Copilot ``--resume``, Codex
    ``resume`` subcommand) or — for Antigravity, which has no picker flag —
    continues the most recent conversation (``--continue``). Returns an
    empty string for an unknown id or an agent with no resume path; the
    caller treats that as "not resumable" rather than raising, so a bad id
    can never break a launch.
    """
    agent = AGENTS.get(agent_id)
    return agent.resume_token if agent else ""


def is_fullscreen(agent_id: str) -> bool:
    """Whether ``agent_id`` is a full-screen differential TUI.

    Drives the session-host's (re)connect handling: full-screen agents
    skip the raw scrollback-ring replay and get a forced repaint instead
    (issue #128). An unknown id is treated as non-fullscreen — the safe
    inline default, matching Claude Code.
    """
    agent = AGENTS.get(agent_id)
    return bool(agent and agent.fullscreen)


def is_installed(agent_id: str) -> bool:
    """Whether ``agent_id``'s command resolves on ``PATH``."""
    agent = AGENTS.get(agent_id)
    if agent is None:
        return False
    return shutil.which(agent.command) is not None


def detect_agents() -> List[Dict[str, object]]:
    """Detection snapshot for the SPA — one dict per known agent.

    Each dict is ``{"id", "label", "available", "fullscreen"}``;
    ``available`` is the live ``PATH`` check, and ``fullscreen`` lets the
    SPA tell a differential TUI (Codex/ratatui) apart from inline Claude so
    the phone terminal can pan the fixed canvas above the keyboard instead
    of reflowing — reflowing resizes the PTY and makes ratatui repaint on
    every keyboard open/close (issue #264). The Coding tab disables an
    agent's launch button (with a hover hint) when ``available`` is
    ``False``.
    """
    return [
        {
            "id": agent.id,
            "label": agent.label,
            "available": is_installed(agent.id),
            "fullscreen": agent.fullscreen,
        }
        for agent in AGENTS.values()
    ]
