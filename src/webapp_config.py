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
launch logic, and lives in :mod:`src.launching.flags` (``build_*_flags``, split
out in issue #691). That module imports this one; never the reverse.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import MISSING, asdict, dataclass, field, replace
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlencode, urlparse, urlunparse

from src.agents import DEFAULT_AGENT
from src.launching.model_catalog import (
    CODEX_MODEL_SPECS,
    DEFAULT_HIDDEN_MODELS,
    load_hidden_models, migrate_renamed_models, with_visible_defaults,
)

from src._json_io import atomic_write_json
from src.usage.providers import DEFAULT_USAGE_PROVIDERS, load_usage_providers
from src.webapp_settings.values import (  # noqa: F401 — re-exported for webapp_config.<name> callers
    ALWAYS_ON_CLAUDE_FLAGS, CREDENTIAL_KEYS, DEFAULT_CHANNEL_MODEL,
    DEFAULT_CHIEF_AUTO_COMPACT_THRESHOLD, DEFAULT_CHIEF_MODEL, DEFAULT_CHIEF_WORKER_CAP,
    DEFAULT_CLAUDE_EFFORT, DEFAULT_CLAUDE_MODEL, DEFAULT_CLAUDE_PERMISSION_MODE,
    DEFAULT_CODEX_EFFORT, DEFAULT_CODEX_MODEL, DEFAULT_CODEX_MODEL_EFFORTS,
    DEFAULT_CODEX_PERMISSION_MODE, DEFAULT_CODING_MODEL_CHOICE, DEFAULT_GROK_EFFORT,
    DEFAULT_GROK_PERMISSION_MODE, DEFAULT_HOST, DEFAULT_LARGE_UPLOAD_MAX_MB,
    DEFAULT_PI_EFFORT, DEFAULT_PI_MODEL, DEFAULT_PI_TRUST_MODE, DEFAULT_PORT,
    DEFAULT_SESSION_HOST_PORT, DEFAULT_TERMINAL_HISTORY_LINES, LEGACY_CLAUDE_MODELS,
    MAX_CHIEF_AUTO_COMPACT_THRESHOLD, MAX_CHIEF_WORKER_CAP, MAX_LARGE_UPLOAD_MAX_MB,
    MAX_TERMINAL_HISTORY_LINES, MIN_CHIEF_AUTO_COMPACT_THRESHOLD, MIN_CHIEF_WORKER_CAP,
    MIN_LARGE_UPLOAD_MAX_MB, MIN_TERMINAL_HISTORY_LINES, SESSION_HOST_PORT_ENV,
    VALID_CHIEF_MODELS, VALID_CLAUDE_EFFORTS, VALID_CLAUDE_MODELS,
    VALID_CLAUDE_PERMISSION_MODES, VALID_CODEX_EFFORTS, VALID_CODEX_MODELS,
    VALID_CODEX_PERMISSION_MODES, VALID_CODING_MODEL_CHOICES, VALID_GROK_EFFORTS,
    VALID_GROK_PERMISSION_MODES, VALID_PI_EFFORTS, VALID_PI_MODELS,
    VALID_PI_TRUST_MODES, WEBAPP_CONFIG_PATH_ENV, valid_claude_efforts_for,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "webapp_config.json"



def _default_projects_dir() -> str:
    """Default to the parent of this repo (so siblings are visible)."""
    return str(PROJECT_ROOT.parent)


def _default_life_os_dir() -> str:
    """Default to the sibling ``life-os`` checkout next to this repo."""
    return str(PROJECT_ROOT.parent / "life-os")


def _default_fleet_config_dir() -> str:
    """Default to the sibling ``fleet-config`` checkout next to this repo."""
    return str(PROJECT_ROOT.parent / "fleet-config")


def _default_sessions_state_file() -> str:
    """Where fleet-config's ``session_state`` hook writes the board rows."""
    return str(Path.home() / ".claude" / "hooks" / "state" / "sessions-state.json")


def _default_chief_plan_file() -> str:
    """Where fleet-config's ``chief_ops.py plan`` writes the chief's plan (#1279)."""
    return str(Path.home() / ".claude" / "hooks" / "state" / "chief-plan.json")


def _default_chief_managed_file() -> str:
    """Where fleet-config's ``chief_managed.py`` records chief-dispatched sessions (#1644)."""
    return str(Path.home() / ".claude" / "hooks" / "state" / "chief-managed.json")


def _default_chief_inbox_dir() -> str:
    """The chief's mailbox directory (fleet-config ``docs/chief-mailbox.md``, #1644)."""
    return str(Path.home() / ".claude" / "hooks" / "state" / "chief-inbox")


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
    # Coding tab. Favorites sort to the top of the project
    # list and can be filtered to on their own. Stored exactly like
    # `projects_ignore` — a plain string list in this same config — so the
    # feature needs no new file.
    coding_favorites: list = field(default_factory=list)
    # Agent ids whose Coding-row launch button the user hid,
    # plus the pseudo-id `github` for the repo-issues button. A *hidden*
    # list rather than a visible one so a newly registered agent shows up
    # by default and needs no config migration. Stored like
    # `coding_favorites` — a plain string list in this same config.
    coding_hidden_agents: list = field(default_factory=list)
    # Provider-qualified models the dropdowns leave out (`src.launching.model_catalog`).
    hidden_models: list = field(default_factory=lambda: list(DEFAULT_HIDDEN_MODELS))
    # The one agent whose launch button stays on a Coding project row.
    # Every other visible agent — and the GitHub-issues
    # button — moved into the row's ⋯ menu, so the row is three controls
    # wide on the phone instead of nine. Stored as a plain agent id;
    # `src.agents.DEFAULT_AGENT` is the default and also the fallback the
    # config router coerces an unknown id to, so a removed agent can never
    # leave a row with no launch button at all.
    coding_favorite_agent: str = DEFAULT_AGENT
    # Provider-qualified value shown in the Coding header's compact picker.
    # The provider's own model field remains the launch source of truth.
    coding_model_choice: str = DEFAULT_CODING_MODEL_CHOICE
    # Where the Apps tab scans recursively for launcher `.bat` files.
    apps_scan_root: str = field(default_factory=_default_projects_dir)
    # Skill ids the user starred on the Life OS tab, treated as
    # `coding_favorites` treats Coding projects: sorted to the top.
    life_os_favorites: list = field(default_factory=list)
    # Root of the life-os checkout the Life OS tab surfaces:
    # skills at `.claude/skills`, identity at `identity`. Without the skills
    # dir the tab shows disabled, as Coding does without `projects_dir`.
    life_os_dir: str = field(default_factory=_default_life_os_dir)
    # A resumed Life OS conversation re-reads its skill's instructions when
    # they changed since it last loaded them (src/life_os/skill_refresh.py).
    life_os_refresh_skill_instructions: bool = True
    # Root of the fleet-config checkout. The Board, sessions, Life OS
    # conversations and the fleet-chief ensure path read their hooks, state
    # and scripts from it.
    fleet_config_dir: str = field(default_factory=_default_fleet_config_dir)
    # --- Board tab -----------------------------------
    # The sessions-state file written by fleet-config's session_state hook.
    # The board reads it defensively — absent/corrupt/stale
    # degrades to unknown session status, never an error.
    sessions_state_file: str = field(default_factory=_default_sessions_state_file)
    # The rate-limits cache a fleet-config statusline writer maintains
    # — 5h/7d Claude usage % + reset times.
    # Read defensively like sessions_state_file: absent/corrupt/stale hides
    # the Board's usage badges, never an error.
    rate_limits_file: str = field(default_factory=_default_rate_limits_file)
    # The fleet chief's plan: lanes, queue, what waits on Roberto.
    # Read defensively like sessions_state_file: absent/corrupt/unsupported
    # leaves the Board's "Chief's plan" card blank, never an error.
    chief_plan_file: str = field(default_factory=_default_chief_plan_file)
    # The session-host's reaper publishes a chief ``session-end`` event for a
    # chief-managed worker that died without running a hook: the
    # registry fleet-config's ``chief_managed.py`` writes, and the mailbox
    # directory the event goes into. Read by the session-host process only.
    chief_managed_file: str = field(default_factory=_default_chief_managed_file)
    chief_inbox_dir: str = field(default_factory=_default_chief_inbox_dir)
    # --- Context filter ---------
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
    # Overrides only: ``{model: effort}`` for a model whose effort the
    # owner changed. A model with no entry runs at its catalog default. The old
    # global ``claude_effort`` key is a legacy read, see _migrate_legacy_keys.
    claude_model_efforts: dict = field(default_factory=dict)
    claude_verbose: bool = True
    claude_debug: bool = False
    # Permission mode for the `claude` launch — "auto" or "skip"
    # (see VALID_CLAUDE_PERMISSION_MODES).
    claude_permission_mode: str = DEFAULT_CLAUDE_PERMISSION_MODE
    # Antigravity CLI launch toggles. The CLI has
    # `--model` and `--effort`, but its model is chosen with `/model`
    # in-session, so the launcher has no model to key an effort on:
    # these two switches are the whole story.
    antigravity_skip_permissions: bool = False
    antigravity_sandbox: bool = False
    # Codex CLI launch settings. Effort is remembered per model;
    # ``codex_effort`` mirrors the currently selected model for compatibility.
    codex_model: str = DEFAULT_CODEX_MODEL
    codex_effort: str = DEFAULT_CODEX_EFFORT
    codex_model_efforts: dict = field(
        default_factory=lambda: dict(DEFAULT_CODEX_MODEL_EFFORTS)
    )
    codex_permission_mode: str = DEFAULT_CODEX_PERMISSION_MODE
    # GitHub Copilot CLI launch settings.
    # `copilot_skip_permissions` is the opt-in allow-all switch, and the
    # only one: there is deliberately no model setting, because Copilot
    # resolves `--model` against the account at launch and silently
    # substitutes when it refuses (see
    # `src.launching.flags.build_copilot_flags`). Model choice belongs to the
    # CLI's own `/model` and `/config model`.
    copilot_skip_permissions: bool = False
    # Grok Build launch settings. `grok_effort` is the
    # reasoning tier (low/medium/high); `grok_permission_mode` mirrors
    # Claude's and Codex's auto/skip. No model picker — see VALID_GROK_*.
    grok_effort: str = DEFAULT_GROK_EFFORT
    grok_permission_mode: str = DEFAULT_GROK_PERMISSION_MODE
    # Pi coding agent launch settings. `pi_model` uses
    # the curated subscription-safe catalog — never
    # empty; `build_pi_flags` falls back to DEFAULT_PI_MODEL for an unknown
    # value so the launch can't slip onto a billing path. `pi_model_efforts`
    # maps to `--thinking` (default high); `pi_trust_mode` maps to `--approve` /
    # `--no-approve` (project trust, not a tool-permission gate — see
    # VALID_PI_TRUST_MODES).
    pi_model: str = DEFAULT_PI_MODEL
    # Overrides only, like ``claude_model_efforts``: ``{model: effort}``
    # sent as ``--thinking``; a model with no entry runs at its catalog default.
    pi_model_efforts: dict = field(default_factory=dict)
    pi_trust_mode: str = DEFAULT_PI_TRUST_MODE
    # --- Fleet chief ----------------------------------------
    # Settings for the standing conversational orchestrator (Board chat
    # mode). Editable from the Board's chief-settings dialog; the worker
    # cap is also read by the /chief skill over loopback.
    chief_model: str = DEFAULT_CHIEF_MODEL
    chief_worker_cap: int = DEFAULT_CHIEF_WORKER_CAP
    # Auto-compact threshold in percent, 0 = off. Set in Settings'
    # Chief card; served to the chief by GET /api/board/chief-settings.
    chief_auto_compact_threshold: int = DEFAULT_CHIEF_AUTO_COMPACT_THRESHOLD
    # The chief doorbell: type one fixed line into an idle chief whose
    # mailbox holds pending events. Off by default: it types into a live
    # session, and the chief only understands the line once fleet-config
    # ships its side, which is what turns this on.
    chief_doorbell: bool = False
    # Publish one job-failed event into the chief mailbox per failed Job run
    # run. On by default: the event only lands while a chief is live, and
    # the doorbell above decides whether the chief is woken for it.
    chief_job_failures: bool = True
    # Bearer token enforced when the request did NOT come from a
    # loopback IP. Empty string disables enforcement entirely.
    auth_token: str = ""
    # Optional password gate that hands the bearer token back to the
    # browser when the user types it correctly. Lets a fresh device
    # bootstrap without copy-pasting a tokenised URL.
    auth_password: str = ""
    # --- interactive phone terminal ---------------------------
    # Loopback port the PTY session-host binds (never network-reachable).
    session_host_port: int = DEFAULT_SESSION_HOST_PORT
    # Extra IPs / CIDRs allowed to reach the terminal endpoints on top of
    # loopback + the Tailscale CGNAT range (100.64.0.0/10). Empty by default.
    tailnet_allowlist: list = field(default_factory=list)
    # Folders, besides a session's own, that an agent may send a file to the
    # Chat pane from: its outbox and Claude's SendUserFile. %VAR% and ~
    # expand, so a temp-dir root needs no machine path. Defaults to Claude's
    # scratch base, where its SendUserFile files live; a configured non-empty
    # list replaces it (an empty one reads as unset, like every list setting).
    # Settings > Folders > Chat file folders edits it, checking each
    # folder on save; this file's load stays lenient about a folder that has
    # since gone.
    outbox_scratch_roots: list = field(default_factory=lambda: ["%TEMP%/claude"])
    # When true, launching a session from the phone also opens an
    # interactive terminal window for it on the PC (over loopback, so it
    # bypasses the Tailscale + passkey gate). Input works from both sides.
    claude_show_local_window: bool = True
    # Where the SPA opens a session on a desktop browser. Off (the default),
    # a row tap and a new launch render in the page; on, a full-control
    # session's terminal gets the dedicated PC Edge window as before. The
    # session kebab's "Open in new window" reaches the window either way.
    # Chief and API launches carry no page flag and keep mirroring.
    open_sessions_in_window: bool = False
    # A session with no remembered view opens in Chat wherever Chat exists,
    # on the phone too; off restores Terminal on the phone. An explicit
    # Terminal choice and the per-session remembered view still win.
    open_sessions_in_chat: bool = True
    # Telegram channel sessions are long-lived and serve a household
    # member, so on by default they leave the Board and the Coding tab's
    # session list for one summary line (and a read-only list) — a mis-tap
    # on their Stop would cut off a person's channel. Off restores today's
    # behaviour exactly. UI-only: the sessions themselves are untouched.
    hide_channel_sessions: bool = True
    # Start every ready Telegram channel profile when the webapp starts,
    # so the bots answer again after a reboot. Off by default: it
    # launches real sessions, so an absent setting must never arm it.
    autostart_channel_sessions: bool = False
    # Model a Telegram channel session starts on when its launch names none,
    # and the one the boot-time autostart uses.
    channel_model: str = DEFAULT_CHANNEL_MODEL
    # Where a link tapped in Chat or a transcript opens when the page runs on
    # the PC itself: "chrome" (found through the registry's App
    # Paths, falling back to the default when absent) or "system" (the
    # Windows default browser, which is how the links opened before).
    desktop_browser: str = "chrome"
    # Bounded scrollback: how many lines of a
    # full-screen (ratatui) agent's history the session-host retains and
    # replays on a (re)connect. See DEFAULT_TERMINAL_HISTORY_LINES for the
    # real-session evidence behind the default.
    terminal_history_lines: int = DEFAULT_TERMINAL_HISTORY_LINES
    # Large-file attach limit in MB, Settings-tab configurable.
    # See DEFAULT_LARGE_UPLOAD_MAX_MB.
    large_upload_max_mb: int = DEFAULT_LARGE_UPLOAD_MAX_MB
    # Usage meter providers, and the Grok auto-switch for Sonnet lanes: src/usage/.
    usage_providers: list = field(default_factory=lambda: list(DEFAULT_USAGE_PROVIDERS))
    grok_auto_switch: bool = False
    # WebAuthn relying-party identity for the passkey gate. rp_id is the
    # bare tailnet hostname (e.g. "pc.tailnet.ts.net"); origin is the full
    # https origin the phone connects to. Empty disables the passkey gate.
    webauthn_rp_id: str = ""
    webauthn_rp_name: str = "Launcher"
    webauthn_origin: str = ""
    # --- voice dictation -----------------------------------
    # Base URL of the sibling voice-transcriber webapp, whose consumable
    # session API (POST /api/sessions → /upload) transcribes a recording
    # dictated from the Coding terminal's compose bar. The webapp proxies
    # to it over loopback (verify=False on the self-signed cert), so the
    # phone never talks to it directly. Empty string disables the feature
    # (the 🎤 record button hides). Defaults to the voice-transcriber's
    # loopback HTTPS port.
    voice_transcriber_url: str = "https://127.0.0.1:8443"
    # --- screenshot OCR ------------------------------------
    # Base URL of the sibling photo-ocr webapp, whose consumable single-shot
    # API (POST /api/extract) returns clean text for a screenshot captured
    # from the Coding terminal's compose bar. The webapp proxies to it over
    # loopback (verify=False on the self-signed cert), so the phone never
    # talks to it directly — the pixel counterpart to voice_transcriber_url.
    # Empty string disables the feature (the 📷 OCR button hides). Defaults
    # to the photo-ocr loopback HTTPS port.
    photo_ocr_url: str = "https://127.0.0.1:8444"
    # --- read-aloud hub TTS --------------------------------
    # Base URL of the local-llm-hub, whose OpenAI-shape POST /v1/audio/speech
    # synthesizes the Coding terminal's 🔊 read-aloud with the high-quality
    # Orpheus voice (streamed WAV). The webapp proxies to it over loopback so
    # the phone never talks to it directly. Empty string disables the hub
    # path (🔊 falls back to the on-device Web Speech voice). Plain HTTP — the
    # hub binds loopback only and serves no TLS, unlike the sibling apps above.
    llm_hub_url: str = "http://127.0.0.1:8000"
    # --- Screen time export to task-os ---------------------
    # Where `launcher.py usage-export` and the Screen time sheet's Send to
    # task-os put each completed day's minutes (src/usage/export.py): the
    # sibling task-os's loopback URL, the task-os person id the entries
    # belong to, and the target category as area/category slugs, resolved
    # through task-os's GET /api/time/meta on every send. The person has no
    # default, so the export stays off until it is set; the category must
    # already exist in task-os (it is never created from here).
    task_os_url: str = "https://127.0.0.1:8448"
    task_os_person: str = ""
    task_os_area: str = "work"
    task_os_category: str = "launcher"
    # --- Jobs-tab failure notifications ---------------------
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
    # --- Per-job Telegram failure alerts --------------------
    # Credentials for the vendored src/notify Telegram primitive. Both
    # empty means no-op — the executor still finalises runs identically.
    # Unlike notify_on_failure above (global, Pushover, every job), this
    # channel fires per job: only jobs with Job.alert_on_failure=True push
    # here, so the shared Telegram chat isn't spammed by every failure.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # Optional chat for routine log lines, e.g. the nightly
    # zombie-cleanup summary — not alerts. Empty falls back to
    # telegram_chat_id, so a single-chat setup is unchanged. Failure alerts
    # never use it.
    telegram_chat_log: str = ""
    # --- Jobs missed-fire coverage --------------------------
    # How often the webapp re-scans every scheduled job for coverage: a
    # missing/disabled Task Scheduler entry, or an elapsed slot with no run
    # record. 0 disables the background tick entirely (the `/api/jobs` badge
    # still computes lazily on poll). Deliberately *on* by default — unlike
    # the notify_* switches above it pushes nothing on its own: alerts still
    # route through notify_on_failure / Job.alert_on_failure, both opt-in.
    jobs_coverage_interval_minutes: int = 60
    # --- webapp/sessions retention --------------------------
    # THE retention window for the per-session audit trail: every
    # webapp/sessions/<id>.log and <id>.transcript whose mtime is older than
    # this many days is deleted by the webapp's daily sweep
    # (src/session_retention.py) — except files of a live session, which are
    # never removed. 0 keeps everything forever (the earlier behaviour).
    # Deliberately not patchable from the Settings API: a deleting setting is
    # edited here, by hand, or not at all.
    session_retention_days: int = 365
    # --- Job secrets ----------------------------------
    # One gitignored place for secret values, referenced from jobs.json by
    # opaque "$secret:<key>" strings resolved at fire time
    # (src.jobs.secrets): a job's webhook.secret and every
    # Job.env value both draw from this dict, so a rotated
    # credential lives here and never in jobs.json. Loaded from the
    # "secrets" key, falling back to the legacy "webhook_secrets" key this
    # block shipped under before it was generalized. Empty by default.
    secrets: Dict[str, str] = field(default_factory=dict)
    # --- Scoped bearer tokens, from Settings > API tokens ---
    # Salted-hash records only; each raw token is shown once and discarded.
    # api_tokens (src.auth.api_tokens): {id, label, salt, hash, scope, ...}, scope
    # "*" (like auth_token) or {"jobs": [ids]}, which fires only those jobs.
    # delegate_token (src.auth.delegate_token): {salt, hash, created_at},
    # {} = off; it may only read the Board and file issues.
    api_tokens: list = field(default_factory=list)
    delegate_token: dict = field(default_factory=dict)


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


def _load_secrets(raw: dict) -> Dict[str, str]:
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


def _load_api_tokens(raw: dict) -> list:
    """``api_tokens`` — a list of *records*, not of strings, and a malformed
    entry is dropped rather than stringified: a half-written token record can
    only ever fail closed."""
    return [
        dict(token)
        for token in (raw.get("api_tokens") or [])
        if isinstance(token, dict)
    ]


def _load_pi_model(raw: dict) -> str:
    """Migrate the three pre-#845 pinned ids to their current successors."""
    value = str(raw.get("pi_model") or DEFAULT_PI_MODEL)
    return {
        "claude-opus-4-8": "claude-opus-5",
        "claude-sonnet-4-6": "claude-sonnet-5",
        "gpt-5.5": "gpt-5.6-sol",
    }.get(value, value)


def _load_codex_model_efforts(raw: dict) -> Dict[str, str]:
    """Load only supported model/effort pairs and seed newly added models.

    ``codex_effort`` mirrors the selected model's entry, and a hand-edited file
    can make them disagree. The selected model's own ``codex_effort`` has always
    been the one launched, so it wins here, and ``effective_effort`` reads the
    dict alone.
    """
    result = dict(DEFAULT_CODEX_MODEL_EFFORTS)
    authored = raw.get("codex_model_efforts") or {}
    if isinstance(authored, dict):
        for model, effort in authored.items():
            if (
                model in VALID_CODEX_MODELS
                and effort in CODEX_MODEL_SPECS[model]["efforts"]
            ):
                result[str(model)] = str(effort)
    selected = raw.get("codex_model") or DEFAULT_CODEX_MODEL
    mirrored = _load_codex_effort(raw)
    if (
        ("codex_model" in raw or "codex_model_efforts" in raw)
        and selected in VALID_CODEX_MODELS
        and mirrored in CODEX_MODEL_SPECS[selected]["efforts"]
    ):
        result[str(selected)] = mirrored
    return result


def _load_claude_model_efforts(raw: dict) -> Dict[str, str]:
    """Load the Claude overrides: only a known model with an effort it supports."""
    authored = raw.get("claude_model_efforts") or {}
    if not isinstance(authored, dict):
        return {}
    return {
        str(model): str(effort)
        for model, effort in authored.items()
        if effort in valid_claude_efforts_for(str(model))
    }


def _load_pi_model_efforts(raw: dict) -> Dict[str, str]:
    """Load the Pi overrides: only a known model with a ``--thinking`` level."""
    authored = raw.get("pi_model_efforts") or {}
    if not isinstance(authored, dict):
        return {}
    return {
        str(model): str(effort)
        for model, effort in authored.items()
        if str(model) in VALID_PI_MODELS and effort in VALID_PI_EFFORTS
    }


def _load_codex_effort(raw: dict) -> str:
    """Do not reinterpret the old global High default as Luna's preference."""
    if "codex_model" not in raw and "codex_model_efforts" not in raw:
        return DEFAULT_CODEX_EFFORT
    return str(raw.get("codex_effort") or DEFAULT_CODEX_EFFORT)


#: Fields whose on-disk shape can't be derived from the annotation alone.
#: Everything else is handled by the generic loop in
#: :func:`load_webapp_config`, so a new setting is declared once — on
#: :class:`WebappConfig` — and both directions follow it.
_CUSTOM_LOADERS: Dict[str, Callable[[dict], Any]] = {
    "secrets": _load_secrets,
    "api_tokens": _load_api_tokens,
    "pi_model": _load_pi_model,
    "codex_effort": _load_codex_effort,
    "codex_model_efforts": _load_codex_model_efforts,
    "claude_model_efforts": _load_claude_model_efforts,
    "pi_model_efforts": _load_pi_model_efforts,
    "usage_providers": load_usage_providers,
    "hidden_models": load_hidden_models,
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
        # than being coerced by truthiness — matching the int
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
        return custom(raw)
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


# Keys renamed since a live ``config/webapp_config.json`` may have been written
# (the file is gitignored, so a pull never rewrites it): old name -> new name.
# ``claude_config_dir`` held the fleet-config checkout root all along, not
# Claude's own config dir.
_LEGACY_KEYS = {"claude_config_dir": "fleet_config_dir"}


def _migrate_legacy_keys(raw: Dict[str, Any], source: Path) -> Dict[str, Any]:
    """``raw`` with each renamed key carried over to its new name.

    In memory only: the next save writes the new name and drops the old one, so
    the live file migrates itself the first time a setting changes. A new key
    already present wins over the old one.
    """
    migrated = dict(raw)
    for old, new in _LEGACY_KEYS.items():
        if old not in migrated:
            continue
        legacy = migrated.pop(old)
        if new not in migrated:
            migrated[new] = legacy
            logger.info(
                f"ℹ️  {source.name}: key '{old}' is now '{new}'; read it under "
                f"the new name (the file is rewritten on the next save)"
            )
    migrated = _migrate_global_effort(
        migrated, source, legacy_key="claude_effort", dict_key="claude_model_efforts",
        default=DEFAULT_CLAUDE_EFFORT, models=VALID_CLAUDE_MODELS,
        allowed_for=valid_claude_efforts_for,
    )
    return _migrate_global_effort(
        migrated, source, legacy_key="pi_effort", dict_key="pi_model_efforts",
        default=DEFAULT_PI_EFFORT, models=VALID_PI_MODELS,
        allowed_for=lambda model: VALID_PI_EFFORTS,
    )


def _migrate_global_effort(
    raw: Dict[str, Any],
    source: Path,
    *,
    legacy_key: str,
    dict_key: str,
    default: str,
    models: Tuple[str, ...],
    allowed_for: Callable[[str], Tuple[str, ...]],
) -> Dict[str, Any]:
    """Turn a pre-#1711 global effort key into per-model overrides.

    One global value was sent to every model of the agent, so ``default`` (the
    old default, ``high``) cannot be told from "never chose" and is dropped:
    copying it to every model would keep Opus one level above its own default,
    which is the bug. Any other value was a deliberate choice and is carried to
    every model that supports it, so nothing changes silently (the live Pi
    ``medium`` stays ``medium``). An existing ``dict_key`` wins. In memory only,
    like the key renames above: the next save writes the new shape and drops the
    old key. Claude was Step 1 (#1712), Pi Step 2 (#1713).
    """
    if legacy_key not in raw:
        return raw
    migrated = dict(raw)
    legacy = migrated.pop(legacy_key)
    if dict_key in migrated:
        return migrated
    carried = {model: legacy for model in models if legacy in allowed_for(model)}
    if legacy in (None, "", default) or not carried:
        logger.info(
            f"ℹ️  {source.name}: legacy {legacy_key} {legacy!r} dropped; every "
            f"model runs at its own default effort"
        )
        return migrated
    migrated[dict_key] = carried
    logger.info(
        f"ℹ️  {source.name}: legacy {legacy_key} {legacy!r} carried to every "
        f"model as a per-model override"
    )
    return migrated


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

    raw = migrate_renamed_models(_migrate_legacy_keys(raw, target))

    # One generic pass over the dataclass's own field list: the dataclass is
    # the single declaration of name, type and default both directions read.
    cfg = WebappConfig(
        **{
            spec.name: _load_field(spec, raw)
            for spec in dataclass_fields(WebappConfig)
        }
    )
    cfg = with_visible_defaults(cfg, VALID_CODING_MODEL_CHOICES)
    if apply_env_override:
        _apply_session_host_override(cfg)
    _validate(cfg)
    return cfg


def save_webapp_config(cfg: WebappConfig, path: Optional[Path] = None) -> Path:
    """Atomically write the config back to disk."""
    target = _resolve_config_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Derived straight from the dataclass — the field list, in
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
    patched = with_visible_defaults(replace(current, **fields), VALID_CODING_MODEL_CHOICES)
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


#: Enum-valued settings validated by plain membership — a new
#: enum field is one row here instead of a hand-written ``not in`` check, the
#: same declarative-table shape ``_CUSTOM_LOADERS`` above and
#: ``middleware._TERMINAL_GUARD_RULES`` already use.
_ENUM_VALIDATIONS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("coding_model_choice", VALID_CODING_MODEL_CHOICES),
    ("claude_model", VALID_CLAUDE_MODELS),
    ("channel_model", VALID_CLAUDE_MODELS),
    ("claude_permission_mode", VALID_CLAUDE_PERMISSION_MODES),
    ("codex_model", VALID_CODEX_MODELS),
    ("codex_effort", VALID_CODEX_EFFORTS),
    ("codex_permission_mode", VALID_CODEX_PERMISSION_MODES),
    ("grok_effort", VALID_GROK_EFFORTS),
    ("grok_permission_mode", VALID_GROK_PERMISSION_MODES),
    ("pi_model", VALID_PI_MODELS),
    ("pi_trust_mode", VALID_PI_TRUST_MODES),
    ("chief_model", VALID_CHIEF_MODELS),
)


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
    if not (MIN_LARGE_UPLOAD_MAX_MB <= cfg.large_upload_max_mb <= MAX_LARGE_UPLOAD_MAX_MB):
        raise ValueError(
            f"large_upload_max_mb must be between {MIN_LARGE_UPLOAD_MAX_MB} "
            f"and {MAX_LARGE_UPLOAD_MAX_MB}; got {cfg.large_upload_max_mb}"
        )
    for field_name, allowed in _ENUM_VALIDATIONS:
        value = getattr(cfg, field_name)
        if value not in allowed:
            raise ValueError(
                f"{field_name} must be one of {allowed}; got {value!r}"
            )
    if not isinstance(cfg.claude_model_efforts, dict):
        raise ValueError("claude_model_efforts must be an object")
    for model, effort in cfg.claude_model_efforts.items():
        if model not in VALID_CLAUDE_MODELS:
            raise ValueError(f"unknown Claude model in claude_model_efforts: {model!r}")
        supported = valid_claude_efforts_for(model)
        if effort not in supported:
            raise ValueError(
                f"Claude effort {effort!r} is not supported by {model}; expected one of {supported}"
            )
    if not isinstance(cfg.pi_model_efforts, dict):
        raise ValueError("pi_model_efforts must be an object")
    for model, effort in cfg.pi_model_efforts.items():
        if model not in VALID_PI_MODELS:
            raise ValueError(f"unknown Pi model in pi_model_efforts: {model!r}")
        if effort not in VALID_PI_EFFORTS:
            raise ValueError(
                f"Pi effort {effort!r} is not supported; expected one of {VALID_PI_EFFORTS}"
            )
    if not isinstance(cfg.codex_model_efforts, dict):
        raise ValueError("codex_model_efforts must be an object")
    for model, effort in cfg.codex_model_efforts.items():
        if model not in VALID_CODEX_MODELS:
            raise ValueError(f"unknown Codex model in codex_model_efforts: {model!r}")
        supported = CODEX_MODEL_SPECS[model]["efforts"]
        if effort not in supported:
            raise ValueError(
                f"Codex effort {effort!r} is not supported by {model}; expected one of {supported}"
            )
    if cfg.codex_effort not in CODEX_MODEL_SPECS[cfg.codex_model]["efforts"]:
        raise ValueError(
            f"Codex effort {cfg.codex_effort!r} is not supported by {cfg.codex_model}"
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
    if cfg.session_retention_days < 0:
        raise ValueError(
            f"session_retention_days must be >= 0; got {cfg.session_retention_days}"
        )
    if not (MIN_CHIEF_WORKER_CAP <= cfg.chief_worker_cap <= MAX_CHIEF_WORKER_CAP):
        raise ValueError(
            f"chief_worker_cap must be between {MIN_CHIEF_WORKER_CAP} and "
            f"{MAX_CHIEF_WORKER_CAP}; got {cfg.chief_worker_cap}"
        )
    threshold = cfg.chief_auto_compact_threshold
    if threshold != 0 and not (
        MIN_CHIEF_AUTO_COMPACT_THRESHOLD <= threshold <= MAX_CHIEF_AUTO_COMPACT_THRESHOLD
    ):
        raise ValueError(
            "chief_auto_compact_threshold must be 0 (off) or between "
            f"{MIN_CHIEF_AUTO_COMPACT_THRESHOLD} and "
            f"{MAX_CHIEF_AUTO_COMPACT_THRESHOLD} percent; got {threshold}"
        )
