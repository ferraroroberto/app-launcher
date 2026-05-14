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

# Two switches are *always* added to the generated claude command line.
# Listing them in user config would be misleading because the UI can't
# turn them off without breaking the remote-control workflow.
ALWAYS_ON_CLAUDE_FLAGS = ("--remote-control", "--dangerously-skip-permissions")


def _default_projects_dir() -> str:
    """Default to the parent of this repo (so siblings are visible)."""
    return str(PROJECT_ROOT.parent)


@dataclass
class WebappConfig:
    """User-authored, persisted webapp settings."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    # Where the Claude Code tab scans for `.code-workspace` + `*-remote.bat`.
    projects_dir: str = field(default_factory=_default_projects_dir)
    # Where the Apps tab scans recursively for launcher `.bat` files.
    apps_scan_root: str = field(default_factory=_default_projects_dir)
    # Persisted Claude Code launch flag defaults.
    claude_model: str = DEFAULT_CLAUDE_MODEL
    claude_effort: str = DEFAULT_CLAUDE_EFFORT
    claude_verbose: bool = True
    claude_debug: bool = False
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
        apps_scan_root=str(raw.get("apps_scan_root") or _default_projects_dir()),
        claude_model=str(raw.get("claude_model", DEFAULT_CLAUDE_MODEL)),
        claude_effort=str(raw.get("claude_effort", DEFAULT_CLAUDE_EFFORT)),
        claude_verbose=bool(raw.get("claude_verbose", True)),
        claude_debug=bool(raw.get("claude_debug", False)),
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
        "apps_scan_root": cfg.apps_scan_root,
        "claude_model": cfg.claude_model,
        "claude_effort": cfg.claude_effort,
        "claude_verbose": cfg.claude_verbose,
        "claude_debug": cfg.claude_debug,
        "auth_token": cfg.auth_token,
        "auth_password": cfg.auth_password,
        "session_host_port": cfg.session_host_port,
        "tailnet_allowlist": cfg.tailnet_allowlist,
        "claude_show_local_window": cfg.claude_show_local_window,
        "webauthn_rp_id": cfg.webauthn_rp_id,
        "webauthn_rp_name": cfg.webauthn_rp_name,
        "webauthn_origin": cfg.webauthn_origin,
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
    if cfg.claude_model in VALID_CLAUDE_MODELS:
        parts.extend(["--model", cfg.claude_model])
    if cfg.claude_effort in VALID_CLAUDE_EFFORTS and cfg.claude_effort != "off":
        parts.extend(["--effort", cfg.claude_effort])
    if cfg.claude_verbose:
        parts.append("--verbose")
    if cfg.claude_debug:
        parts.append("--debug")
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
    if cfg.claude_effort not in VALID_CLAUDE_EFFORTS:
        raise ValueError(
            f"claude_effort must be one of {VALID_CLAUDE_EFFORTS}; got {cfg.claude_effort!r}"
        )
