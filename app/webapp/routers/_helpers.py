"""Cross-router helpers — no router imports another router; shared utility
lives here instead.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlencode

from fastapi import Request, WebSocket

from src.webapp_config import WebappConfig, append_auth_token
from src.webauthn_gate import WebAuthnGate

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


# (mtime, hostname-or-None) of the last classified cert.pem — the cert only
# changes on provision/renew, so one parse per change, not per PTY launch.
_TSNET_CACHE: Optional[tuple[float, Optional[str]]] = None


def tsnet_host_from_cert(cert_path: Optional[Path] = None) -> Optional[str]:
    """The .ts.net hostname the active cert is issued for, or None.

    Returns a hostname only for a genuine ``tailscale cert`` leaf — keyed on
    the ISSUER (Let's Encrypt), because a legacy self-signed leaf (the
    retired gen_ssl_cert.py) also carried the ts.net name in its SAN and
    must keep routing mirrors over loopback (same discriminator as
    scripts/gen_tailscale_cert.py, issue #354).
    """
    global _TSNET_CACHE
    path = cert_path or (PROJECT_ROOT / "webapp" / "certificates" / "cert.pem")
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if cert_path is None and _TSNET_CACHE is not None and _TSNET_CACHE[0] == mtime:
        return _TSNET_CACHE[1]
    host: Optional[str] = None
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID

        cert = x509.load_pem_x509_certificate(path.read_bytes())
        issuer_orgs = [
            attr.value
            for attr in cert.issuer.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
        ]
        if any("let's encrypt" in str(org).lower() for org in issuer_orgs):
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            for name in san.value.get_values_for_type(x509.DNSName):
                if ".ts.net" in name:
                    host = name
                    break
    except Exception:
        host = None
    if cert_path is None:
        _TSNET_CACHE = (mtime, host)
    return host


def mirror_url(request: Request, cfg: WebappConfig, sid: str) -> str:
    """The URL a launcher-spawned PC terminal window opens for ``sid``.

    Self-signed / no cert → loopback, whose auth bypass the mirror rides
    (issue #20/#241). With a Tailscale LE cert the loopback URL would hit a
    hostname-mismatch interstitial (the cert names only the ts.net host), so
    the mirror targets the ts.net URL instead and carries its credentials
    explicitly: ``?token=`` bootstraps the bearer (same mechanism as the
    tunnel URL) and ``?tt=`` a server-minted terminal token when the passkey
    gate is configured. Trust-equivalent to the loopback bypass — the window
    is spawned by this server, on this machine, for this user (issue #356);
    the SPA strips both params from the visible URL on boot.
    """
    ts_host = tsnet_host_from_cert()
    if ts_host is None:
        scheme = "https" if cert_present() else "http"
        return f"{scheme}://127.0.0.1:{cfg.port}/?terminal={sid}"
    url = append_auth_token(
        f"https://{ts_host}:{cfg.port}/?terminal={sid}", cfg.auth_token
    )
    gate = getattr(request.app.state, "webauthn_gate", None)
    if gate is not None and WebAuthnGate.configured(cfg):
        url += "&" + urlencode({"tt": gate.mint_local_token()})
    return url


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
    # in-page and skips it. mirror_url picks loopback (auth-bypass) or the
    # ts.net URL with explicit credentials, keyed on the active cert (#356).
    if should_mirror_to_pc(cfg.claude_show_local_window, request, body):
        # Pass sid so launcher tracks the mirror window's HWND for Stop &
        # Close to dismiss it later (issue #20).
        asyncio.create_task(
            asyncio.to_thread(mirror_fn, mirror_url(request, cfg, sid), sid)
        )


def client_ip_ws(websocket: WebSocket) -> str:
    return websocket.client.host if websocket.client else "?"
