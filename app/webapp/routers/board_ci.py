"""Red CI cards: the Board's dismiss for a workflow run the owner has dealt with.

    POST /api/board/ci/dismiss → record a red run so its card leaves the Board

The cards themselves ride ``GET /api/board`` (built by
:func:`src.board.ci_runs.board_cards`). Mounted on ``board.router`` like
:mod:`app.webapp.routers.board_owner`, so ``server.py`` still registers one
Board router.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from app.webapp.routers._helpers import maybe_json
from src.board import ci_dismissed, ci_runs

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/board/ci/dismiss")
async def post_ci_dismiss(request: Request) -> Dict[str, Any]:
    """Mark a red CI run as dealt with, so its card leaves the Board.

    Body: ``{"repo": str, "workflow": str, "run_id": int}``. Only the red run a
    card shows now is dismissible; any other (a run since replaced, a green
    workflow, an unreadable repo) answers 409. The workflow's next red run
    raises a fresh card.
    """
    body = await maybe_json(request)
    repo, workflow, run_id = body.get("repo"), body.get("workflow"), body.get("run_id")
    if (
        not isinstance(repo, str) or not isinstance(workflow, str)
        or not isinstance(run_id, int) or isinstance(run_id, bool)
    ):
        raise HTTPException(
            status_code=400, detail="repo and workflow must be strings and run_id an integer"
        )
    if not ci_runs.is_red_run(ci_runs.snapshot(), repo, workflow, run_id):
        raise HTTPException(status_code=409, detail="that run is not a current red CI run")
    try:
        await asyncio.to_thread(ci_dismissed.dismiss, repo, workflow, run_id)
    except OSError as exc:
        logger.warning("⚠️ CI alert dismissal not written: %s", exc)
        raise HTTPException(status_code=500, detail="dismissal not written")
    return {"dismissed": True, "repo": repo, "workflow": workflow, "run_id": run_id}
