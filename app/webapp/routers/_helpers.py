"""Cross-router helpers — no router imports another router; shared utility
lives here instead.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import Request, WebSocket

from src.webapp_config import WebappConfig

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


async def maybe_json(request: Request) -> Dict[str, Any]:
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def cert_present() -> bool:
    return (
        (PROJECT_ROOT / "webapp" / "certificates" / "cert.pem").exists()
        and (PROJECT_ROOT / "webapp" / "certificates" / "key.pem").exists()
    )


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def should_mirror_to_pc(
    show_local_window: bool, request: Request, body: Dict[str, Any]
) -> bool:
    """Whether a PTY launch should open the PC mirror window (issue #20).

    Both the phone and a desktop browser get a dedicated Edge ``--app``
    window on the PC (issue #241):

    * **Phone** — non-loopback and no ``desktop`` flag: the PC has no window
      of its own, so mirror to one.
    * **Desktop browser** — ``desktop: true`` in the launch body: mirror to a
      dedicated, independently-closable Edge window rather than rendering the
      terminal inside the user's own browser. This reverses issue #159's
      desktop-skips-mirror optimization for the PTY case — the "redundant"
      in-page render was the very thing that let Stop & Close tear down the
      controlling Chrome window, so the dedicated window is the fix, not the
      redundancy. The flag (set client-side by ``isDesktopClient``) is what
      distinguishes a desktop from a phone regardless of loopback vs tunnel.

    A non-``desktop`` loopback launch (the rare PC client that reports a
    coarse pointer) still skips the mirror and renders in-page — harmless now
    that an in-page loopback terminal is no longer mis-treated as a mirror.
    """
    # Imported here to avoid a module-load cycle (middleware imports nothing
    # from the routers package, but keep the dependency edge one-directional).
    from app.webapp.middleware import LOOPBACK_HOSTS

    if not show_local_window:
        return False
    return bool(body.get("desktop")) or client_ip(request) not in LOOPBACK_HOSTS


async def audit_session_start_and_maybe_mirror(
    cfg: WebappConfig,
    request: Request,
    body: Dict[str, Any],
    *,
    sid: str,
    agent: str,
    name: str,
    project: str,
    audit_mod: Any,
    mirror_fn: Callable[[str, str], Any],
    resume: Optional[bool] = None,
    skill: Optional[str] = None,
) -> None:
    """Audit a freshly spawned PTY session, then mirror it to a PC terminal
    window if appropriate (issue #241) — the shared tail every PTY-launch
    call site (Coding tab ``apps.py``, Board issue-start/dispatch
    ``board.py``) needs right after ``spawn_claude_session`` (issue #334).

    Life OS (``routers/life_os.py``) already has its own
    ``_spawn_skill_session`` covering this same tail plus the "remote" kind
    and response-shaping, so it isn't routed through here — this helper only
    dedupes the three PTY call sites that don't have an equivalent.

    ``audit_mod`` / ``mirror_fn`` must be the *caller's own* module-level
    ``audit`` / ``open_local_terminal_window`` references (not this module's)
    so ``tests/conftest.py``'s per-router monkeypatches — which stub the
    audit writer and stub the mirror spawn to keep unit tests from spawning
    real windows or writing real audit logs — still take effect when this
    helper runs on the caller's behalf.
    """
    audit_mod.audit_event(
        "session_start",
        session=sid,
        agent=agent,
        skill=skill,
        name=name,
        project=project,
        resume=resume,
        client=client_ip(request),
    )
    audit_mod.session_log(
        sid, "start", agent=agent, skill=skill, name=name, project=project,
    )
    # Mirror the session into a dedicated interactive terminal window on the
    # PC for both phone and desktop-browser launches (issue #241 — see
    # should_mirror_to_pc); only a non-desktop loopback launch renders
    # in-page and skips it. The PC window connects over loopback, bypassing
    # the Tailscale + passkey gate.
    if should_mirror_to_pc(cfg.claude_show_local_window, request, body):
        scheme = "https" if cert_present() else "http"
        pc_url = f"{scheme}://127.0.0.1:{cfg.port}/?terminal={sid}"
        # Pass sid so launcher tracks the mirror window's HWND for Stop &
        # Close to dismiss it later (issue #20).
        asyncio.create_task(asyncio.to_thread(mirror_fn, pc_url, sid))


def client_ip_ws(websocket: WebSocket) -> str:
    return websocket.client.host if websocket.client else "?"
