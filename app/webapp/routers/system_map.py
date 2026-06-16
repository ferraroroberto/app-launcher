"""Fleet system map — serve the claude-config architecture PNG (issue #173).

The fleet system map (``<claude_config_dir>/architecture/system-map.png``,
rendered by claude-config's ``/system-map`` job) is surfaced as a foldable
section on the Coding tab — "see my whole system" one tap from the phone,
any time, instead of waiting for the weekly Slack image post.

    GET /api/system-map/status   → {available, claude_config_dir} (token-gated)
    GET /api/system-map/image    → the PNG bytes (token + Tailscale-only)

The image endpoint is gated like the live terminal **minus** the passkey:
bearer-token AND Tailscale-only (refused over the Cloudflare tunnel) — see
``_terminal_guard_level`` in ``app/webapp/middleware.py``. The status probe
stays token-only so the SPA can decide the section's visibility even over the
public tunnel (mirrors ``/api/tts/health``). The served path is a single fixed
file under the configured checkout — no user input reaches the filesystem, so
no path-jail is needed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)
router = APIRouter()

# Location of the rendered map inside a claude-config checkout.
_MAP_REL = "architecture/system-map.png"


def _map_path(cfg: WebappConfig) -> Path:
    """Absolute path to the system-map PNG for the configured checkout."""
    return Path(cfg.claude_config_dir) / _MAP_REL


@router.get("/api/system-map/status")
async def system_map_status(request: Request) -> Dict[str, Any]:
    """Whether the fleet system map is available (public, token-gated).

    ``available`` is ``True`` only when the rendered PNG exists under
    ``claude_config_dir``; the SPA hides the section otherwise, the same way
    the Life OS tab hides when life-os isn't checked out. Stays token-only
    (not Tailscale-gated) so the section's visibility can be decided even over
    the Cloudflare tunnel — the image fetch itself is the Tailscale-gated part.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    available = _map_path(cfg).is_file()
    return {
        "available": available,
        "claude_config_dir": cfg.claude_config_dir,
    }


@router.get("/api/system-map/image")
async def system_map_image(request: Request) -> FileResponse:
    """Return the fleet system-map PNG (token + Tailscale-only, no passkey).

    Gated upstream in the middleware (``_terminal_guard_level`` → ``"tailnet"``):
    refused over the Cloudflare tunnel, reachable only over Tailscale/loopback,
    still bearer-token gated. Served ``no-cache`` so a fresh ``/system-map`` run
    shows on the next open without relying on a hashed-URL cache bust.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    path = _map_path(cfg)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="system map not found")
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": "no-cache"},
    )
