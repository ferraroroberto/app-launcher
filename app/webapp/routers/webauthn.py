"""Passkey enrollment + authentication for the live terminal gate.

The enrollment window can only be opened from the PC (loopback) — opening
it deliberately from the tray menu is what makes adding a new device a
conscious act. Begin/finish ceremonies are Tailscale-gated by the
terminal_http_gate middleware.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src import audit
from src.webapp_config import WebappConfig
from src.webauthn_gate import WebAuthnGate

from app.webapp.middleware import LOOPBACK_HOSTS
from app.webapp.routers._helpers import client_ip, maybe_json

router = APIRouter()


@router.get("/api/webauthn/status")
async def webauthn_status(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    gate: WebAuthnGate = request.app.state.webauthn_gate
    return {
        "configured": WebAuthnGate.configured(cfg),
        "rp_id": cfg.webauthn_rp_id,
        "enrollment_open": gate.enrollment_open(),
        "enrollment_seconds_left": gate.enrollment_seconds_left(),
        "devices": gate.list_devices(),
    }


@router.post("/api/webauthn/enroll/window")
async def webauthn_open_window(request: Request) -> Dict[str, Any]:
    """Open the one-time passkey enrollment window. PC-only (loopback).

    Called by the tray menu item — opening it deliberately from the PC
    is what makes adding a new device a conscious act.
    """
    client_host = request.client.host if request.client else ""
    if client_host not in LOOPBACK_HOSTS:
        raise HTTPException(
            status_code=403,
            detail="the enrollment window can only be opened from the PC",
        )
    gate: WebAuthnGate = request.app.state.webauthn_gate
    body = await maybe_json(request)
    seconds = min(max(float(body.get("seconds") or 300), 30.0), 900.0)
    gate.open_enrollment_window(seconds)
    audit.audit_event("enroll_window_opened", seconds=int(seconds))
    return {
        "enrollment_open": True,
        "seconds": gate.enrollment_seconds_left(),
    }


@router.post("/api/webauthn/enroll/begin")
async def webauthn_enroll_begin(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    gate: WebAuthnGate = request.app.state.webauthn_gate
    if not WebAuthnGate.configured(cfg):
        raise HTTPException(status_code=503, detail="webauthn not configured")
    body = await maybe_json(request)
    label = str(body.get("label") or "device").strip()[:60] or "device"
    try:
        options = gate.begin_registration(cfg, label)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    audit.audit_event(
        "enroll_begin", label=label, client=client_ip(request)
    )
    return options


@router.post("/api/webauthn/enroll/finish")
async def webauthn_enroll_finish(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    gate: WebAuthnGate = request.app.state.webauthn_gate
    credential = await maybe_json(request)
    try:
        result = gate.finish_registration(cfg, credential)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — verification failure
        audit.audit_event(
            "enroll_fail", error=str(exc), client=client_ip(request)
        )
        raise HTTPException(
            status_code=400, detail=f"registration failed: {exc}"
        )
    audit.audit_event(
        "enroll_ok",
        device=result.get("id"),
        label=result.get("label"),
        client=client_ip(request),
    )
    return result


@router.post("/api/webauthn/auth/begin")
async def webauthn_auth_begin(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    gate: WebAuthnGate = request.app.state.webauthn_gate
    if not WebAuthnGate.configured(cfg):
        raise HTTPException(status_code=503, detail="webauthn not configured")
    try:
        return gate.begin_authentication(cfg)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post("/api/webauthn/auth/finish")
async def webauthn_auth_finish(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    gate: WebAuthnGate = request.app.state.webauthn_gate
    credential = await maybe_json(request)
    try:
        token = gate.finish_authentication(cfg, credential)
    except PermissionError as exc:
        audit.audit_event(
            "auth_fail", error=str(exc), client=client_ip(request)
        )
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — verification failure
        audit.audit_event(
            "auth_fail", error=str(exc), client=client_ip(request)
        )
        raise HTTPException(
            status_code=400, detail=f"authentication failed: {exc}"
        )
    audit.audit_event("auth_ok", client=client_ip(request))
    return {"terminal_token": token, "ttl_seconds": 12 * 3600}


@router.delete("/api/webauthn/devices/{device_id}")
async def webauthn_remove_device(
    device_id: str, request: Request
) -> Dict[str, Any]:
    gate: WebAuthnGate = request.app.state.webauthn_gate
    if not gate.remove_device(device_id):
        raise HTTPException(status_code=404, detail="unknown device")
    audit.audit_event(
        "device_removed", device=device_id, client=client_ip(request)
    )
    return {"removed": device_id}
