"""The defaults and value sets each :class:`src.webapp_config.WebappConfig` field
validates against: network ports, credential keys, display bounds and every
coding agent's launch choices.

:mod:`src.webapp_config` re-exports every name here, so callers keep reading
them from there.
"""

from __future__ import annotations

from typing import Tuple

from src.launching.model_catalog import CLAUDE_MODEL_SPECS, CODEX_MODEL_SPECS, PI_MODEL_SPECS, available_values

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8445
# Loopback port the PTY session-host binds. Never network-reachable.
DEFAULT_SESSION_HOST_PORT = 8446
# Env override for the session-host port. Set ONLY by the e2e pre-ship gate's
# autoboot so a disposable webapp can be pointed at a disposable, free-port
# session-host instead of the live :8446 a running tray owns. This is what
# stops the gate from reaching into — and killing — the user's real PTY
# sessions. Not a user-facing knob; intentionally undocumented
# in the config sample.
SESSION_HOST_PORT_ENV = "LAUNCHER_SESSION_HOST_PORT"
# Env override for the config file *path* itself. Set ONLY by the e2e
# pre-ship gate's autoboot (tests/e2e/fixtures/autoboot.py) so the disposable webapp
# reads AND writes its own temp config (derived from the real one, minus every
# credential) instead of the real, shared
# config/webapp_config.json — a Settings-tab e2e test that clicks Save must
# never mutate the user's real file (a port corruption
# was this exact shared-file design biting). Not a user-facing knob;
# intentionally undocumented in the config sample.
WEBAPP_CONFIG_PATH_ENV = "LAUNCHER_WEBAPP_CONFIG"
# Every on-disk key that holds a credential. Anything that derives
# a config for a test or disposable run from the real file must drop these —
# the e2e autoboot does (tests/_credential_hygiene.py), and its failure-output
# redaction scrubs their values. ``webhook_secrets`` is the legacy name
# ``secrets`` still loads from (see ``_load_secrets``), so it is a credential
# too. A new credential field must be added here: tests/unit/repo/test_credential_hygiene.py
# fails on any credential-shaped field name this tuple doesn't list.
CREDENTIAL_KEYS: Tuple[str, ...] = (
    "auth_token",
    "auth_password",
    "pushover_api_token",
    "pushover_user_key",
    "telegram_bot_token",
    "secrets",
    "webhook_secrets",
    "api_tokens",
    "delegate_token",
)

# Bounded scrollback for full-screen (ratatui) agent sessions —
# how many lines of history the session-host retains and
# replays on a (re)connect. 10,000 is evidence-based, not a guess: a real,
# tool-heavy Codex exchange was observed producing 3371-4447 total scrolled
# lines and rendering to only ~216-283 KB even at that size (see
# src/vt_snapshot.py's _HISTORY_LINES docstring for the full history). The
# bounds keep a user-set value sane: too low reintroduces "can't see the
# start of a real conversation"; too high risks a slow reconnect paint on
# a weak mobile connection.
DEFAULT_TERMINAL_HISTORY_LINES = 10_000
MIN_TERMINAL_HISTORY_LINES = 200
MAX_TERMINAL_HISTORY_LINES = 50_000

# The compose bar's large-file attach: the most it will take,
# in MB. Separate from the ordinary attach's fixed 12 MB, because a large file
# is stored for the agent to use by path and never read into its context.
# 2 GB covers an archive of scanned documents with room to spare; the ceiling
# keeps a typo from turning into an unbounded write to the project drive.
DEFAULT_LARGE_UPLOAD_MAX_MB = 2048
MIN_LARGE_UPLOAD_MAX_MB = 1
MAX_LARGE_UPLOAD_MAX_MB = 10240

# --- Fleet chief ---------------------------------------
# The standing conversational orchestrator the Board's chat mode talks to.
# `chief_model` must be a dispatchable Claude tier; the worker cap is read
# by the /chief skill over loopback as its dispatch rail — it caps how many
# worker sessions the chief may have running at once. The
# daily-respawn setting is retired: fleet-config shipped compact-and-continue
# (chief hands its own handover log back to itself on every session start),
# so an unattended daily respawn would now discard a live batch's context
# rather than protect it. A restart is still available, but only as an
# explicit operator action (the Board's chief Restart button) — never
# a schedule that fires unattended.
VALID_CHIEF_MODELS = available_values(CLAUDE_MODEL_SPECS)
DEFAULT_CHIEF_MODEL = "fable"
DEFAULT_CHIEF_WORKER_CAP = 3
MIN_CHIEF_WORKER_CAP = 1
MAX_CHIEF_WORKER_CAP = 10
# The context-use percent at which the chief compacts itself (read by
# fleet-config's `chief_ops.py self-compact` from
# GET /api/board/chief-settings). 0 turns it off; otherwise it must sit in
# the range. The app only stores the number and never compacts anything.
DEFAULT_CHIEF_AUTO_COMPACT_THRESHOLD = 30
MIN_CHIEF_AUTO_COMPACT_THRESHOLD = 10
MAX_CHIEF_AUTO_COMPACT_THRESHOLD = 90

# Derived from the catalog like the Codex/Pi/chief sets, so a model
# refresh makes a tier pickable, savable and launchable in one edit. Haiku
# remains accepted for existing configs and direct launches, but it is not a
# catalog tier, so the curated phone selectors never offer it.
LEGACY_CLAUDE_MODELS = ("haiku",)
VALID_CLAUDE_MODELS = available_values(CLAUDE_MODEL_SPECS) + LEGACY_CLAUDE_MODELS
VALID_CLAUDE_EFFORTS = ("off", "low", "medium", "high", "xhigh", "max")
DEFAULT_CLAUDE_MODEL = "opus"
# Telegram channel sessions answer short household requests: Sonnet by default,
# not the global Claude model.
DEFAULT_CHANNEL_MODEL = "sonnet"
# The effort of last resort: what a model with no override and no
# catalog default resolves to. ``src.launching.flags.effective_effort`` owns the rule.
DEFAULT_CLAUDE_EFFORT = "high"


def valid_claude_efforts_for(model: str) -> Tuple[str, ...]:
    """Efforts a Claude override may hold for ``model``: its catalog list, plus
    ``off`` ("send no ``--effort``"). A legacy model (haiku) has no catalog row,
    so it takes the whole enum."""
    spec = CLAUDE_MODEL_SPECS.get(model)
    if spec is None:
        return VALID_CLAUDE_EFFORTS if model in LEGACY_CLAUDE_MODELS else ()
    return ("off",) + tuple(spec["efforts"])

# Claude Code permission mode for the launch command. "auto" maps to
# `--permission-mode auto` (no prompts, but a classifier blocks dangerous
# actions — the safer autopilot); "skip" maps to the legacy
# `--dangerously-skip-permissions` (no prompts, no safety net).
VALID_CLAUDE_PERMISSION_MODES = ("auto", "skip")
DEFAULT_CLAUDE_PERMISSION_MODE = "auto"

# Codex model/effort pairs. The CLI's per-model effort support is
# encoded in the shared catalog; Luna intentionally defaults to Extra High.
VALID_CODEX_MODELS = available_values(CODEX_MODEL_SPECS)
VALID_CODEX_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
DEFAULT_CODEX_MODEL = "gpt-6-luna"
DEFAULT_CODEX_MODEL_EFFORTS = {
    value: spec["default_effort"]
    for value, spec in CODEX_MODEL_SPECS.items()
    if spec.get("available")
}
DEFAULT_CODEX_EFFORT = DEFAULT_CODEX_MODEL_EFFORTS[DEFAULT_CODEX_MODEL]
VALID_CODING_MODEL_CHOICES = tuple(
    [f"claude:{model}" for model in available_values(CLAUDE_MODEL_SPECS)]
    + [f"codex:{model}" for model in VALID_CODEX_MODELS]
)
DEFAULT_CODING_MODEL_CHOICE = f"claude:{DEFAULT_CLAUDE_MODEL}"

# Permission mode for the `codex` launch, mirroring Claude's auto/skip.
# "auto" → `--ask-for-approval never --sandbox workspace-write` (no
# prompts, but still sandboxed — the safe autopilot); "skip" → the
# legacy `--dangerously-bypass-approvals-and-sandbox` (no prompts, no
# sandbox).
VALID_CODEX_PERMISSION_MODES = ("auto", "skip")
DEFAULT_CODEX_PERMISSION_MODE = "auto"

# GitHub Copilot deliberately has no model catalogue here.
# The launcher used to mirror `copilot help config`'s list and send the
# chosen id as `--model`, but that list says what the *CLI* accepts, not
# what the *account* is entitled to — Copilot resolves entitlement per
# launch and silently falls back to "auto" when it refuses, so any id the
# launcher displayed was an unverifiable claim. `build_copilot_flags`
# sends no model flag; `/model` and `/config model` own the choice and
# show live what the plan allows.

# Pi coding-agent launch models. The Coding tab shows a
# deliberately small, segmented model control — three options spanning two
# subscription providers (the one cross-provider wrinkle in the launcher):
#
#   - Opus / Sonnet → the `claude-agent-sdk` provider, driven by the Claude
#     **subscription** via the claude-agent-sdk-pi extension (the Claude Agent
#     SDK / Claude Code path). NOT pi's native `anthropic` provider, which
#     bills metered API "extra usage" credits (that OAuth is disconnected on
#     this machine — only the SDK + openai-codex paths remain).
#   - GPT → pi's `openai-codex` provider, the ChatGPT-plan **subscription**
#     (its OAuth token lives in pi's auth.json). Verified no-API-credit:
#     `pi -p --provider openai-codex --model openai-codex/gpt-5.6-sol` completes
#     cleanly with no key set.
#
# Each option maps to (provider, full `provider/id` model arg, display label);
# `build_pi_flags` switches `--provider`/`--model` on the chosen option.
# `pi_model` is never empty — an unknown value falls back to DEFAULT_PI_MODEL
# so the launch can never slip onto a billing path. Refresh the model ids from
# `pi --list-models claude-agent-sdk` / `pi --list-models openai-codex`.
VALID_PI_MODELS = available_values(PI_MODEL_SPECS)
DEFAULT_PI_MODEL = "claude-opus-5"

# Pi reasoning effort, mapped to pi's full `--thinking <level>` ladder.
VALID_PI_EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
DEFAULT_PI_EFFORT = "high"

# Pi project-trust mode, mapped to pi's `--approve`/`--no-approve` flag.
# NOTE: this is NOT a tool-execution permission gate like
# Claude's/Codex's auto/skip — pi has no tool sandbox or per-action prompt
# (see pi's security.md). It governs project *trust*: whether pi loads
# project-local `.pi/` settings/extensions/skills. "trust" → `--approve`
# (load them, no startup trust prompt — the smooth phone default); "ask" →
# `--no-approve` (ignore project-local resources for the run). Default
# "trust" so an interactive phone launch never stalls on a trust prompt.
VALID_PI_TRUST_MODES = ("trust", "ask")
DEFAULT_PI_TRUST_MODE = "trust"

# Grok Build launch knobs.
# Reasoning tier for `--reasoning-effort` — the exact set the CLI accepts,
# probed off the binary rather than the docs (`grok --reasoning-effort bogus`
# answers "use one of: high, medium, low", verified on 0.2.114). No model
# picker: `grok models` still lists only `grok-4.5`, and the launcher already
# omits a one-option control for the same reason on Antigravity.
VALID_GROK_EFFORTS = ("low", "medium", "high")
DEFAULT_GROK_EFFORT = "high"

# Permission mode for the `grok` launch, mirroring Claude's and Codex's
# auto/skip rather than exposing grok's own six-value `--permission-mode`
# space (default|acceptEdits|auto|dontAsk|bypassPermissions|plan). "auto" →
# `--permission-mode auto` (no prompts, guard rails intact); "skip" →
# `--permission-mode bypassPermissions`. `--always-approve` is deliberately
# not surfaced — it is a third spelling of the same idea, so it would add
# surface without adding capability.
VALID_GROK_PERMISSION_MODES = ("auto", "skip")
DEFAULT_GROK_PERMISSION_MODE = "auto"

# `--remote-control` is *always* added to the generated claude command
# line — that's the whole point of the Coding tab, and the UI can't turn
# it off without breaking the workflow. The permission flag used to live
# here too; it is now user-selectable via `claude_permission_mode`.
ALWAYS_ON_CLAUDE_FLAGS = ("--remote-control",)
