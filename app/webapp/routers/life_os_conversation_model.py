"""The model a stored Life OS conversation last ran on.

    GET /api/life-os/skills/{id}/conversations/model?agent=&sid=
                                               → {agent, model}, "" when not known
                                                 (Tailscale + passkey)

The conversation sheet's Model row names it before a resume; the index carries
none, so it is read from the agent's own transcript. Mounted on ``life_os.router``
via ``include_router`` like :mod:`app.webapp.routers.life_os_conversations`.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, Request

from app.webapp.routers.life_os_spawn import _resolve_skill
from src.session_view.conversation_model import recorded_model

router = APIRouter()


@router.get("/api/life-os/skills/{skill_id}/conversations/model")
async def conversation_model(skill_id: str, request: Request, agent: str = "", sid: str = "") -> Dict[str, Any]:
    """``""`` is "not known" (no transcript, or an id that is not a canonical UUID):
    the reader only builds a file name from a validated id."""
    _resolve_skill(request.app.state.webapp_config, skill_id)
    known = agent if agent in ("claude", "codex") else ""
    model = await asyncio.to_thread(recorded_model, known, sid) if known else ""
    return {"agent": known, "model": model}
