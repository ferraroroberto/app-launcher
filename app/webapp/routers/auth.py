"""Password login → bearer token swap.

The bearer token in `webapp_config.json` is never sent to the client until
a correct password is presented. Failed attempts and successful logins
both land in `webapp/auth.log` (separate from the main launcher log so a
phone-side review is easy).
"""

from __future__ import annotations

import hmac
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from app.webapp.observability import ensure_file_log_handler
from app.webapp.routers._helpers import PROJECT_ROOT, maybe_json
from src.login_lockout import LockoutAlert, LoginLockout, retry_after_header
from src.middleware import trust
from src.notify import Notifier, NotifierError, TelegramConfig, build_notifier
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)
auth_logger = logging.getLogger("launcher.auth")
_AUTH_LOG_PATH = PROJECT_ROOT / "webapp" / "auth.log"


def ensure_log_handler() -> None:
    """Attach the auth.log file handler exactly once. Idempotent — safe to
    call from `create_app()` on every boot."""
    ensure_file_log_handler(_AUTH_LOG_PATH, auth_logger)


router = APIRouter()

APP_NAME = "app-launcher"


def _telegram_notifier(cfg: WebappConfig) -> Optional[Notifier]:
    """The alerts-chat notifier, or None when token or chat id is unset."""
    token = cfg.telegram_bot_token
    chat_id = cfg.telegram_chat_id
    if not (token and chat_id):
        return None
    return build_notifier("telegram", TelegramConfig(token, chat_id))


def _spawn_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


def build_login_lockout(
    get_config: Callable[[], WebappConfig],
    *,
    make_notifier: Callable[[WebappConfig], Optional[Notifier]] = _telegram_notifier,
    spawn: Callable[[Callable[[], None]], None] = _spawn_thread,
    clock: Callable[[], float] = time.monotonic,
) -> LoginLockout:
    """The app's one lock-out, alerting through the vendored notifier.

    The notifier is built per alert from the live config, so a token or chat
    change in Settings applies without a restart; the send runs in a thread
    because ``on_alert`` is called inside the login request.
    """

    def alert_owner(alert: LockoutAlert) -> None:
        notifier = make_notifier(get_config())
        if notifier is None:
            return
        held = f" (+{alert.suppressed} more held back)" if alert.suppressed else ""
        text = (
            f"🚨 {APP_NAME}: {alert.failures} wrong passwords in a row "
            f"from {alert.key}{held}"
        )

        def send() -> None:
            try:
                notifier.send_text(text)
            except NotifierError as exc:
                logger.warning(f"⚠️  login alert not delivered: {type(exc).__name__}")

        spawn(send)

    return LoginLockout(on_alert=alert_owner, clock=clock)


def _client_key(client_host: str, headers: Any) -> str:
    """The address the lock-out counts a caller under.

    A loopback socket carrying the tunnel's headers is a public request that
    reached us over loopback, so every such caller would share one counter
    and one stranger could lock out the owner; the edge's client header names
    the real caller. A non-loopback socket never gets to say who it is.
    """
    if client_host in trust.LOOPBACK_HOSTS and trust.via_cloudflare(headers):
        return (headers.get("cf-connecting-ip") or "").strip() or client_host
    return client_host


@router.post("/api/login")
async def login(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    client_host = request.client.host if request.client else "?"
    lockout: LoginLockout = request.app.state.login_lockout
    exempt = trust.is_pc_itself(client_host, request.headers)
    key = _client_key(client_host, request.headers)
    wait = 0.0 if exempt else lockout.retry_after(key)
    if wait > 0:
        auth_logger.warning(f"🔒 Login refused for {client_host}: locked out")
        raise HTTPException(
            status_code=429,
            detail="too many attempts",
            headers=retry_after_header(wait),
        )
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
        if not exempt:
            lockout.record_failure(key)
        raise HTTPException(status_code=401, detail="bad password")
    lockout.reset(key)
    auth_logger.info(f"🔓 Password login from {client_host}")
    return {"token": cfg.auth_token}
