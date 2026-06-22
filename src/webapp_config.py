"""Webapp-specific configuration loader.

Lives separately from `app_config.py` because these settings are
authored from the web UI ("Save defaults" button) and persist across
runs. The CLI also reads this file so both surfaces share one source
of truth.

Holds:
- network knobs (host, port)
- scan roots for Claude-Code projects and Apps
- per-agent launch flags (model, effort, verbose, debug) for all
  registered coding agents (claude, codex, antigravity, copilot, pi)
- sibling-app loopback URLs (voice-transcriber, photo-ocr, local-llm-hub)
- Life OS tab settings
- terminal display and passkey / WebAuthn config
- Pushover failure-notification credentials
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
# Env override for the session-host port. Set ONLY by the e2e pre-ship gate's
# autoboot so a disposable webapp can be pointed at a disposable, free-port
# session-host instead of the live :8446 a running tray owns. This is what
# stops the gate from reaching into — and killing — the user's real PTY
# sessions (issue #260). Not a user-facing knob; intentionally undocumented
# in the config sample.
SESSION_HOST_PORT_ENV = "LAUNCHER_SESSION_HOST_PORT"

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


def load_webapp_config(path: Optional[Path] = None) -> WebappConfig:
    """Load the webapp config, falling back to defaults if the file is missing."""
    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not target.exists():
        logger.info(
            f"📂 webapp_config not found at {target}, using defaults "
            f"(file will be created when settings change)"
        )
        return _apply_session_host_override(WebappConfig())

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            f"⚠️  Could not read {target} ({exc}); falling back to defaults"
        )
        return _apply_session_host_override(WebappConfig())

    cfg = WebappConfig(
        host=str(raw.get("host", DEFAULT_HOST)),
        port=int(raw.get("port", DEFAULT_PORT)),
        projects_dir=str(raw.get("projects_dir") or _default_projects_dir()),
        projects_ignore=[str(p) for p in (raw.get("projects_ignore") or [])],
        coding_favorites=[str(p) for p in (raw.get("coding_favorites") or [])],
        apps_scan_root=str(raw.get("apps_scan_root") or _default_projects_dir()),
        life_os_dir=str(raw.get("life_os_dir") or _default_life_os_dir()),
        claude_config_dir=str(
            raw.get("claude_config_dir") or _default_claude_config_dir()
        ),
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
        codex_effort=str(raw.get("codex_effort", DEFAULT_CODEX_EFFORT)),
        codex_permission_mode=str(
            raw.get("codex_permission_mode", DEFAULT_CODEX_PERMISSION_MODE)
        ),
        copilot_skip_permissions=bool(
            raw.get("copilot_skip_permissions", False)
        ),
        copilot_model=str(raw.get("copilot_model", "")),
        pi_model=str(raw.get("pi_model", DEFAULT_PI_MODEL)),
        pi_effort=str(raw.get("pi_effort", DEFAULT_PI_EFFORT)),
        pi_trust_mode=str(raw.get("pi_trust_mode", DEFAULT_PI_TRUST_MODE)),
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
        voice_transcriber_url=str(
            raw.get("voice_transcriber_url", "https://127.0.0.1:8443")
        ),
        photo_ocr_url=str(
            raw.get("photo_ocr_url", "https://127.0.0.1:8444")
        ),
        llm_hub_url=str(
            raw.get("llm_hub_url", "http://127.0.0.1:8000")
        ),
        pushover_api_token=str(raw.get("pushover_api_token", "")),
        pushover_user_key=str(raw.get("pushover_user_key", "")),
        notify_on_failure=bool(raw.get("notify_on_failure", False)),
        notify_failure_streak=int(raw.get("notify_failure_streak", 0) or 0),
        notify_failure_summary=bool(raw.get("notify_failure_summary", False)),
    )
    _apply_session_host_override(cfg)
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
        "coding_favorites": cfg.coding_favorites,
        "apps_scan_root": cfg.apps_scan_root,
        "life_os_dir": cfg.life_os_dir,
        "claude_config_dir": cfg.claude_config_dir,
        "claude_model": cfg.claude_model,
        "claude_effort": cfg.claude_effort,
        "claude_verbose": cfg.claude_verbose,
        "claude_debug": cfg.claude_debug,
        "claude_permission_mode": cfg.claude_permission_mode,
        "antigravity_skip_permissions": cfg.antigravity_skip_permissions,
        "antigravity_sandbox": cfg.antigravity_sandbox,
        "codex_effort": cfg.codex_effort,
        "codex_permission_mode": cfg.codex_permission_mode,
        "copilot_skip_permissions": cfg.copilot_skip_permissions,
        "copilot_model": cfg.copilot_model,
        "pi_model": cfg.pi_model,
        "pi_effort": cfg.pi_effort,
        "pi_trust_mode": cfg.pi_trust_mode,
        "auth_token": cfg.auth_token,
        "auth_password": cfg.auth_password,
        "session_host_port": cfg.session_host_port,
        "tailnet_allowlist": cfg.tailnet_allowlist,
        "claude_show_local_window": cfg.claude_show_local_window,
        "webauthn_rp_id": cfg.webauthn_rp_id,
        "webauthn_rp_name": cfg.webauthn_rp_name,
        "webauthn_origin": cfg.webauthn_origin,
        "voice_transcriber_url": cfg.voice_transcriber_url,
        "photo_ocr_url": cfg.photo_ocr_url,
        "llm_hub_url": cfg.llm_hub_url,
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


def build_codex_flags(cfg: WebappConfig) -> str:
    """Compose the `codex` CLI flags from the persisted Codex knobs.

    Two pieces: a permission mode (auto = no prompts but sandboxed; skip =
    the all-bypass switch) and a reasoning tier passed through Codex's
    config override. The model is left unset so Codex uses the account
    default (gpt-5-codex on the ChatGPT-plan login). The reasoning value
    is sent bare (``model_reasoning_effort=high``): it isn't valid TOML, so
    Codex's `-c` parser falls back to the raw string — which also dodges
    Windows ``cmd`` quote-stripping that a quoted value would suffer.
    """
    parts: list[str] = []
    if cfg.codex_permission_mode == "skip":
        parts.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        parts.extend(
            ["--ask-for-approval", "never", "--sandbox", "workspace-write"]
        )
    if cfg.codex_effort in VALID_CODEX_EFFORTS:
        parts.extend(["-c", f"model_reasoning_effort={cfg.codex_effort}"])
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

    - **provider + model** — looked up from :data:`PI_MODEL_SPECS` for the
      chosen option. Opus/Sonnet route to the ``claude-agent-sdk`` provider
      (the Claude **subscription** quota, no API "extra usage" credits); GPT
      routes to ``openai-codex`` (the ChatGPT-plan subscription). ``pi_model``
      is never empty — an unknown value falls back to ``DEFAULT_PI_MODEL`` so
      the launch can't slip onto a billing path (pi's native ``anthropic``
      provider is deliberately bypassed, and disconnected on this machine).
    - **thinking** — ``--thinking <effort>`` from ``pi_effort`` (default high).
    - **trust** — ``--approve`` (trust mode) or ``--no-approve`` (ask mode).
      This is project *trust* (loading project-local ``.pi/`` resources), NOT
      a tool-permission gate: pi has no tool sandbox or per-action prompt.

    In-session switching stays available via ``/model`` / ``Ctrl+L`` /
    ``Shift+Tab``. See docs/pi-coding-agent.md.
    """
    model = cfg.pi_model if cfg.pi_model in VALID_PI_MODELS else DEFAULT_PI_MODEL
    provider, model_arg, _label = PI_MODEL_SPECS[model]
    parts = ["--provider", provider, "--model", model_arg]
    effort = cfg.pi_effort if cfg.pi_effort in VALID_PI_EFFORTS else DEFAULT_PI_EFFORT
    parts.extend(["--thinking", effort])
    parts.append("--approve" if cfg.pi_trust_mode == "trust" else "--no-approve")
    return " ".join(parts)


def build_resume_flags(
    cfg: WebappConfig, agent_id: str, model_override: Optional[str] = None
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

    ``model_override`` forces a specific Claude ``--model`` (used by the
    Life OS tab's opus toggle); it is ignored for non-Claude agents, which
    have no launch-time model flag.
    """
    from src.agents import resume_command_for

    token = resume_command_for(agent_id)
    if agent_id == "codex":
        parts = [token]
        if cfg.codex_effort in VALID_CODEX_EFFORTS:
            parts.extend(["-c", f"model_reasoning_effort={cfg.codex_effort}"])
        return " ".join(parts).strip()
    if agent_id == "claude":
        base = build_claude_flags(cfg, model_override=model_override)
    elif agent_id == "antigravity":
        base = build_antigravity_flags(cfg)
    elif agent_id == "copilot":
        base = build_copilot_flags(cfg)
    elif agent_id == "pi":  # keep the SDK provider/model on resume (issue #273)
        base = build_pi_flags(cfg)
    else:
        base = ""
    return f"{token} {base}".strip()


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
    if cfg.codex_effort not in VALID_CODEX_EFFORTS:
        raise ValueError(
            f"codex_effort must be one of {VALID_CODEX_EFFORTS}; got {cfg.codex_effort!r}"
        )
    if cfg.codex_permission_mode not in VALID_CODEX_PERMISSION_MODES:
        raise ValueError(
            f"codex_permission_mode must be one of {VALID_CODEX_PERMISSION_MODES}; "
            f"got {cfg.codex_permission_mode!r}"
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
