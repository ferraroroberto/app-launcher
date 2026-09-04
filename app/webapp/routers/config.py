"""Launcher config read/patch + machine status readout.

`/api/status` exposes the tunnel URL, TLS cert presence, and a
terminal-reachability hint so the SPA can explain up front when the
live terminal won't work on the current connection.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from src import boot_autostart
from src.launch_flags import (
    build_antigravity_flags,
    build_claude_flags,
    build_codex_flags,
    build_copilot_flags,
    build_grok_flags,
    build_pi_flags,
)
from src.model_catalog import (
    CLAUDE_MODEL_SPECS,
    CODEX_MODEL_SPECS,
    PI_MODEL_SPECS,
    catalog_payload,
)
from src.webapp_config import (
    MAX_TERMINAL_HISTORY_LINES,
    MIN_TERMINAL_HISTORY_LINES,
    VALID_CODEX_PERMISSION_MODES,
    VALID_COPILOT_MODELS,
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
        # Coding-row buttons the user hid (issue #666) — agent ids plus the
        # pseudo-id `github`. The SPA filters the row strip on this.
        "coding_hidden_agents": cfg.coding_hidden_agents,
        "coding_model_choice": cfg.coding_model_choice,
        "apps_scan_root": cfg.apps_scan_root,
        "life_os_dir": cfg.life_os_dir,
        "claude_config_dir": cfg.claude_config_dir,
        "terminal_history_lines": cfg.terminal_history_lines,
        "terminal_history_lines_min": MIN_TERMINAL_HISTORY_LINES,
        "terminal_history_lines_max": MAX_TERMINAL_HISTORY_LINES,
        "claude": claude_flags_payload(cfg),
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
        "copilot": {
            "skip_permissions": cfg.copilot_skip_permissions,
            "model": cfg.copilot_model,
            "models_available": list(VALID_COPILOT_MODELS),
            "computed_flags": build_copilot_flags(cfg),
        },
        "pi": {
            "model": cfg.pi_model,
            "effort": cfg.pi_effort,
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
        "model_catalog": {
            "claude": catalog_payload(CLAUDE_MODEL_SPECS),
            "codex": catalog_payload(CODEX_MODEL_SPECS),
        },
        "auth_password_set": bool(cfg.auth_password),
        # Boot-autostart toggle (issue #456): live-queried, not stored in
        # webapp_config.json — the Startup-folder wrapper bat's presence
        # *is* the state, so a stale cached bool can never drift from it.
        "boot_autostart_enabled": boot_autostart.is_enabled(),
    }


@router.post("/api/config")
async def patch_config(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    body = await maybe_json(request)
    allowed = {
        "projects_dir",
        "projects_ignore",
        "coding_hidden_agents",
        "coding_model_choice",
        "apps_scan_root",
        "life_os_dir",
        "claude_config_dir",
        "terminal_history_lines",
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
        "copilot_model",
        "pi_model",
        "pi_effort",
        "pi_trust_mode",
        "grok_effort",
        "grok_permission_mode",
    }
    patch = {k: v for k, v in body.items() if k in allowed}
    # projects_ignore is a list of patterns — coerce to a clean string
    # list so a stray scalar from the client can't corrupt the config.
    if "projects_ignore" in patch:
        raw = patch["projects_ignore"]
        patch["projects_ignore"] = [
            str(p).strip() for p in (raw or []) if str(p).strip()
        ]
    # Same coercion for the hidden-button list (issue #666) — a stray scalar
    # must never reach the config as anything but a clean string list.
    if "coding_hidden_agents" in patch:
        raw = patch["coding_hidden_agents"]
        patch["coding_hidden_agents"] = [
            str(p).strip() for p in (raw or []) if str(p).strip()
        ]
    # Keep the compact Coding choice and the provider-specific defaults in
    # lockstep. Effort changes are remembered against the selected Codex model.
    choice = str(patch.get("coding_model_choice") or "")
    if choice.startswith("claude:"):
        patch["claude_model"] = choice.split(":", 1)[1]
    elif choice.startswith("codex:"):
        patch["codex_model"] = choice.split(":", 1)[1]
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
        # voice-transcriber base URL is configured (issue #165). The SPA
        # hides the 🎤 record button otherwise.
        "voice_dictation": bool((cfg.voice_transcriber_url or "").strip()),
        # Compose-bar screenshot OCR is available only when a photo-ocr
        # base URL is configured (issue #171). The SPA hides the 📷 OCR
        # button otherwise — the pixel counterpart to voice_dictation.
        "screenshot_ocr": bool((cfg.photo_ocr_url or "").strip()),
        # Read-aloud hub TTS is available only when a local-llm-hub base URL
        # is configured (issue #203). A cheap config-presence flag — the SPA
        # gates the 🔊 button's hub path on it before doing a live
        # /api/tts/health probe to confirm the hub is actually answering.
        "tts": bool((cfg.llm_hub_url or "").strip()),
    }
