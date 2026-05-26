"""Webapp-specific configuration loader.

Lives separately from `app_config.py` because these settings are
authored from the web UI ("Save defaults" button) and persist across
runs. The CLI also reads this file so both surfaces share one source
of truth.

Holds:
- network knobs (host, port)
- where to scan for Claude-Code projects and Apps
- persisted Claude-Code launch flags (model, effort, verbose, debug)
- auth secrets (bearer token + login password)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlparse, urlunparse

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "webapp_config.json"
SAMPLE_CONFIG_PATH = PROJECT_ROOT / "config" / "webapp_config.sample.json"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8445
# Loopback port the PTY session-host binds. Never network-reachable.
DEFAULT_SESSION_HOST_PORT = 8446

VALID_CLAUDE_MODELS = ("opus", "sonnet", "haiku")
VALID_CLAUDE_EFFORTS = ("off", "low", "medium", "high")
DEFAULT_CLAUDE_MODEL = "opus"
DEFAULT_CLAUDE_EFFORT = "high"

# Claude Code permission mode for the launch command. "auto" maps to
# `--permission-mode auto` (no prompts, but a classifier blocks dangerous
# actions — the safer autopilot); "skip" maps to the legacy
# `--dangerously-skip-permissions` (no prompts, no safety net).
VALID_CLAUDE_PERMISSION_MODES = ("auto", "skip")
DEFAULT_CLAUDE_PERMISSION_MODE = "auto"

# Models the GitHub Copilot CLI accepts for the `--model` flag (and the
# in-session `/model` command). Source: `copilot help config`. An empty
# `copilot_model` means "don't pass --model" — the CLI then uses its own
# configured default. This list will drift as GitHub adds models; refresh
# it from `copilot help config` when that happens.
VALID_COPILOT_MODELS = (
    "claude-sonnet-4.6",
    "claude-sonnet-4.5",
    "claude-haiku-4.5",
    "claude-opus-4.7",
    "claude-opus-4.6",
    "claude-opus-4.6-fast",
    "claude-opus-4.5",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.3-codex",
    "gpt-5.2-codex",
    "gpt-5.2",
    "gpt-5.4-mini",
    "gpt-5-mini",
    "gpt-4.1",
)

# `--remote-control` is *always* added to the generated claude command
# line — that's the whole point of the Coding tab, and the UI can't turn
# it off without breaking the workflow. The permission flag used to live
# here too; it is now user-selectable via `claude_permission_mode`.
ALWAYS_ON_CLAUDE_FLAGS = ("--remote-control",)


def _default_projects_dir() -> str:
    """Default to the parent of this repo (so siblings are visible)."""
    return str(PROJECT_ROOT.parent)


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
    # Where the Apps tab scans recursively for launcher `.bat` files.
    apps_scan_root: str = field(default_factory=_default_projects_dir)
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
    # GitHub Copilot CLI launch settings (issue #48). `copilot_model` is
    # the `--model` value (empty = let the CLI use its own default);
    # `copilot_skip_permissions` is the opt-in allow-all switch.
    copilot_skip_permissions: bool = False
    copilot_model: str = ""
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
    # WebAuthn relying-party identity for the passkey gate. rp_id is the
    # bare tailnet hostname (e.g. "pc.tailnet.ts.net"); origin is the full
    # https origin the phone connects to. Empty disables the passkey gate.
    webauthn_rp_id: str = ""
    webauthn_rp_name: str = "Launcher"
    webauthn_origin: str = ""
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


def load_webapp_config(path: Optional[Path] = None) -> WebappConfig:
    """Load the webapp config, falling back to defaults if the file is missing."""
    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not target.exists():
        logger.info(
            f"📂 webapp_config not found at {target}, using defaults "
            f"(file will be created when settings change)"
        )
        return WebappConfig()

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            f"⚠️  Could not read {target} ({exc}); falling back to defaults"
        )
        return WebappConfig()

    cfg = WebappConfig(
        host=str(raw.get("host", DEFAULT_HOST)),
        port=int(raw.get("port", DEFAULT_PORT)),
        projects_dir=str(raw.get("projects_dir") or _default_projects_dir()),
        projects_ignore=[str(p) for p in (raw.get("projects_ignore") or [])],
        apps_scan_root=str(raw.get("apps_scan_root") or _default_projects_dir()),
        claude_model=str(raw.get("claude_model", DEFAULT_CLAUDE_MODEL)),
        claude_effort=str(raw.get("claude_effort", DEFAULT_CLAUDE_EFFORT)),
        claude_verbose=bool(raw.get("claude_verbose", True)),
        claude_debug=bool(raw.get("claude_debug", False)),
        claude_permission_mode=str(
            raw.get("claude_permission_mode", DEFAULT_CLAUDE_PERMISSION_MODE)
        ),
        antigravity_skip_permissions=bool(
            raw.get("antigravity_skip_permissions", False)
        ),
        antigravity_sandbox=bool(raw.get("antigravity_sandbox", False)),
        copilot_skip_permissions=bool(
            raw.get("copilot_skip_permissions", False)
        ),
        copilot_model=str(raw.get("copilot_model", "")),
        auth_token=str(raw.get("auth_token", "")),
        auth_password=str(raw.get("auth_password", "")),
        session_host_port=int(
            raw.get("session_host_port", DEFAULT_SESSION_HOST_PORT)
        ),
        tailnet_allowlist=list(raw.get("tailnet_allowlist") or []),
        claude_show_local_window=bool(
            raw.get("claude_show_local_window", True)
        ),
        webauthn_rp_id=str(raw.get("webauthn_rp_id", "")),
        webauthn_rp_name=str(raw.get("webauthn_rp_name", "Launcher")),
        webauthn_origin=str(raw.get("webauthn_origin", "")),
        pushover_api_token=str(raw.get("pushover_api_token", "")),
        pushover_user_key=str(raw.get("pushover_user_key", "")),
        notify_on_failure=bool(raw.get("notify_on_failure", False)),
        notify_failure_streak=int(raw.get("notify_failure_streak", 0) or 0),
        notify_failure_summary=bool(raw.get("notify_failure_summary", False)),
    )
    _validate(cfg)
    return cfg


def save_webapp_config(cfg: WebappConfig, path: Optional[Path] = None) -> Path:
    """Atomically write the config back to disk."""
    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "host": cfg.host,
        "port": cfg.port,
        "projects_dir": cfg.projects_dir,
        "projects_ignore": cfg.projects_ignore,
        "apps_scan_root": cfg.apps_scan_root,
        "claude_model": cfg.claude_model,
        "claude_effort": cfg.claude_effort,
        "claude_verbose": cfg.claude_verbose,
        "claude_debug": cfg.claude_debug,
        "claude_permission_mode": cfg.claude_permission_mode,
        "antigravity_skip_permissions": cfg.antigravity_skip_permissions,
        "antigravity_sandbox": cfg.antigravity_sandbox,
        "copilot_skip_permissions": cfg.copilot_skip_permissions,
        "copilot_model": cfg.copilot_model,
        "auth_token": cfg.auth_token,
        "auth_password": cfg.auth_password,
        "session_host_port": cfg.session_host_port,
        "tailnet_allowlist": cfg.tailnet_allowlist,
        "claude_show_local_window": cfg.claude_show_local_window,
        "webauthn_rp_id": cfg.webauthn_rp_id,
        "webauthn_rp_name": cfg.webauthn_rp_name,
        "webauthn_origin": cfg.webauthn_origin,
        "pushover_api_token": cfg.pushover_api_token,
        "pushover_user_key": cfg.pushover_user_key,
        "notify_on_failure": cfg.notify_on_failure,
        "notify_failure_streak": cfg.notify_failure_streak,
        "notify_failure_summary": cfg.notify_failure_summary,
    }

    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    logger.info(f"💾 Saved webapp_config to {target}")
    return target


def update_webapp_config(**fields) -> WebappConfig:
    """Read, patch, save — convenience for the API endpoint."""
    current = load_webapp_config()
    patched = replace(current, **fields)
    _validate(patched)
    save_webapp_config(patched)
    return patched


def append_auth_token(url: str, token: Optional[str]) -> str:
    """Return ``url`` with ``?token=<token>`` appended when ``token`` is set."""
    if not token:
        return url
    parsed = urlparse(url)
    existing = parsed.query
    extra = urlencode({"token": token})
    new_query = f"{existing}&{extra}" if existing else extra
    return urlunparse(parsed._replace(query=new_query))


def build_claude_flags(cfg: WebappConfig) -> str:
    """Compose the `claude` CLI flags from the persisted defaults."""
    parts: list[str] = list(ALWAYS_ON_CLAUDE_FLAGS)
    if cfg.claude_permission_mode == "skip":
        parts.append("--dangerously-skip-permissions")
    else:
        parts.extend(["--permission-mode", "auto"])
    if cfg.claude_model in VALID_CLAUDE_MODELS:
        parts.extend(["--model", cfg.claude_model])
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


def _validate(cfg: WebappConfig) -> None:
    if not (1 <= cfg.port <= 65535):
        raise ValueError(f"port out of range: {cfg.port}")
    if not (1 <= cfg.session_host_port <= 65535):
        raise ValueError(
            f"session_host_port out of range: {cfg.session_host_port}"
        )
    if cfg.session_host_port == cfg.port:
        raise ValueError("session_host_port must differ from the webapp port")
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
    if cfg.copilot_model and cfg.copilot_model not in VALID_COPILOT_MODELS:
        raise ValueError(
            f"copilot_model must be empty or one of {VALID_COPILOT_MODELS}; "
            f"got {cfg.copilot_model!r}"
        )
    if cfg.notify_failure_streak < 0:
        raise ValueError(
            f"notify_failure_streak must be >= 0; got {cfg.notify_failure_streak}"
        )
