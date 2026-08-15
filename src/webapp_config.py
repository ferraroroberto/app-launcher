"""Webapp-specific configuration loader.

Lives separately from `app_config.py` because these settings are
authored from the web UI ("Save defaults" button) and persist across
runs. The CLI also reads this file so both surfaces share one source
of truth.

Holds:
- network knobs (host, port)
- scan roots for Claude-Code projects and Apps
- the per-agent launch *settings* (model, effort, verbose, debug) for all
  registered coding agents (claude, codex, antigravity, copilot, pi, grok),
  together with the ``VALID_*`` / ``PI_MODEL_SPECS`` value sets each field
  validates against
- sibling-app loopback URLs (voice-transcriber, photo-ocr, local-llm-hub)
- Life OS tab settings
- terminal display and passkey / WebAuthn config
- Pushover failure-notification credentials
- auth secrets (bearer token + login password)

Config only — turning those settings into each agent's actual CLI argv is
launch logic, and lives in :mod:`src.launch_flags` (``build_*_flags``, split
out in issue #691). That module imports this one; never the reverse.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import MISSING, asdict, dataclass, field, replace
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlencode, urlparse, urlunparse

from src._json_io import atomic_write_json

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "webapp_config.json"
SAMPLE_CONFIG_PATH = PROJECT_ROOT / "config" / "webapp_config.sample.json"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8445
# Loopback port the PTY session-host binds. Never network-reachable.
DEFAULT_SESSION_HOST_PORT = 8446
# Env override for the session-host port. Set ONLY by the e2e pre-ship gate's
# autoboot so a disposable webapp can be pointed at a disposable, free-port
# session-host instead of the live :8446 a running tray owns. This is what
# stops the gate from reaching into — and killing — the user's real PTY
# sessions (issue #260). Not a user-facing knob; intentionally undocumented
# in the config sample.
SESSION_HOST_PORT_ENV = "LAUNCHER_SESSION_HOST_PORT"
# Env override for the config file *path* itself. Set ONLY by the e2e
# pre-ship gate's autoboot (tests/e2e/conftest.py) so the disposable webapp
# reads AND writes a temp copy of the config instead of the real, shared
# config/webapp_config.json — a Settings-tab e2e test that clicks Save must
# never mutate the user's real file (issue #441; the #438 port corruption
# was this exact shared-file design biting). Not a user-facing knob;
# intentionally undocumented in the config sample.
WEBAPP_CONFIG_PATH_ENV = "LAUNCHER_WEBAPP_CONFIG"

# Bounded scrollback for full-screen (ratatui) agent sessions (issue #435
# follow-up) — how many lines of history the session-host retains and
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

# --- Fleet chief (issue #245) ---------------------------------------
# The standing conversational orchestrator the Board's chat mode talks to.
# `chief_model` must be a dispatchable Claude tier; the worker cap is read
# by the /chief skill over loopback as its dispatch rail — it caps how many
# worker sessions the chief may have running at once. #616 retired the
# daily-respawn setting: fleet-config#442/#449 shipped compact-and-continue
# (chief hands its own handover log back to itself on every session start),
# so an unattended daily respawn would now discard a live batch's context
# rather than protect it. A restart is still available, but only as an
# explicit operator action (the Board's chief Restart button, #617) — never
# a schedule that fires unattended.
VALID_CHIEF_MODELS = ("sonnet", "opus", "fable")
DEFAULT_CHIEF_MODEL = "fable"
DEFAULT_CHIEF_WORKER_CAP = 3
MIN_CHIEF_WORKER_CAP = 1
MAX_CHIEF_WORKER_CAP = 10

VALID_CLAUDE_MODELS = ("opus", "sonnet", "haiku", "fable")
VALID_CLAUDE_EFFORTS = ("off", "low", "medium", "high")
DEFAULT_CLAUDE_MODEL = "opus"
DEFAULT_CLAUDE_EFFORT = "high"

# Claude Code permission mode for the launch command. "auto" maps to
# `--permission-mode auto` (no prompts, but a classifier blocks dangerous
# actions — the safer autopilot); "skip" maps to the legacy
# `--dangerously-skip-permissions` (no prompts, no safety net).
VALID_CLAUDE_PERMISSION_MODES = ("auto", "skip")
DEFAULT_CLAUDE_PERMISSION_MODE = "auto"

# Codex CLI launch knobs (issue #120). Codex has no Claude-style model
# tiers — its quality knob is reasoning effort, set via the config
# override `-c model_reasoning_effort=<low|medium|high>`. The model
# itself stays the account default (gpt-5-codex via the ChatGPT-plan
# login), so there is no model picker. "off" is not offered: Codex's
# reasoning is always on.
VALID_CODEX_EFFORTS = ("low", "medium", "high")
DEFAULT_CODEX_EFFORT = "high"

# Permission mode for the `codex` launch, mirroring Claude's auto/skip.
# "auto" → `--ask-for-approval never --sandbox workspace-write` (no
# prompts, but still sandboxed — the safe autopilot); "skip" → the
# legacy `--dangerously-bypass-approvals-and-sandbox` (no prompts, no
# sandbox).
VALID_CODEX_PERMISSION_MODES = ("auto", "skip")
DEFAULT_CODEX_PERMISSION_MODE = "auto"

# Models the GitHub Copilot CLI accepts for the `--model` flag (and the
# in-session `/model` command). Source: `copilot help config`. An empty
# `copilot_model` means "don't pass --model" — the CLI then uses its own
# configured default. This list will drift as GitHub adds models; refresh
# it from `copilot help config` when that happens.
VALID_COPILOT_MODELS = (
    "claude-sonnet-5",
    "claude-sonnet-4.6",
    "claude-sonnet-4.5",
    "claude-haiku-4.5",
    "claude-fable-5",
    "claude-opus-4.8",
    "claude-opus-4.8-fast",
    "claude-opus-4.7",
    "claude-opus-4.6",
    "claude-opus-4.5",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.3-codex",
    "gpt-5.4-mini",
    "gpt-5-mini",
    "gemini-3.1-pro-preview",
    "gemini-3.5-flash",
    "kimi-k2.7-code",
)

# Pi coding-agent launch models (issues #273, #288). The Coding tab shows a
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
#     `pi -p --provider openai-codex --model openai-codex/gpt-5.5` completes
#     cleanly with no key set.
#
# Each option maps to (provider, full `provider/id` model arg, display label);
# `build_pi_flags` switches `--provider`/`--model` on the chosen option.
# `pi_model` is never empty — an unknown value falls back to DEFAULT_PI_MODEL
# so the launch can never slip onto a billing path. Refresh the model ids from
# `pi --list-models claude-agent-sdk` / `pi --list-models openai-codex`.
PI_MODEL_SPECS: dict = {
    "claude-opus-4-8": ("claude-agent-sdk", "claude-agent-sdk/claude-opus-4-8", "Opus"),
    "claude-sonnet-4-6": ("claude-agent-sdk", "claude-agent-sdk/claude-sonnet-4-6", "Sonnet"),
    "gpt-5.5": ("openai-codex", "openai-codex/gpt-5.5", "GPT"),
}
VALID_PI_MODELS = tuple(PI_MODEL_SPECS)
DEFAULT_PI_MODEL = "claude-opus-4-8"

# Pi reasoning effort, mapped to pi's `--thinking <level>` flag (issue #288).
# A small segmented control mirroring Claude's Effort; defaults high (the user
# changes it in-session with Shift+Tab if needed). Pi's full ladder is
# off/minimal/low/medium/high/xhigh; the UI offers the same small set as the
# other agents.
VALID_PI_EFFORTS = ("low", "medium", "high")
DEFAULT_PI_EFFORT = "high"

# Pi project-trust mode, mapped to pi's `--approve`/`--no-approve` flag
# (issue #288). NOTE: this is NOT a tool-execution permission gate like
# Claude's/Codex's auto/skip — pi has no tool sandbox or per-action prompt
# (see pi's security.md). It governs project *trust*: whether pi loads
# project-local `.pi/` settings/extensions/skills. "trust" → `--approve`
# (load them, no startup trust prompt — the smooth phone default); "ask" →
# `--no-approve` (ignore project-local resources for the run). Default
# "trust" so an interactive phone launch never stalls on a trust prompt.
VALID_PI_TRUST_MODES = ("trust", "ask")
DEFAULT_PI_TRUST_MODE = "trust"

# Grok Build launch knobs (issue #667, filling in #626's deliberate stub).
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


def _default_projects_dir() -> str:
    """Default to the parent of this repo (so siblings are visible)."""
    return str(PROJECT_ROOT.parent)


def _default_life_os_dir() -> str:
    """Default to the sibling ``life-os`` checkout next to this repo."""
    return str(PROJECT_ROOT.parent / "life-os")


def _default_claude_config_dir() -> str:
    """Default to the sibling ``fleet-config`` checkout next to this repo."""
    return str(PROJECT_ROOT.parent / "fleet-config")


def _default_sessions_state_file() -> str:
    """Where fleet-config's ``session_state`` hook writes the board rows."""
    return str(Path.home() / ".claude" / "hooks" / "state" / "sessions-state.json")


def _default_rate_limits_file() -> str:
    """Where fleet-config's statusline writer caches 5h/7d usage % (issue #326)."""
    return str(Path.home() / ".claude" / "hooks" / "state" / "rate-limits.json")


def _default_context_filter_mode_file() -> str:
    """Where the fleet context filter's PreToolUse hook reads its machine-wide
    off/shadow/rewrite switch (fleet-config#544, issue #713)."""
    return str(Path.home() / ".fleet-context-filter" / "mode.json")


def _default_context_filter_log_file() -> str:
    """Where the fleet context filter appends its shadow/rewrite telemetry
    (fleet-config#544, issue #713) — one JSON row per hook invocation."""
    return str(Path.home() / ".fleet-context-filter" / "shadow.jsonl")


@dataclass
class WebappConfig:
    """User-authored, persisted webapp settings."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    # Master folder whose direct child directories the Claude Code tab
    # lists as launchable projects.
    projects_dir: str = field(default_factory=_default_projects_dir)
    # gitignore-style patterns: directory names under `projects_dir` to
    # exclude from the Claude Code tab (matched case-insensitively, `*`
    # globs honoured). VCS / build dirs are always skipped regardless.
    projects_ignore: list = field(default_factory=list)
    # Project ids (scanner slugs) the user starred as favorites in the
    # Coding tab (issue #250). Favorites sort to the top of the project
    # list and can be filtered to on their own. Stored exactly like
    # `projects_ignore` — a plain string list in this same config — so the
    # feature needs no new file.
    coding_favorites: list = field(default_factory=list)
    # Agent ids whose Coding-row launch button the user hid (issue #666),
    # plus the pseudo-id `github` for the repo-issues button. A *hidden*
    # list rather than a visible one so a newly registered agent shows up
    # by default and needs no config migration. Stored like
    # `coding_favorites` — a plain string list in this same config.
    coding_hidden_agents: list = field(default_factory=list)
    # Where the Apps tab scans recursively for launcher `.bat` files.
    apps_scan_root: str = field(default_factory=_default_projects_dir)
    # Root of the life-os checkout the Life OS tab surfaces (issue #102).
    # Skills live at `<life_os_dir>/.claude/skills`, identity at
    # `<life_os_dir>/identity`. When the skills dir doesn't exist the tab
    # shows disabled, the same way the Coding tab handles a missing
    # `projects_dir`.
    life_os_dir: str = field(default_factory=_default_life_os_dir)
    # Root of the fleet-config checkout whose `architecture/` directory holds
    # the rendered fleet system map (issue #173). The Coding tab's 🗺️ System
    # map section serves `<claude_config_dir>/architecture/system-map.png`;
    # when the PNG is absent the section hides, the same way the Life OS tab
    # handles a missing life-os checkout.
    claude_config_dir: str = field(default_factory=_default_claude_config_dir)
    # --- Board tab (issue #300 / #164) -----------------------------------
    # The sessions-state file written by fleet-config's session_state hook
    # (fleet-config#91). The board reads it defensively — absent/corrupt/stale
    # degrades to unknown session status, never an error.
    sessions_state_file: str = field(default_factory=_default_sessions_state_file)
    # The rate-limits cache a fleet-config statusline writer maintains
    # (fleet-config#259 / issue #326) — 5h/7d Claude usage % + reset times.
    # Read defensively like sessions_state_file: absent/corrupt/stale hides
    # the Board's usage badges, never an error.
    rate_limits_file: str = field(default_factory=_default_rate_limits_file)
    # --- Context filter (issue #713, fleet-config#392/#541/#544) ---------
    # The machine-wide mode switch (off/shadow/rewrite) and telemetry log the
    # fleet's PreToolUse context-filter hook reads/writes. The mode itself is
    # NOT stored here — it lives only in mode.json, so this launcher process
    # and every coding-agent session on the machine always read the one true
    # value. Read defensively like sessions_state_file / rate_limits_file:
    # absent/corrupt degrades the Settings panel, never an error.
    context_filter_mode_file: str = field(default_factory=_default_context_filter_mode_file)
    context_filter_log_file: str = field(default_factory=_default_context_filter_log_file)
    # GitHub owner whose repos the Board's gh searches span (backlog / PRs /
    # done-today). One owner covers the whole fleet.
    github_owner: str = "ferraroroberto"
    # Persisted Claude Code launch flag defaults.
    claude_model: str = DEFAULT_CLAUDE_MODEL
    claude_effort: str = DEFAULT_CLAUDE_EFFORT
    claude_verbose: bool = True
    claude_debug: bool = False
    # Permission mode for the `claude` launch — "auto" or "skip"
    # (see VALID_CLAUDE_PERMISSION_MODES).
    claude_permission_mode: str = DEFAULT_CLAUDE_PERMISSION_MODE
    # Antigravity CLI launch toggles (issue #45 follow-up). The Antigravity
    # CLI exposes no model / effort / verbose flags — its model is chosen
    # with `/model` in-session — so these two switches are the whole story.
    antigravity_skip_permissions: bool = False
    antigravity_sandbox: bool = False
    # Codex CLI launch settings (issue #120). `codex_effort` is the
    # reasoning tier (low/medium/high); `codex_permission_mode` mirrors
    # Claude's auto/skip. The model stays the account default — no picker.
    codex_effort: str = DEFAULT_CODEX_EFFORT
    codex_permission_mode: str = DEFAULT_CODEX_PERMISSION_MODE
    # GitHub Copilot CLI launch settings (issue #48). `copilot_model` is
    # the `--model` value (empty = let the CLI use its own default);
    # `copilot_skip_permissions` is the opt-in allow-all switch.
    copilot_skip_permissions: bool = False
    copilot_model: str = ""
    # Grok Build launch settings (issue #667). `grok_effort` is the
    # reasoning tier (low/medium/high); `grok_permission_mode` mirrors
    # Claude's and Codex's auto/skip. No model picker — see VALID_GROK_*.
    grok_effort: str = DEFAULT_GROK_EFFORT
    grok_permission_mode: str = DEFAULT_GROK_PERMISSION_MODE
    # Pi coding agent launch settings (issues #273, #288). `pi_model` is one
    # of three segmented options (Opus/Sonnet on the claude-agent-sdk
    # subscription path, GPT on the openai-codex ChatGPT-plan path) — never
    # empty; `build_pi_flags` falls back to DEFAULT_PI_MODEL for an unknown
    # value so the launch can't slip onto a billing path. `pi_effort` maps to
    # `--thinking` (default high); `pi_trust_mode` maps to `--approve` /
    # `--no-approve` (project trust, not a tool-permission gate — see
    # VALID_PI_TRUST_MODES).
    pi_model: str = DEFAULT_PI_MODEL
    pi_effort: str = DEFAULT_PI_EFFORT
    pi_trust_mode: str = DEFAULT_PI_TRUST_MODE
    # --- Fleet chief (issue #245) ----------------------------------------
    # Settings for the standing conversational orchestrator (Board chat
    # mode). Editable from the Board's chief-settings dialog; the worker
    # cap is also read by the /chief skill over loopback.
    chief_model: str = DEFAULT_CHIEF_MODEL
    chief_worker_cap: int = DEFAULT_CHIEF_WORKER_CAP
    # Bearer token enforced when the request did NOT come from a
    # loopback IP. Empty string disables enforcement entirely.
    auth_token: str = ""
    # Optional password gate that hands the bearer token back to the
    # browser when the user types it correctly. Lets a fresh device
    # bootstrap without copy-pasting a tokenised URL.
    auth_password: str = ""
    # --- interactive phone terminal (issue #1) ---------------------------
    # Loopback port the PTY session-host binds (never network-reachable).
    session_host_port: int = DEFAULT_SESSION_HOST_PORT
    # Extra IPs / CIDRs allowed to reach the terminal endpoints on top of
    # loopback + the Tailscale CGNAT range (100.64.0.0/10). Empty by default.
    tailnet_allowlist: list = field(default_factory=list)
    # When true, launching a session from the phone also opens an
    # interactive terminal window for it on the PC (over loopback, so it
    # bypasses the Tailscale + passkey gate). Input works from both sides.
    claude_show_local_window: bool = True
    # Bounded scrollback (issue #435 follow-up): how many lines of a
    # full-screen (ratatui) agent's history the session-host retains and
    # replays on a (re)connect. See DEFAULT_TERMINAL_HISTORY_LINES for the
    # real-session evidence behind the default.
    terminal_history_lines: int = DEFAULT_TERMINAL_HISTORY_LINES
    # WebAuthn relying-party identity for the passkey gate. rp_id is the
    # bare tailnet hostname (e.g. "pc.tailnet.ts.net"); origin is the full
    # https origin the phone connects to. Empty disables the passkey gate.
    webauthn_rp_id: str = ""
    webauthn_rp_name: str = "Launcher"
    webauthn_origin: str = ""
    # --- voice dictation (issue #165) -----------------------------------
    # Base URL of the sibling voice-transcriber webapp, whose consumable
    # session API (POST /api/sessions → /upload) transcribes a recording
    # dictated from the Coding terminal's compose bar. The webapp proxies
    # to it over loopback (verify=False on the self-signed cert), so the
    # phone never talks to it directly. Empty string disables the feature
    # (the 🎤 record button hides). Defaults to the voice-transcriber's
    # loopback HTTPS port.
    voice_transcriber_url: str = "https://127.0.0.1:8443"
    # --- screenshot OCR (issue #171) ------------------------------------
    # Base URL of the sibling photo-ocr webapp, whose consumable single-shot
    # API (POST /api/extract) returns clean text for a screenshot captured
    # from the Coding terminal's compose bar. The webapp proxies to it over
    # loopback (verify=False on the self-signed cert), so the phone never
    # talks to it directly — the pixel counterpart to voice_transcriber_url.
    # Empty string disables the feature (the 📷 OCR button hides). Defaults
    # to the photo-ocr loopback HTTPS port.
    photo_ocr_url: str = "https://127.0.0.1:8444"
    # --- read-aloud hub TTS (issue #203) --------------------------------
    # Base URL of the local-llm-hub, whose OpenAI-shape POST /v1/audio/speech
    # synthesizes the Coding terminal's 🔊 read-aloud with the high-quality
    # Orpheus voice (streamed WAV). The webapp proxies to it over loopback so
    # the phone never talks to it directly. Empty string disables the hub
    # path (🔊 falls back to the on-device Web Speech voice). Plain HTTP — the
    # hub binds loopback only and serves no TLS, unlike the sibling apps above.
    llm_hub_url: str = "http://127.0.0.1:8000"
    # --- Jobs-tab failure notifications (issue #66) ---------------------
    # Pushover credentials — both empty means no-op notifier (executor
    # still finalises runs identically). The master switch
    # `notify_on_failure` defaults off so the feature ships dormant.
    pushover_api_token: str = ""
    pushover_user_key: str = ""
    notify_on_failure: bool = False
    # Also fire when the consecutive-failure streak hits this count
    # (useful when single-failure pushes are muted via Pushover quiet
    # hours). 0 disables the streak fire.
    notify_failure_streak: int = 0
    # Pipe the run's output tail through the local LLM hub
    # (http://127.0.0.1:8000, claude-haiku-4-5) for a one-line
    # "what went wrong" summary prepended to the push body. Default off
    # so the issue lands without a hard dependency on the hub.
    notify_failure_summary: bool = False
    # --- Per-job Telegram failure alerts (issue #597) --------------------
    # Credentials for the vendored src/notify Telegram primitive. Both
    # empty means no-op — the executor still finalises runs identically.
    # Unlike notify_on_failure above (global, Pushover, every job), this
    # channel fires per job: only jobs with Job.alert_on_failure=True push
    # here, so the shared Telegram chat isn't spammed by every failure.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # --- Jobs missed-fire coverage (issue #697) --------------------------
    # How often the webapp re-scans every scheduled job for coverage: a
    # missing/disabled Task Scheduler entry, or an elapsed slot with no run
    # record. 0 disables the background tick entirely (the `/api/jobs` badge
    # still computes lazily on poll). Deliberately *on* by default — unlike
    # the notify_* switches above it pushes nothing on its own: alerts still
    # route through notify_on_failure / Job.alert_on_failure, both opt-in.
    jobs_coverage_interval_minutes: int = 60
    # --- Job secrets (issues #73, #72) ----------------------------------
    # One gitignored place for secret values, referenced from jobs.json by
    # opaque "$secret:<key>" strings resolved at fire time
    # (src.jobs_secrets): a job's webhook.secret (issue #73) and every
    # Job.env value (issue #72) both draw from this dict, so a rotated
    # credential lives here and never in jobs.json. Loaded from the
    # "secrets" key, falling back to the legacy "webhook_secrets" key this
    # block shipped under before #72 generalized it. Empty by default.
    secrets: Dict[str, str] = field(default_factory=dict)
    # --- Scoped API bearer tokens (issue #72) ---------------------------
    # List of token records minted from the Settings tab (see
    # src.api_tokens): {id, label, salt, hash, scope, created_at,
    # last_used_at}. Only the salted SHA-256 hash is stored — the raw
    # token is shown once at mint time and discarded. scope is "*" or
    # {"jobs": [ids]}; a job-scoped token can only fire its allowed jobs
    # (POST /api/jobs/<id>/run) and nothing else. The legacy auth_token
    # above keeps working unchanged with implicit scope "*".
    api_tokens: list = field(default_factory=list)


def _apply_session_host_override(cfg: WebappConfig) -> WebappConfig:
    """Apply the ``LAUNCHER_SESSION_HOST_PORT`` env override, if set and valid.

    The webapp subprocess reads its session-host port from config; the e2e
    pre-ship gate sets this env var so its disposable webapp connects to a
    disposable, free-port session-host rather than adopting the live :8446
    (issue #260). A missing/blank/invalid value leaves the configured port
    untouched, so normal runs are unaffected.
    """
    raw = os.environ.get(SESSION_HOST_PORT_ENV, "").strip()
    if not raw:
        return cfg
    try:
        port = int(raw)
    except ValueError:
        logger.warning(
            "⚠️  ignoring non-integer %s=%r", SESSION_HOST_PORT_ENV, raw
        )
        return cfg
    if not (1 <= port <= 65535):
        logger.warning(
            "⚠️  ignoring out-of-range %s=%d", SESSION_HOST_PORT_ENV, port
        )
        return cfg
    cfg.session_host_port = port
    return cfg


def _load_secrets(raw: dict, default: Any) -> Dict[str, str]:
    """``secrets``, honouring the legacy ``webhook_secrets`` key.

    The block shipped under ``webhook_secrets`` before #72 generalized it
    from webhook secrets to every ``Job.env`` value, and old config files in
    the wild still spell it that way.
    """
    return {
        str(key): str(value)
        for key, value in (
            raw.get("secrets") or raw.get("webhook_secrets") or {}
        ).items()
    }


def _load_api_tokens(raw: dict, default: Any) -> list:
    """``api_tokens`` — a list of *records*, not of strings, and a malformed
    entry is dropped rather than stringified: a half-written token record can
    only ever fail closed."""
    return [
        dict(token)
        for token in (raw.get("api_tokens") or [])
        if isinstance(token, dict)
    ]


#: Fields whose on-disk shape can't be derived from the annotation alone.
#: Everything else is handled by the generic loop in
#: :func:`load_webapp_config`, so a new setting is declared once — on
#: :class:`WebappConfig` — and both directions follow it.
_CUSTOM_LOADERS: Dict[str, Callable[[dict, Any], Any]] = {
    "secrets": _load_secrets,
    "api_tokens": _load_api_tokens,
}


def _coerce(value: Any, annotation: Any, default: Any) -> Any:
    """Cast one raw JSON value to the type its dataclass field declares.

    ``from __future__ import annotations`` is active, so a field's ``type``
    arrives as the *string* ``"str"`` / ``"int"`` / ``"bool"`` / ``"list"``;
    both spellings are accepted so this keeps working if that import ever
    goes away. An unusable number falls back to the declared default rather
    than raising — a hand-edited config with a null in an int field
    shouldn't take the whole webapp down. Unknown annotations pass through
    untouched; those are the ones that belong in ``_CUSTOM_LOADERS``.
    """
    if annotation in (str, "str"):
        return str(value)
    if annotation in (bool, "bool"):
        # A hand-edited config's non-boolean value (e.g. the string
        # ``"false"``, truthy under `bool()`) or an unusable null (were it
        # ever to reach here) falls back to the declared default rather
        # than being coerced by truthiness (issue #755) — matching the int
        # branch's discipline just below.
        return value if isinstance(value, bool) else default
    if annotation in (int, "int"):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if annotation in (list, "list"):
        return [str(item) for item in value]
    return value


def _load_field(spec, raw: dict) -> Any:
    """Read one :class:`WebappConfig` field out of the parsed JSON.

    Two default disciplines, both inherited from the per-field block this
    replaced and both derivable from the field itself:

    - A ``default_factory`` field (every path-ish setting, plus the list and
      dict ones) treats *any* falsy on-disk value as absent and re-derives
      the default — an empty ``projects_dir`` means "you never set one", not
      "scan the filesystem root".
    - A literal-default field keeps a falsy value as authored, because that
      is how features here are switched off: an empty ``voice_transcriber_url``
      hides the 🎤 button, an empty ``auth_token`` disables the bearer gate.

    Collapsing the two would silently re-enable whatever the user turned off,
    so the distinction is load-bearing rather than incidental. An explicit
    JSON ``null`` is the one falsy value treated as absent everywhere: it is
    never a meaningful "off" for any setting here (``""`` and ``0`` are), and
    the old per-field block turned it into ``"None"`` or a ``TypeError``
    depending on the field.
    """
    custom = _CUSTOM_LOADERS.get(spec.name)
    if custom is not None:
        return custom(raw, None)
    if spec.default_factory is not MISSING:  # type: ignore[misc]
        default = spec.default_factory()
        value = raw.get(spec.name)
        return _coerce(value, spec.type, default) if value else default
    default = spec.default
    value = raw.get(spec.name, default)
    if value is None:
        return default
    return _coerce(value, spec.type, default)


def _resolve_config_path(path: Optional[Path]) -> Path:
    """Resolve the config file path: explicit arg > ``LAUNCHER_WEBAPP_CONFIG``
    env override (e2e-gate isolation, issue #441) > the repo default.

    Consulted by both :func:`load_webapp_config` and
    :func:`save_webapp_config` so the override is symmetric — a process
    pointed at a temp copy reads and writes that copy, never half of each.
    """
    if path is not None:
        return Path(path)
    env = os.environ.get(WEBAPP_CONFIG_PATH_ENV, "").strip()
    if env:
        return Path(env)
    return DEFAULT_CONFIG_PATH


def load_webapp_config(
    path: Optional[Path] = None, *, apply_env_override: bool = True
) -> WebappConfig:
    """Load the webapp config, falling back to defaults if the file is missing.

    ``apply_env_override=False`` skips the ``LAUNCHER_SESSION_HOST_PORT``
    override (see :func:`_apply_session_host_override`) — used by
    :func:`update_webapp_config` so a config PATCH made against the e2e
    gate's disposable autobooted webapp (which sets that env var) can never
    bake the disposable session-host port into the real, shared
    ``config/webapp_config.json`` on save. Every other caller wants the
    override applied, hence the default.
    """
    target = _resolve_config_path(path)
    if not target.exists():
        logger.info(
            f"📂 webapp_config not found at {target}, using defaults "
            f"(file will be created when settings change)"
        )
        cfg = WebappConfig()
        return _apply_session_host_override(cfg) if apply_env_override else cfg

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            f"⚠️  Could not read {target} ({exc}); falling back to defaults"
        )
        cfg = WebappConfig()
        return _apply_session_host_override(cfg) if apply_env_override else cfg

    if not isinstance(raw, dict):
        logger.warning(
            f"⚠️  {target} is not a JSON object; falling back to defaults"
        )
        cfg = WebappConfig()
        return _apply_session_host_override(cfg) if apply_env_override else cfg

    # One generic pass over the dataclass's own field list (issue #722).
    # The ~55 settings used to be enumerated three times — once as fields,
    # once as a per-field ``raw.get`` block here, once as a payload dict in
    # ``save_webapp_config`` — which meant three edits per new setting and a
    # field silently stuck on its default whenever one was forgotten. The
    # dataclass already carries the name, the type and the default, so it is
    # the single declaration both directions now read.
    cfg = WebappConfig(
        **{
            spec.name: _load_field(spec, raw)
            for spec in dataclass_fields(WebappConfig)
        }
    )
    if apply_env_override:
        _apply_session_host_override(cfg)
    _validate(cfg)
    return cfg


def save_webapp_config(cfg: WebappConfig, path: Optional[Path] = None) -> Path:
    """Atomically write the config back to disk."""
    target = _resolve_config_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Derived straight from the dataclass (issue #722) — the field list, in
    # declaration order, IS the on-disk schema. asdict() also deep-copies the
    # list/dict fields, so the writer can never alias live config state.
    payload = asdict(cfg)
    atomic_write_json(target, payload)
    logger.info(f"💾 Saved webapp_config to {target}")
    return target


def update_webapp_config(**fields) -> WebappConfig:
    """Read, patch, save — convenience for the API endpoint.

    Loads the *un-overridden* on-disk config (``apply_env_override=False``)
    so a save can never bake the e2e gate's disposable
    ``LAUNCHER_SESSION_HOST_PORT`` into the real, shared
    ``config/webapp_config.json`` — that env var is a runtime-only override
    for the disposable autobooted webapp, never something to persist. The
    returned config still carries the override applied, so the caller's own
    in-process state (e.g. the running webapp's ``app.state.webapp_config``)
    keeps talking to the right session-host for the rest of that process's
    life — only the on-disk file is protected.
    """
    current = load_webapp_config(apply_env_override=False)
    patched = replace(current, **fields)
    _validate(patched)
    save_webapp_config(patched)
    return _apply_session_host_override(patched)


def append_auth_token(url: str, token: Optional[str]) -> str:
    """Return ``url`` with ``?token=<token>`` appended when ``token`` is set."""
    if not token:
        return url
    parsed = urlparse(url)
    existing = parsed.query
    extra = urlencode({"token": token})
    new_query = f"{existing}&{extra}" if existing else extra
    return urlunparse(parsed._replace(query=new_query))


def _validate(cfg: WebappConfig) -> None:
    if not (1 <= cfg.port <= 65535):
        raise ValueError(f"port out of range: {cfg.port}")
    if not (1 <= cfg.session_host_port <= 65535):
        raise ValueError(
            f"session_host_port out of range: {cfg.session_host_port}"
        )
    if cfg.session_host_port == cfg.port:
        raise ValueError("session_host_port must differ from the webapp port")
    if not (MIN_TERMINAL_HISTORY_LINES <= cfg.terminal_history_lines <= MAX_TERMINAL_HISTORY_LINES):
        raise ValueError(
            f"terminal_history_lines must be between {MIN_TERMINAL_HISTORY_LINES} "
            f"and {MAX_TERMINAL_HISTORY_LINES}; got {cfg.terminal_history_lines}"
        )
    if cfg.claude_model not in VALID_CLAUDE_MODELS:
        raise ValueError(
            f"claude_model must be one of {VALID_CLAUDE_MODELS}; got {cfg.claude_model!r}"
        )
    if cfg.claude_permission_mode not in VALID_CLAUDE_PERMISSION_MODES:
        raise ValueError(
            f"claude_permission_mode must be one of {VALID_CLAUDE_PERMISSION_MODES}; "
            f"got {cfg.claude_permission_mode!r}"
        )
    if cfg.claude_effort not in VALID_CLAUDE_EFFORTS:
        raise ValueError(
            f"claude_effort must be one of {VALID_CLAUDE_EFFORTS}; got {cfg.claude_effort!r}"
        )
    if cfg.codex_effort not in VALID_CODEX_EFFORTS:
        raise ValueError(
            f"codex_effort must be one of {VALID_CODEX_EFFORTS}; got {cfg.codex_effort!r}"
        )
    if cfg.codex_permission_mode not in VALID_CODEX_PERMISSION_MODES:
        raise ValueError(
            f"codex_permission_mode must be one of {VALID_CODEX_PERMISSION_MODES}; "
            f"got {cfg.codex_permission_mode!r}"
        )
    if cfg.grok_effort not in VALID_GROK_EFFORTS:
        raise ValueError(
            f"grok_effort must be one of {VALID_GROK_EFFORTS}; got {cfg.grok_effort!r}"
        )
    if cfg.grok_permission_mode not in VALID_GROK_PERMISSION_MODES:
        raise ValueError(
            f"grok_permission_mode must be one of {VALID_GROK_PERMISSION_MODES}; "
            f"got {cfg.grok_permission_mode!r}"
        )
    if cfg.copilot_model and cfg.copilot_model not in VALID_COPILOT_MODELS:
        raise ValueError(
            f"copilot_model must be empty or one of {VALID_COPILOT_MODELS}; "
            f"got {cfg.copilot_model!r}"
        )
    if cfg.pi_model not in VALID_PI_MODELS:
        raise ValueError(
            f"pi_model must be one of {VALID_PI_MODELS}; got {cfg.pi_model!r}"
        )
    if cfg.pi_effort not in VALID_PI_EFFORTS:
        raise ValueError(
            f"pi_effort must be one of {VALID_PI_EFFORTS}; got {cfg.pi_effort!r}"
        )
    if cfg.pi_trust_mode not in VALID_PI_TRUST_MODES:
        raise ValueError(
            f"pi_trust_mode must be one of {VALID_PI_TRUST_MODES}; "
            f"got {cfg.pi_trust_mode!r}"
        )
    if cfg.notify_failure_streak < 0:
        raise ValueError(
            f"notify_failure_streak must be >= 0; got {cfg.notify_failure_streak}"
        )
    if cfg.jobs_coverage_interval_minutes < 0:
        raise ValueError(
            "jobs_coverage_interval_minutes must be >= 0; got "
            f"{cfg.jobs_coverage_interval_minutes}"
        )
    if cfg.chief_model not in VALID_CHIEF_MODELS:
        raise ValueError(
            f"chief_model must be one of {VALID_CHIEF_MODELS}; got {cfg.chief_model!r}"
        )
    if not (MIN_CHIEF_WORKER_CAP <= cfg.chief_worker_cap <= MAX_CHIEF_WORKER_CAP):
        raise ValueError(
            f"chief_worker_cap must be between {MIN_CHIEF_WORKER_CAP} and "
            f"{MAX_CHIEF_WORKER_CAP}; got {cfg.chief_worker_cap}"
        )
