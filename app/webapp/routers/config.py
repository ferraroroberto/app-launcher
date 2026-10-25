"""Launcher config read/patch + machine status readout.

`/api/status` exposes the tunnel URL, TLS cert presence, and a
terminal-reachability hint so the SPA can explain up front when the
live terminal won't work on the current connection.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from src import boot_autostart
from src.session_view import outbox as session_outbox
from src.agents import AGENTS, DEFAULT_AGENT, is_installed
from src.launching.flags import (
    build_antigravity_flags,
    build_claude_flags,
    build_codex_flags,
    build_copilot_flags,
    build_grok_flags,
    build_pi_flags,
    default_effort,
    effective_effort,
)
from src.launching.model_catalog import (
    CLAUDE_MODEL_SPECS,
    CODEX_MODEL_SPECS,
    GROK_DISPATCH_MODEL_SPECS,
    PI_MODEL_SPECS,
    catalog_payload,
)
from src.usage.providers import VALID_USAGE_PROVIDERS, validate_usage_providers
from src.webapp_config import (
    MAX_CHIEF_AUTO_COMPACT_THRESHOLD,
    MAX_CHIEF_WORKER_CAP,
    MAX_LARGE_UPLOAD_MAX_MB,
    MAX_TERMINAL_HISTORY_LINES,
    MIN_CHIEF_AUTO_COMPACT_THRESHOLD,
    MIN_CHIEF_WORKER_CAP,
    MIN_LARGE_UPLOAD_MAX_MB,
    MIN_TERMINAL_HISTORY_LINES,
    VALID_CODEX_PERMISSION_MODES,
    VALID_GROK_EFFORTS,
    VALID_GROK_PERMISSION_MODES,
    VALID_PI_EFFORTS,
    VALID_PI_TRUST_MODES,
    WebappConfig,
    update_webapp_config,
)

from app.webapp.middleware import terminal_reachability
from app.webapp.routers._helpers import (
    PROJECT_ROOT,
    cert_present,
    claude_flags_payload,
    maybe_json,
)

router = APIRouter()


@router.get("/api/config")
async def get_config(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    return {
        "host": cfg.host,
        "port": cfg.port,
        "projects_dir": cfg.projects_dir,
        "projects_ignore": cfg.projects_ignore,
        # Folders, besides a session's own, an agent may send Chat files from:
        # the text as typed, so Settings shows what the owner wrote.
        "outbox_scratch_roots": cfg.outbox_scratch_roots,
        # Coding-row buttons the user hid — agent ids plus the
        # pseudo-id `github`. The SPA filters the row strip on this.
        "coding_hidden_agents": cfg.coding_hidden_agents,
        # The agent whose launch button stays on a project row;
        # every other visible one is a row in that row's ⋯ menu.
        "coding_favorite_agent": cfg.coding_favorite_agent,
        "coding_model_choice": cfg.coding_model_choice,
        # Models the Projects, Skills and Board dropdowns leave out.
        "hidden_models": cfg.hidden_models,
        # Telegram channel sessions leave the Board and session list.
        "hide_channel_sessions": cfg.hide_channel_sessions,
        # ...and start by themselves when the webapp starts.
        "autostart_channel_sessions": cfg.autostart_channel_sessions,
        "channel_model": cfg.channel_model,
        "apps_scan_root": cfg.apps_scan_root,
        "life_os_dir": cfg.life_os_dir,
        "fleet_config_dir": cfg.fleet_config_dir,
        "terminal_history_lines": cfg.terminal_history_lines,
        "terminal_history_lines_min": MIN_TERMINAL_HISTORY_LINES,
        "terminal_history_lines_max": MAX_TERMINAL_HISTORY_LINES,
        # The compose bar's large-file attach limit, in MB.
        "large_upload_max_mb": cfg.large_upload_max_mb,
        "large_upload_max_mb_min": MIN_LARGE_UPLOAD_MAX_MB,
        "large_upload_max_mb_max": MAX_LARGE_UPLOAD_MAX_MB,
        # Which providers the usage meter shows.
        "usage_providers": list(cfg.usage_providers),
        "usage_providers_available": list(VALID_USAGE_PROVIDERS),
        # Sonnet-tier dispatches run on Grok near Claude's weekly pace; off by default.
        "grok_auto_switch": cfg.grok_auto_switch,
        # The chief's auto-compact threshold: percent, 0 = off.
        "chief_auto_compact_threshold": cfg.chief_auto_compact_threshold,
        "chief_auto_compact_threshold_min": MIN_CHIEF_AUTO_COMPACT_THRESHOLD,
        "chief_auto_compact_threshold_max": MAX_CHIEF_AUTO_COMPACT_THRESHOLD,
        # The chief doorbell, off by default; Settings > Chief's
        # Doorbell switch saves it here.
        "chief_doorbell": cfg.chief_doorbell,
        # Failed Job runs reach the chief mailbox; on by default.
        "chief_job_failures": cfg.chief_job_failures,
        "life_os_refresh_skill_instructions": cfg.life_os_refresh_skill_instructions,
        # The chief's model and worker cap, read-only here so the
        # Settings > Chief row can show them without a passkey prompt. Writes
        # stay on the passkey-gated PUT /api/board/chief/settings.
        "chief_model": cfg.chief_model,
        "chief_worker_cap": cfg.chief_worker_cap,
        "chief_worker_cap_min": MIN_CHIEF_WORKER_CAP,
        "chief_worker_cap_max": MAX_CHIEF_WORKER_CAP,
        "claude": claude_flags_payload(cfg),
        **_agent_payloads(cfg),
        "model_catalog": {
            "claude": catalog_payload(CLAUDE_MODEL_SPECS),
            "codex": catalog_payload(CODEX_MODEL_SPECS),
        },
        # Models only the Board dispatch picker adds to the shared catalog's
        # choices; a provider is empty while its CLI is not on this host.
        "board_dispatch_models": {
            "grok": catalog_payload(GROK_DISPATCH_MODEL_SPECS) if is_installed("grok") else [],
        },
        "auth_password_set": bool(cfg.auth_password),
        # Boot-autostart toggle: live-queried, not stored in
        # webapp_config.json — the Startup-folder wrapper bat's presence
        # *is* the state, so a stale cached bool can never drift from it.
        "boot_autostart_enabled": boot_autostart.is_enabled(),
    }


def _agent_payloads(cfg: WebappConfig) -> Dict[str, Any]:
    """Each non-Claude agent's settings, choices and computed flags."""
    return {
    "codex": {
        "model": cfg.codex_model,
        "effort": cfg.codex_effort,
        "model_efforts": cfg.codex_model_efforts,
        "models_available": catalog_payload(CODEX_MODEL_SPECS),
        "efforts_available": list(CODEX_MODEL_SPECS[cfg.codex_model]["efforts"]),
        "permission_mode": cfg.codex_permission_mode,
        "permission_modes_available": list(VALID_CODEX_PERMISSION_MODES),
        "computed_flags": build_codex_flags(cfg),
    },
    "antigravity": {
        "skip_permissions": cfg.antigravity_skip_permissions,
        "sandbox": cfg.antigravity_sandbox,
        "computed_flags": build_antigravity_flags(cfg),
    },
    # No model/models_available: Copilot's model is not a launcher
    # setting — the CLI resolves it per launch against
    # the account, so only `/model` can report it truthfully.
    "copilot": {
        "skip_permissions": cfg.copilot_skip_permissions,
        "computed_flags": build_copilot_flags(cfg),
    },
    "pi": {
        "model": cfg.pi_model,
        # What the selected model launches at, and the two parts it is made
        # of: the owner's overrides and its catalog default.
        "effort": effective_effort("pi", cfg.pi_model, cfg),
        "model_efforts": cfg.pi_model_efforts,
        "default_effort": default_effort("pi", cfg.pi_model),
        "trust_mode": cfg.pi_trust_mode,
        "models_available": [
            {
                "value": item["value"],
                "label": item["label"],
                "available": item["available"],
                **({"unavailable_reason": item["unavailable_reason"]} if item.get("unavailable_reason") else {}),
            }
            for item in catalog_payload(PI_MODEL_SPECS)
        ],
        "efforts_available": list(VALID_PI_EFFORTS),
        "trust_modes_available": list(VALID_PI_TRUST_MODES),
        "computed_flags": build_pi_flags(cfg),
    },
    "grok": {
        "effort": cfg.grok_effort,
        "permission_mode": cfg.grok_permission_mode,
        "efforts_available": list(VALID_GROK_EFFORTS),
        "permission_modes_available": list(VALID_GROK_PERMISSION_MODES),
        "computed_flags": build_grok_flags(cfg),
    },
    }


def _fold_effort_into_overrides(
    patch: Dict[str, Any], agent: str, selected_model: str, current: Dict[str, str]
) -> None:
    """Rewrite ``patch["<agent>_effort"]`` into ``<agent>_model_efforts``: set
    the override for the co-patched (else selected) model, or clear it on an
    empty value. A no-op when the patch carries no such effort."""
    if f"{agent}_effort" not in patch:
        return
    model = str(patch.get(f"{agent}_model") or selected_model)
    overrides = dict(current)
    effort = patch.pop(f"{agent}_effort")
    if effort in (None, ""):
        overrides.pop(model, None)
    else:
        overrides[model] = str(effort)
    patch[f"{agent}_model_efforts"] = overrides


def _checked_outbox_roots(raw: Any, current: List[str]) -> List[str]:
    """The trimmed send-folder list, or a 400 naming the first entry that
    cannot be one. A stored entry (in ``current``) is forgiven a folder that
    has since gone (the default, before Claude first creates it), so that
    cannot block removing a different one; only a new entry must exist."""
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        raise HTTPException(status_code=400, detail="outbox_scratch_roots must be a list of folders")
    entries = [p.strip() for p in raw if p.strip()]
    if not entries:
        raise HTTPException(
            status_code=400,
            detail="Keep at least one folder: an empty list would bring the default back.",
        )
    for index, entry in enumerate(entries):
        try:
            session_outbox.check_root(
                entry, entries[:index], forgive_missing=entry in current
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    return entries


def _checked_usage_providers(raw: Any) -> List[str]:
    """The written provider list in card order, whatever order the client
    sent; 400 on an unknown name. Checked here rather than in _validate: the
    loader already drops an unknown name on read, so only a write carries one."""
    try:
        validate_usage_providers(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return [p for p in VALID_USAGE_PROVIDERS if p in raw]


# The Settings switches: a truthy string must never flip
# one ("false" is truthy in Python), so patch_config refuses anything but a bool.
_SWITCH_KEYS = (
    "hide_channel_sessions",
    "autostart_channel_sessions",
    "chief_doorbell",
    "chief_job_failures",
    "life_os_refresh_skill_instructions",
    "grok_auto_switch",
)


# The settings POST /api/config may change; any other body key is ignored.
_PATCH_KEYS = frozenset({
    "projects_dir",
    "projects_ignore",
    "outbox_scratch_roots",
    "coding_hidden_agents",
    "coding_favorite_agent",
    "coding_model_choice",
    "hidden_models",
    "hide_channel_sessions",
    "autostart_channel_sessions",
    "channel_model",
    "apps_scan_root",
    "life_os_dir",
    "fleet_config_dir",
    "terminal_history_lines",
    "large_upload_max_mb",
    "usage_providers",
    "grok_auto_switch",
    "chief_auto_compact_threshold",
    "chief_doorbell",
    "chief_job_failures",
    "life_os_refresh_skill_instructions",
    "claude_model",
    "claude_effort",
    "claude_verbose",
    "claude_debug",
    "claude_permission_mode",
    "antigravity_skip_permissions",
    "antigravity_sandbox",
    "codex_model",
    "codex_effort",
    "codex_permission_mode",
    "copilot_skip_permissions",
    "pi_model",
    "pi_effort",
    "pi_trust_mode",
    "grok_effort",
    "grok_permission_mode",
})


@router.post("/api/config")
async def patch_config(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    body = await maybe_json(request)
    patch = {k: v for k, v in body.items() if k in _PATCH_KEYS}
    # projects_ignore is a list of patterns — coerce to a clean string
    # list so a stray scalar from the client can't corrupt the config.
    if "projects_ignore" in patch:
        raw = patch["projects_ignore"]
        patch["projects_ignore"] = [
            str(p).strip() for p in (raw or []) if str(p).strip()
        ]
    # The Chat send folders are checked on write, here, and not in
    # `_validate`: that also runs on load, where a folder deleted since it was
    # saved must not take the whole file down (`allowed_roots` skips it). The
    # text is stored as typed (`%VAR%`/`~` kept); an empty list would read as
    # unset and bring the default back, so at least one folder is required.
    if "outbox_scratch_roots" in patch:
        patch["outbox_scratch_roots"] = _checked_outbox_roots(
            patch["outbox_scratch_roots"], cfg.outbox_scratch_roots
        )
    # Same coercion for the hidden-button and hidden-model lists — a stray
    # scalar must never reach the config as anything but a clean string list.
    for key in ("coding_hidden_agents", "hidden_models"):
        if key in patch:
            patch[key] = [str(p).strip() for p in (patch[key] or []) if str(p).strip()]
    if "usage_providers" in patch:
        patch["usage_providers"] = _checked_usage_providers(patch["usage_providers"])
    # The threshold is a whole percent. A bool is an int in Python
    # and a float would be silently truncated, so both are refused here;
    # the range itself is _validate's, which words the 400.
    if "chief_auto_compact_threshold" in patch:
        value = patch["chief_auto_compact_threshold"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise HTTPException(
                status_code=400,
                detail="chief_auto_compact_threshold must be an integer percent",
            )
    # The large-file limit is whole MB, refused the same way.
    if "large_upload_max_mb" in patch:
        value = patch["large_upload_max_mb"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise HTTPException(
                status_code=400, detail="large_upload_max_mb must be a whole number of MB"
            )
    # The switches: anything but a real boolean is a client bug.
    for key in _SWITCH_KEYS:
        if key in patch and not isinstance(patch[key], bool):
            raise HTTPException(
                status_code=400, detail=f"{key} must be true or false"
            )
    # The favourite agent must name a registered agent. An unknown
    # id — a typo, a stale client, an agent dropped from the registry — is
    # coerced to DEFAULT_AGENT rather than stored, because the row renders
    # exactly one launch button and a dangling id would leave it with none.
    if "coding_favorite_agent" in patch:
        wanted = str(patch["coding_favorite_agent"] or "").strip()
        patch["coding_favorite_agent"] = (
            wanted if wanted in AGENTS else DEFAULT_AGENT
        )
    # Keep the compact Coding choice and the provider-specific defaults in
    # lockstep. Effort changes are remembered against the selected Codex model.
    choice = str(patch.get("coding_model_choice") or "")
    if choice.startswith("claude:"):
        patch["claude_model"] = choice.split(":", 1)[1]
    elif choice.startswith("codex:"):
        patch["codex_model"] = choice.split(":", 1)[1]
    # A `claude_effort` / `pi_effort` POST is the override for the model it
    # lands on (the co-patched model, else the selected one), never a global
    # value; an empty one clears it so the model falls back to its catalog
    # default.
    _fold_effort_into_overrides(patch, "claude", cfg.claude_model, cfg.claude_model_efforts)
    _fold_effort_into_overrides(patch, "pi", cfg.pi_model, cfg.pi_model_efforts)
    if "codex_model" in patch or "codex_effort" in patch:
        model = str(patch.get("codex_model") or cfg.codex_model)
        efforts = dict(cfg.codex_model_efforts)
        if "codex_effort" in patch:
            efforts[model] = str(patch["codex_effort"])
        else:
            patch["codex_effort"] = efforts.get(
                model, CODEX_MODEL_SPECS.get(model, {}).get("default_effort", "")
            )
        patch["codex_model_efforts"] = efforts
    try:
        new_cfg = update_webapp_config(**patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    request.app.state.webapp_config = new_cfg
    return {
        "ok": True,
        "claude_flags": build_claude_flags(new_cfg),
    }


@router.post("/api/settings/boot-autostart")
async def set_boot_autostart(request: Request) -> Dict[str, Any]:
    body = await maybe_json(request)
    enabled = bool(body.get("enabled"))
    try:
        if enabled:
            boot_autostart.enable()
        else:
            boot_autostart.disable()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "boot_autostart_enabled": boot_autostart.is_enabled()}


@router.get("/api/status")
async def status(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    tunnel_file = PROJECT_ROOT / "webapp" / "last_tunnel_url.txt"
    tunnel_url: Optional[str] = None
    if tunnel_file.exists():
        try:
            tunnel_url = tunnel_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            tunnel_url = None
    return {
        "projects_dir": cfg.projects_dir,
        "apps_scan_root": cfg.apps_scan_root,
        "tunnel_url": tunnel_url,
        "tls": cert_present(),
        "terminal": terminal_reachability(request),
        # Compose-bar voice dictation is available only when a
        # voice-transcriber base URL is configured. The SPA
        # hides the 🎤 record button otherwise.
        "voice_dictation": bool((cfg.voice_transcriber_url or "").strip()),
        # Compose-bar screenshot OCR is available only when a photo-ocr
        # base URL is configured. The SPA hides the 📷 OCR
        # button otherwise — the pixel counterpart to voice_dictation.
        "screenshot_ocr": bool((cfg.photo_ocr_url or "").strip()),
        # Read-aloud hub TTS is available only when a local-llm-hub base URL
        # is configured. A cheap config-presence flag — the SPA
        # gates the 🔊 button's hub path on it before doing a live
        # /api/tts/health probe to confirm the hub is actually answering.
        "tts": bool((cfg.llm_hub_url or "").strip()),
    }
