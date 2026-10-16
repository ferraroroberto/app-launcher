"""Password login → bearer token swap.

The bearer token in `webapp_config.json` is never sent to the client until
a correct password is presented. Failed attempts and successful logins
both land in `webapp/auth.log` (separate from the main launcher log so a
phone-side review is easy).
"""

from __future__ import annotations

import hmac
import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src.webapp_config import WebappConfig

from app.webapp.observability import ensure_file_log_handler
from app.webapp.routers._helpers import PROJECT_ROOT, maybe_json

logger = logging.getLogger(__name__)
auth_logger = logging.getLogger("launcher.auth")
_AUTH_LOG_PATH = PROJECT_ROOT / "webapp" / "auth.log"


def ensure_log_handler() -> None:
    """Attach the auth.log file handler exactly once. Idempotent — safe to
    call from `create_app()` on every boot."""
    ensure_file_log_handler(_AUTH_LOG_PATH, auth_logger)


router = APIRouter()


@router.post("/api/login")
async def login(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    client_host = request.client.host if request.client else "?"
    if not cfg.auth_password:
        auth_logger.info(
            f"⚠️  Login attempt from {client_host} but no auth_password configured"
        )
        raise HTTPException(
            status_code=503, detail="password auth not configured"
        )
    if not cfg.auth_token:
        auth_logger.info(
            f"⚠️  Login attempt from {client_host} but no auth_token configured"
        )
        raise HTTPException(
            status_code=503, detail="bearer token not configured"
        )
    body = await maybe_json(request)
    presented = str(body.get("password") or "")
    if not presented or not hmac.compare_digest(presented, cfg.auth_password):
        auth_logger.warning(
            f"🚨 Failed password attempt from {client_host} "
            f"(presented: {len(presented)} chars)"
        )
        raise HTTPException(status_code=401, detail="bad password")
    auth_logger.info(f"🔓 Password login from {client_host}")
    return {"token": cfg.auth_token}
