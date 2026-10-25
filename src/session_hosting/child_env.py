"""The environment a launcher-spawned agent process starts with."""

from __future__ import annotations

import logging
import os
from typing import Dict

from src.env_path import effective_path

logger = logging.getLogger(__name__)

# Environment variables a *parent* coding agent injects into the short-lived,
# non-interactive subprocesses it spawns for its own tool calls. They are
# correct there and actively wrong for the long-lived interactive agent we are
# about to host:
#
#   NO_COLOR                   strips every ANSI colour out of the child's TUI,
#                              so the session renders monochrome.
#   CLAUDE_CODE_CHILD_SESSION  makes Claude Code treat itself as a nested run
#                              and disable transcript saving — no .jsonl, no
#                              --resume, no history for the whole session.
#
# The rest are the parent's own identity, stale the moment we inherit it.
#
# They reach us whenever the tray was (re)started from inside an agent's tool
# subprocess — an agent running `tray.bat --restart` is the usual route — since
# the whole tray → webapp → session-host chain inherits that environment and we
# then hand our own os.environ to everything we spawn. Scrubbing here makes a
# launcher-hosted session independent of *how* the host happened to be started.
#
# Deliberately NOT scrubbed: user-scope settings.json vars that are meant for
# every agent run (CLAUDE_AUTOCOMPACT_PCT_OVERRIDE, telemetry), and
# GIT_TERMINAL_PROMPT — restoring git's interactive credential prompt could
# hang an agent's own git call on a prompt nobody is watching.
_INHERITED_AGENT_MARKERS = frozenset(
    {
        "AI_AGENT",
        "CLAUDECODE",
        "CLAUDE_CODE_BRIDGE_SESSION_ID",
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_PID",
        "FORCE_COLOR",
        "NO_COLOR",
    }
)


def agent_child_env(session_id: str, agent: str) -> Dict[str, str]:
    """Build the environment for a launcher-spawned agent process.

    The host's own environment minus :data:`_INHERITED_AGENT_MARKERS`, plus
    this session's ``APP_LAUNCHER_SESSION_ID``/``APP_LAUNCHER_AGENT`` stamp,
    plus the **effective** ``PATH`` (issue #668).

    That last part is what lets ``cmd /c <exe>`` find a CLI installed after
    this host process started: the inherited block is frozen at spawn, so
    without it the child inherits a ``PATH`` that predates the install. It
    must be the *same* path :func:`src.agents.is_installed` resolves against
    — a button that lights up for a launch that then dies with "is not
    recognized as an internal or external command" is the worse failure.
    Only ``PATH`` is refreshed: the marker-scrubbing policy above is
    deliberate and stays exactly as it is.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in _INHERITED_AGENT_MARKERS
    }
    env["PATH"] = effective_path()
    inherited_term = env.get("TERM", "")
    # A PTY we own is xterm-compatible regardless of the shell that happened
    # to start the tray. Codex 0.153.3 refuses its interactive TUI when an
    # automation parent leaks TERM=dumb, even though ConPTY itself is fully
    # capable. Pin the child-facing terminal contract at this boundary.
    env["TERM"] = "xterm-256color"
    if inherited_term and inherited_term != env["TERM"]:
        logger.info(
            "ℹ️ PTY child TERM normalized from %s to xterm-256color for %s",
            inherited_term,
            agent,
        )
    env["APP_LAUNCHER_SESSION_ID"] = session_id
    env["APP_LAUNCHER_AGENT"] = agent
    return env
