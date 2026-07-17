"""``/api/tokens`` — mint / list / revoke scoped API bearer tokens (issue #72).

Backs the Settings tab's "API tokens" panel. The raw token appears
exactly once, in the mint response — only the salted hash is persisted
(see :mod:`src.api_tokens`). Reachable only with full-scope auth: a
job-scoped token is rejected on these paths by the middleware's scope
gate before the routes run.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src import api_tokens
from src.jobs_config import get_by_id, load_jobs
from src.webapp_config import update_webapp_config

from app.webapp.routers._helpers import maybe_json

logger = logging.getLogger(__name__)
router = APIRouter()


def _persist(request: Request, tokens: list) -> None:
    """Write the token list through the config store and refresh app state."""
    new_cfg = update_webapp_config(api_tokens=tokens)
    request.app.state.webapp_config = new_cfg


@router.get("/api/tokens")
async def list_tokens(request: Request) -> Dict[str, Any]:
    cfg = request.app.state.webapp_config
    return {
        "tokens": [t.public_dict() for t in api_tokens.parse_tokens(cfg.api_tokens)]
    }


@router.post("/api/tokens")
async def mint_token(request: Request) -> Dict[str, Any]:
    """Mint a token. Body: ``{label, jobs: [ids]}`` or ``{label, scope: "*"}``.

    The response's ``token`` field is the only time the raw value exists —
    the UI shows it once with a copy button, then it is gone.
    """
    body = await maybe_json(request)
    label = str(body.get("label") or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="label is required")

    scope: Any
    if body.get("scope") == "*":
        scope = "*"
    else:
        raw_jobs = body.get("jobs")
        if not isinstance(raw_jobs, list) or not raw_jobs:
            raise HTTPException(
                status_code=400,
                detail='either scope "*" or a non-empty jobs list is required',
            )
        registry = load_jobs()
        job_ids = []
        for entry in raw_jobs:
            job_id = str(entry or "").strip()
            if not job_id or get_by_id(registry, job_id) is None:
                raise HTTPException(
                    status_code=400, detail=f"unknown job {job_id!r}"
                )
            if job_id not in job_ids:
                job_ids.append(job_id)
        scope = {"jobs": job_ids}

    record, raw = api_tokens.mint_token(label, scope)
    cfg = request.app.state.webapp_config
    tokens = list(cfg.api_tokens or []) + [record]
    _persist(request, tokens)
    logger.info(f"🔑 minted API token {record['id']} ({label!r}, scope={scope!r})")
    public = api_tokens.parse_tokens([record])[0].public_dict()
    return {"token": raw, **public}


@router.delete("/api/tokens/{token_id}")
async def revoke_token(token_id: str, request: Request) -> Dict[str, Any]:
    cfg = request.app.state.webapp_config
    tokens = list(cfg.api_tokens or [])
    remaining = [
        t for t in tokens if not (isinstance(t, dict) and t.get("id") == token_id)
    ]
    if len(remaining) == len(tokens):
        raise HTTPException(status_code=404, detail=f"unknown token {token_id}")
    _persist(request, remaining)
    logger.info(f"🗑️ revoked API token {token_id}")
    return {"revoked": token_id}
