"""Owner checks: the Board's answers to work waiting on the owner.

    POST /api/board/owner-checks/answer → apply the answer sheet's owner-check
                                          answers (Tailscale + passkey)

The list itself rides ``GET /api/board`` as its ``owner_checks`` section, read
from :mod:`src.board.owner_checks`'s cache; ``POST /api/board/github/refresh``
starts that cache's background refresh. Mounted on ``board.router`` like
:mod:`app.webapp.routers.board_chief`, so ``server.py`` still registers one
Board router.

Writes reach GitHub with the owner's own ``gh`` account: Validated and Done
through fleet-config's ``owner_check clear``, a Problem as a plain comment.
Each answer is applied on its own and reported on its own, so one failed write
never hides the ones that landed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from src.board import owner_checks
from src.clients import github as github_client
from src.webapp_config import WebappConfig

from app.webapp.routers._helpers import maybe_json

logger = logging.getLogger(__name__)
router = APIRouter()

# The sheet holds a few owner checks; this bounds one request's GitHub writes.
_MAX_ANSWERS = 30


def _apply(cfg: WebappConfig, ans: Dict[str, Any]) -> Dict[str, Any]:
    def comment(repo: str, number: int, body: str) -> None:
        github_client.comment_issue(cfg.github_owner, repo, number, body)

    try:
        owner_checks.answer(cfg.fleet_config_dir, ans, comment=comment)
    except (owner_checks.OwnerCheckError, github_client.GhError, OSError) as exc:
        logger.warning("⚠️ owner check %s: %s not applied: %s", ans["ref"], ans["answer"], exc)
        return {"ref": ans["ref"], "answer": ans["answer"], "ok": False, "error": str(exc)}
    return {"ref": ans["ref"], "answer": ans["answer"], "ok": True}


@router.post("/api/board/owner-checks/answer")
async def post_owner_answers(request: Request) -> Dict[str, Any]:
    """Body ``{"answers": [{ref, kind, answer, text?}]}``; one result per answer.

    ``answer`` is ``validated`` / ``problem`` / ``later`` for a validate-after
    item and ``done`` / ``later`` for a before-start one; a problem carries its
    ``text``. A malformed answer fails the whole request (400) before any write.
    """
    body = await maybe_json(request)
    raw = body.get("answers")
    if not isinstance(raw, list) or not raw or len(raw) > _MAX_ANSWERS:
        raise HTTPException(status_code=400, detail=f"answers must be a list of 1 to {_MAX_ANSWERS}")
    try:
        answers = [owner_checks.validate_answer(a) for a in raw]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cfg: WebappConfig = request.app.state.webapp_config
    results: List[Dict[str, Any]] = []
    for ans in answers:
        results.append(await asyncio.to_thread(_apply, cfg, ans))
    return {"results": results, "owner_checks": owner_checks.snapshot()}
