"""Per-agent command-line composition — the launch shape of every coding agent.

Split off ``src/webapp_config.py`` (issue #691, a `/codebase-audit`
maintainability finding). That module owns *configuration*: the
:class:`~src.webapp_config.WebappConfig` dataclass plus its load / save /
validate plumbing. What lives here is not config plumbing at all — it is the
business logic that turns those persisted knobs into the exact argv each
agent's CLI expects, and it is the part that changes when a new harness is
onboarded, not when a setting is added.

One builder per registered agent, plus the resume-launch variant:

* :func:`build_claude_flags`, :func:`build_antigravity_flags`,
  :func:`build_codex_flags`, :func:`build_copilot_flags`,
  :func:`build_pi_flags`, :func:`build_grok_flags` — the normal launch line.
* :func:`build_resume_flags` — the same, with the agent's native resume token
  spliced ahead of the flags its resume path actually accepts.

**Adding a seventh coding agent?** This is the module the flag-builder step of
`fleet-config`'s ``docs/adding-a-coding-harness.md`` lands in; the allowed
values it validates against (``VALID_*``, ``PI_MODEL_SPECS``) stay in
``webapp_config.py`` beside the dataclass field and its validator.

Imports one-way (``launch_flags`` → ``webapp_config``): the config module never
imports this one, so there is no cycle to work around.
"""

from __future__ import annotations

from typing import Optional

from src.agents import resume_command_for
from src.webapp_config import (
    ALWAYS_ON_CLAUDE_FLAGS,
    CODEX_MODEL_SPECS,
    DEFAULT_PI_EFFORT,
    DEFAULT_PI_MODEL,
    PI_MODEL_SPECS,
    VALID_CLAUDE_EFFORTS,
    VALID_CLAUDE_MODELS,
    VALID_CODEX_EFFORTS,
    VALID_CODEX_MODELS,
    VALID_COPILOT_MODELS,
    VALID_GROK_EFFORTS,
    VALID_PI_EFFORTS,
    VALID_PI_MODELS,
    WebappConfig,
)


def build_claude_flags(
    cfg: WebappConfig, model_override: Optional[str] = None
) -> str:
    """Compose the `claude` CLI flags from the persisted defaults.

    ``model_override`` forces a specific ``--model`` regardless of the
    persisted ``claude_model`` — used by the Life OS tab (issue #102),
    whose ``opus`` toggle picks ``opus``/``sonnet`` per launch while the
    rest of the flags (effort, permission, verbose, debug) still come
    from the shared Coding options. Other callers pass nothing and keep
    the persisted model.
    """
    parts: list[str] = list(ALWAYS_ON_CLAUDE_FLAGS)
    if cfg.claude_permission_mode == "skip":
        parts.append("--dangerously-skip-permissions")
    else:
        parts.extend(["--permission-mode", "auto"])
    model = model_override if model_override is not None else cfg.claude_model
    if model in VALID_CLAUDE_MODELS:
        parts.extend(["--model", model])
    if cfg.claude_effort in VALID_CLAUDE_EFFORTS and cfg.claude_effort != "off":
        parts.extend(["--effort", cfg.claude_effort])
    if cfg.claude_verbose:
        parts.append("--verbose")
    if cfg.claude_debug:
        parts.append("--debug")
    return " ".join(parts)


def build_antigravity_flags(cfg: WebappConfig) -> str:
    """Compose the `agy` CLI flags from the persisted Antigravity toggles.

    The Antigravity CLI has no model / effort / verbose flags, so this is
    just the two opt-in launch switches; an all-default config yields an
    empty string (the CLI is launched bare).
    """
    parts: list[str] = []
    if cfg.antigravity_skip_permissions:
        parts.append("--dangerously-skip-permissions")
    if cfg.antigravity_sandbox:
        parts.append("--sandbox")
    return " ".join(parts)


def build_codex_flags(
    cfg: WebappConfig, model_override: Optional[str] = None
) -> str:
    """Compose the `codex` CLI flags from the persisted Codex knobs.

    Three pieces: explicit model, permission mode, and a model-compatible
    reasoning tier. The reasoning value is sent bare: it isn't valid TOML, so
    Codex's `-c` parser falls back to the raw string — which also dodges
    Windows ``cmd`` quote-stripping that a quoted value would suffer.
    """
    parts: list[str] = []
    model = model_override if model_override is not None else cfg.codex_model
    if model in VALID_CODEX_MODELS:
        parts.extend(["--model", model])
    if cfg.codex_permission_mode == "skip":
        parts.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        parts.extend(
            ["--ask-for-approval", "never", "--sandbox", "workspace-write"]
        )
    effort = (
        cfg.codex_effort
        if model == cfg.codex_model
        else cfg.codex_model_efforts.get(model, "")
    )
    if effort in VALID_CODEX_EFFORTS and effort in CODEX_MODEL_SPECS[model]["efforts"]:
        parts.extend(["-c", f"model_reasoning_effort={effort}"])
    return " ".join(parts)


def build_copilot_flags(cfg: WebappConfig) -> str:
    """Compose the `copilot` CLI flags from the persisted Copilot toggle.

    The Copilot CLI chooses its model in-session, so the only
    launch-relevant switch is ``--allow-all`` (enable every tool
    permission without prompting). An all-default config yields an empty
    string — the CLI is launched bare.
    """
    parts: list[str] = []
    if cfg.copilot_skip_permissions:
        parts.append("--allow-all")
    if cfg.copilot_model in VALID_COPILOT_MODELS:
        parts.extend(["--model", cfg.copilot_model])
    return " ".join(parts)


def build_pi_flags(cfg: WebappConfig) -> str:
    """Compose the `pi` CLI flags from the persisted Pi knobs (issues #273, #288).

    Three pieces, all passed explicitly because pi's settings.json defaults
    do not reliably reroute a launch:

    - **provider + model** — looked up from
      :data:`src.webapp_config.PI_MODEL_SPECS` for the chosen option.
      Opus/Sonnet route to the ``claude-agent-sdk`` provider (the Claude
      **subscription** quota, no API "extra usage" credits); GPT routes to
      ``openai-codex`` (the ChatGPT-plan subscription). ``pi_model`` is never
      empty — an unknown value falls back to ``DEFAULT_PI_MODEL`` so the launch
      can't slip onto a billing path (pi's native ``anthropic`` provider is
      deliberately bypassed, and disconnected on this machine).
    - **thinking** — ``--thinking <effort>`` from ``pi_effort`` (default high).
    - **trust** — ``--approve`` (trust mode) or ``--no-approve`` (ask mode).
      This is project *trust* (loading project-local ``.pi/`` resources), NOT
      a tool-permission gate: pi has no tool sandbox or per-action prompt.

    In-session switching stays available via ``/model`` / ``Ctrl+L`` /
    ``Shift+Tab``. See docs/pi-coding-agent.md.
    """
    model = cfg.pi_model if cfg.pi_model in VALID_PI_MODELS else DEFAULT_PI_MODEL
    spec = PI_MODEL_SPECS[model]
    provider, model_arg = spec["provider"], spec["model_arg"]
    parts = ["--provider", provider, "--model", model_arg]
    effort = cfg.pi_effort if cfg.pi_effort in VALID_PI_EFFORTS else DEFAULT_PI_EFFORT
    parts.extend(["--thinking", effort])
    parts.append("--approve" if cfg.pi_trust_mode == "trust" else "--no-approve")
    return " ".join(parts)


def build_grok_flags(cfg: WebappConfig) -> str:
    """Compose the `grok` CLI flags from the persisted Grok knobs (#626, #667).

    Two pieces, both verified against the running binary rather than the
    docs (0.2.114) — #626's premise was that Grok's properties get probed,
    not assumed:

    - **permission mode** — ``--permission-mode auto`` (no prompts, guard
      rails intact) or ``--permission-mode bypassPermissions`` for "skip".
      Grok's own flag takes six values; the launcher presents the same
      two-state auto/skip shape as Claude and Codex.
    - **reasoning tier** — ``--reasoning-effort <low|medium|high>``, the
      exact set the CLI's own rejection message names.

    No ``--model``: ``grok models`` lists only ``grok-4.5`` today, and a
    one-option picker is dead UI (the same call the launcher already makes
    for Antigravity). Add it when xAI ships a second model.
    """
    parts = [
        "--permission-mode",
        "bypassPermissions" if cfg.grok_permission_mode == "skip" else "auto",
    ]
    if cfg.grok_effort in VALID_GROK_EFFORTS:
        parts.extend(["--reasoning-effort", cfg.grok_effort])
    return " ".join(parts)


def build_resume_flags(
    cfg: WebappConfig, agent_id: str, model_override: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Compose the full flags string for a *Resume* launch (issue #151).

    Splices the agent's native resume token (see
    :func:`src.agents.resume_command_for`) ahead of the flags its resume
    path actually accepts, so the launch line becomes
    ``<command> <resume-token> <flags>`` and the agent renders its own
    session picker over the PTY.

    Most agents accept their normal launch flags after the resume token,
    so this is ``<token> <normal builder output>``. The one exception is
    **Codex**: its ``resume`` subcommand rejects the top-level
    ``--ask-for-approval`` / ``--sandbox`` switches, accepting only the
    config override — so a Codex resume carries just
    ``resume -c model_reasoning_effort=<effort>``.

    ``model_override`` forces a specific Claude or Codex ``--model``. Life OS
    uses only the Claude path; Coding and Board can override either provider.

    ``session_id``, when given, pins the resume token to that specific
    conversation (``<token> <session_id> <flags>``) so the agent reattaches
    directly instead of rendering its interactive picker (issue #633, the
    fleet chief's Resume control — verified empirically that Claude's
    ``--resume <id>`` accepts a session id directly, non-interactively).
    Omitted (the default, and every caller before #633), the bare token
    renders the picker exactly as before.
    """
    token = resume_command_for(agent_id)
    if session_id:
        token = f"{token} {session_id}"
    if agent_id == "codex":
        parts = [token]
        model = model_override if model_override is not None else cfg.codex_model
        if model in VALID_CODEX_MODELS:
            parts.extend(["--model", model])
        effort = (
            cfg.codex_effort
            if model == cfg.codex_model
            else cfg.codex_model_efforts.get(model, "")
        )
        if model in VALID_CODEX_MODELS and effort in CODEX_MODEL_SPECS[model]["efforts"]:
            parts.extend(["-c", f"model_reasoning_effort={effort}"])
        return " ".join(parts).strip()
    if agent_id == "claude":
        base = build_claude_flags(cfg, model_override=model_override)
    elif agent_id == "antigravity":
        base = build_antigravity_flags(cfg)
    elif agent_id == "copilot":
        base = build_copilot_flags(cfg)
    elif agent_id == "pi":  # keep the SDK provider/model on resume (issue #273)
        base = build_pi_flags(cfg)
    elif agent_id == "grok":  # no launch knobs — bare `--resume` (issue #626)
        base = build_grok_flags(cfg)
    else:
        base = ""
    return f"{token} {base}".strip()
