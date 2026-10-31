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

Imports one-way (``src.launching.flags`` → ``webapp_config``): the config module never
imports this one, so there is no cycle to work around.
"""

from __future__ import annotations

from typing import Optional

from src.agents import resume_command_for
from src.launching.model_catalog import (
    CLAUDE_MODEL_SPECS,
    CODEX_MODEL_SPECS,
    LEGACY_CLAUDE_DEFAULT_EFFORTS,
    PI_MODEL_SPECS,
)
from src.webapp_config import (
    ALWAYS_ON_CLAUDE_FLAGS,
    DEFAULT_CLAUDE_EFFORT,
    DEFAULT_PI_MODEL,
    VALID_CLAUDE_MODELS,
    VALID_CODEX_MODELS,
    VALID_GROK_EFFORTS,
    VALID_PI_EFFORTS,
    VALID_PI_MODELS,
    WebappConfig,
    valid_claude_efforts_for,
)


# The Claude Code Telegram channel plugin. ``--channels`` makes Claude
# start that plugin's server and inject the chat's messages into the session.
CHANNEL_PLUGIN = "plugin:telegram@claude-plugins-official"


def default_effort(agent: str, model: str) -> str:
    """The catalog ``default_effort`` of ``model``, else ``high`` (#1711)."""
    if agent == "claude":
        spec = CLAUDE_MODEL_SPECS.get(model, {})
        default = spec.get("default_effort") or LEGACY_CLAUDE_DEFAULT_EFFORTS.get(model)
    elif agent == "codex":
        default = CODEX_MODEL_SPECS.get(model, {}).get("default_effort")
    elif agent == "pi":
        default = PI_MODEL_SPECS.get(model, {}).get("default_effort")
    else:
        raise ValueError(f"no per-model effort for agent {agent!r}")
    return default or DEFAULT_CLAUDE_EFFORT


def effective_effort(agent: str, model: str, cfg: WebappConfig) -> str:
    """The effort ``model`` launches at (#1711): the owner's override for that
    model, else the catalog ``default_effort``, else ``high``.

    The one copy of the precedence rule; every builder asks it for the model it
    is actually launching, never the selected one. An override the model does
    not support is ignored rather than trusted, and an unknown model resolves
    to ``high``. For Claude the value may be ``off`` (an override meaning "send
    no ``--effort``"); the builder, not this function, acts on it. For Pi the
    value is a ``--thinking`` level (``off`` included) sent as is (#1713).
    """
    if agent == "claude":
        overrides, allowed = cfg.claude_model_efforts, valid_claude_efforts_for(model)
    elif agent == "codex":
        overrides = cfg.codex_model_efforts
        allowed = CODEX_MODEL_SPECS.get(model, {}).get("efforts", ())
    elif agent == "pi":
        overrides, allowed = cfg.pi_model_efforts, VALID_PI_EFFORTS
    else:
        raise ValueError(f"no per-model effort for agent {agent!r}")
    override = overrides.get(model)
    return override if override in allowed else default_effort(agent, model)


def claude_model_family(recorded: str) -> str:
    """The Claude model a recorded id belongs to (``claude-opus-5-5`` → ``opus``),
    or ``""`` when it names none of ours."""
    lowered = (recorded or "").lower()
    return next((m for m in VALID_CLAUDE_MODELS if m in lowered), "")


def build_claude_flags(
    cfg: WebappConfig,
    model_override: Optional[str] = None,
    *,
    channel_settings: Optional[str] = None,
    effort_model: Optional[str] = None,
    effort_override: Optional[str] = None,
) -> str:
    """Compose the `claude` CLI flags from the persisted defaults.

    ``model_override`` forces a specific ``--model`` regardless of the
    persisted ``claude_model`` — used by the Life OS tab (issue #102),
    whose ``opus`` toggle picks ``opus``/``sonnet`` per launch while the
    rest of the flags (verbose, debug, permission) still come from the
    shared Coding options. Other callers pass nothing and keep the persisted
    model.

    ``--effort`` is that model's own (:func:`effective_effort`, #1711): the
    model actually launched, never the selected one. When no valid model is
    sent (a keep-model resume leaves the model to the conversation), the
    effort is resolved for ``effort_model`` — the conversation's recorded
    model id — and omitted when that names none of ours, which leaves the
    model at its own default.

    ``effort_override`` replaces that resolved effort for one launch (a Board
    dispatch's ``effort``); the caller has already checked the model offers
    it. ``None`` keeps the persisted per-model effort.

    ``channel_settings`` — the path of a per-profile settings file — turns the
    session into a Telegram channel session (#1366): ``--channels`` starts the
    plugin and ``--settings <file>`` hands it its ``TELEGRAM_STATE_DIR``
    through Claude's own settings ``env`` block, so the variable reaches that
    session's plugin server and no other. A channel session never runs in
    bypass mode, whatever the shared Coding option says (#1338 constraint).
    ``None`` (every other caller) leaves the output exactly as before.
    """
    parts: list[str] = list(ALWAYS_ON_CLAUDE_FLAGS)
    if cfg.claude_permission_mode == "skip" and not channel_settings:
        parts.append("--dangerously-skip-permissions")
    else:
        parts.extend(["--permission-mode", "auto"])
    model = model_override if model_override is not None else cfg.claude_model
    if model in VALID_CLAUDE_MODELS:
        parts.extend(["--model", model])
    effort_target = model if model in VALID_CLAUDE_MODELS else claude_model_family(effort_model or "")
    if effort_target:
        effort = effort_override or effective_effort("claude", effort_target, cfg)
        if effort != "off":
            parts.extend(["--effort", effort])
    if cfg.claude_verbose:
        parts.append("--verbose")
    if cfg.claude_debug:
        parts.append("--debug")
    if channel_settings:
        parts.extend(["--channels", CHANNEL_PLUGIN, "--settings", channel_settings])
    return " ".join(parts)


def build_antigravity_flags(cfg: WebappConfig) -> str:
    """Compose the `agy` CLI flags from the persisted Antigravity toggles.

    The Antigravity CLI (`agy`) does have `--model` and `--effort`, but its
    model is chosen in-session with `/model`, so the launcher has no model to
    key an effort on and sends neither (#1711, skipped by decision). It has no
    verbose flag. So this is just the two opt-in launch switches; an
    all-default config yields an empty string (the CLI is launched bare).
    """
    parts: list[str] = []
    if cfg.antigravity_skip_permissions:
        parts.append("--dangerously-skip-permissions")
    if cfg.antigravity_sandbox:
        parts.append("--sandbox")
    return " ".join(parts)


def build_codex_flags(
    cfg: WebappConfig, model_override: Optional[str] = None,
    *, effort_override: Optional[str] = None,
) -> str:
    """Compose the `codex` CLI flags from the persisted Codex knobs.

    Three pieces: explicit model, permission mode, and a model-compatible
    reasoning tier (``effort_override`` for one launch, already checked
    against the model, else the persisted per-model effort). The reasoning value is sent bare: it isn't valid TOML, so
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
    if model in VALID_CODEX_MODELS:
        effort = effort_override or effective_effort("codex", model, cfg)
        parts.extend(["-c", f"model_reasoning_effort={effort}"])
    return " ".join(parts)


def build_copilot_flags(cfg: WebappConfig) -> str:
    """Compose the `copilot` CLI flags from the persisted Copilot toggle.

    The Copilot CLI chooses its model **in-session**, so the only
    launch-relevant switch is ``--allow-all`` (enable every tool
    permission without prompting). An all-default config yields an empty
    string — the CLI is launched bare.

    **No ``--model`` is sent, deliberately (issue #1017).** The launcher
    used to append one from a hand-maintained catalogue, and the phone
    then displayed a model the session was not running: Copilot resolves
    the flag against the account's *entitlement* at launch and silently
    falls back, printing one line that scrolls away — ``✗ Model "<id>"
    from --model flag is not available. Using "auto" instead.``
    Measured through the launcher's own launch route on Copilot 1.0.86,
    five ids drawn from the CLI's own ``copilot help config`` list were
    refused five times out of five (``gpt-5.6-luna``, ``gpt-5.6-sol``,
    ``claude-sonnet-5``, ``claude-haiku-4.5``, ``gpt-5-mini``); the bare
    launch was the only clean one — and the refusal is not about any
    particular id: Copilot's own ``/model`` picker warns that *"the
    --model argument will be overridden"*, names the plan as the reason,
    and at the time of measuring listed every catalogue entry under its
    **Unavailable models** heading.

    So the flag's success is decided per launch by the account's plan,
    which no list in this repo can predict and which changes without
    anything here changing; asserting a model is the launcher claiming a
    setting it never achieved. Copilot already owns this
    choice properly: ``/model`` for the session and ``/config model``
    for the default, both of which show live what the plan actually
    allows. The launcher stays out of it, exactly as it does for the
    Antigravity CLI.
    """
    parts: list[str] = []
    if cfg.copilot_skip_permissions:
        parts.append("--allow-all")
    return " ".join(parts)


def build_pi_flags(cfg: WebappConfig) -> str:
    """Compose the `pi` CLI flags from the persisted Pi knobs (issues #273, #288).

    Three pieces, all passed explicitly because pi's settings.json defaults
    do not reliably reroute a launch:

    - **provider + model** — looked up from
      :data:`src.launching.model_catalog.PI_MODEL_SPECS` for the chosen option.
      Opus/Sonnet route to the ``claude-agent-sdk`` provider (the Claude
      **subscription** quota, no API "extra usage" credits); GPT routes to
      ``openai-codex`` (the ChatGPT-plan subscription). ``pi_model`` is never
      empty — an unknown value falls back to ``DEFAULT_PI_MODEL`` so the launch
      can't slip onto a billing path (pi's native ``anthropic`` provider is
      deliberately bypassed, and disconnected on this machine).
    - **thinking** — ``--thinking <effort>``: the chosen model's own
      (:func:`effective_effort`, #1713), its override else ``high``.
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
    parts.extend(["--thinking", effective_effort("pi", model, cfg)])
    parts.append("--approve" if cfg.pi_trust_mode == "trust" else "--no-approve")
    return " ".join(parts)


def build_grok_flags(cfg: WebappConfig, *, effort_override: Optional[str] = None) -> str:
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

    ``effort_override`` (one of ``VALID_GROK_EFFORTS``) replaces the
    persisted tier for one launch.
    """
    parts = [
        "--permission-mode",
        "bypassPermissions" if cfg.grok_permission_mode == "skip" else "auto",
    ]
    effort = effort_override or cfg.grok_effort
    if effort in VALID_GROK_EFFORTS:
        parts.extend(["--reasoning-effort", effort])
    return " ".join(parts)


def build_resume_flags(
    cfg: WebappConfig, agent_id: str, model_override: Optional[str] = None,
    session_id: Optional[str] = None, *, keep_model: bool = False,
    effort_model: Optional[str] = None,
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

    ``keep_model`` (#1503, Life OS's swipe-Resume) resumes a Claude
    conversation on its own model: no ``--model`` at all, because a bare
    ``--resume <id>`` keeps the conversation's model and ``--model`` would
    override it (probed, see :mod:`src.session_view.conversation_model`). Codex has no such
    memory, so a Codex caller passes the recorded model as ``model_override``
    and ``keep_model`` changes nothing there. An absent model is never read as
    "keep": that means the persisted default, as it always has.

    ``effort_model`` (Claude, with ``keep_model``) is the conversation's
    recorded model id: no ``--model`` is sent, so the effort is resolved for
    that model's family, and omitted when it is unknown (#1711).
    """
    token = resume_command_for(agent_id)
    if session_id:
        token = f"{token} {session_id}"
    if keep_model and agent_id == "claude":
        model_override = ""  # not a valid model, so build_claude_flags adds no --model
    if agent_id == "codex":
        parts = [token]
        model = model_override if model_override is not None else cfg.codex_model
        if model in VALID_CODEX_MODELS:
            parts.extend(["--model", model])
        if model in VALID_CODEX_MODELS:
            effort = effective_effort("codex", model, cfg)
            parts.extend(["-c", f"model_reasoning_effort={effort}"])
        return " ".join(parts).strip()
    if agent_id == "claude":
        base = build_claude_flags(
            cfg, model_override=model_override, effort_model=effort_model
        )
    elif agent_id == "antigravity":
        base = build_antigravity_flags(cfg)
    elif agent_id == "copilot":
        base = build_copilot_flags(cfg)
    elif agent_id == "pi":  # keep the SDK provider/model on resume
        base = build_pi_flags(cfg)
    elif agent_id == "grok":  # no launch knobs — bare `--resume`
        base = build_grok_flags(cfg)
    else:
        base = ""
    return f"{token} {base}".strip()
