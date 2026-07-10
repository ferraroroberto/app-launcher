"""``POST /api/jobs/{job_id}/hook`` — webhook trigger (issue #73).

Split off ``app/webapp/routers/jobs.py`` (a single-file god-router
candidate flagged by ``/codebase-audit``). Mounted into the parent
``jobs.router`` via ``include_router`` so ``app/webapp/server.py``
keeps registering a single ``jobs.router`` unchanged.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from src import jobs as jobs_mod
from src.jobs_argv import compose_argv
from src.jobs_config import get_by_id, load_jobs
from src.jobs_webhook import (
    event_allowed,
    resolve_mapping,
    resolve_secret,
    verify_webhook,
)

from app.webapp.routers.jobs_run import _admit_and_spawn

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/jobs/{job_id}/hook")
async def run_job_webhook(job_id: str, request: Request) -> Response:
    """Fire a job from an external service (issue #73).

    Authenticated by the job's own ``webhook.provider`` signature — never
    the bearer token (see ``app.webapp.middleware._is_webhook_hook_path``),
    so this URL is safe to hand to a third party in plaintext. Anything
    that fails signature/shape verification writes **no run record** — only
    a verified fire ever touches disk.
    """
    cfg = load_jobs()
    job = get_by_id(cfg, job_id)
    if job is None or job.webhook is None:
        raise HTTPException(
            status_code=404, detail="unknown job or no webhook configured"
        )

    body = await request.body()
    try:
        secret = resolve_secret(job.webhook.secret, request.app.state.webapp_config)
    except ValueError:
        logger.warning(f"⚠️  webhook {job_id}: secret reference did not resolve")
        raise HTTPException(status_code=401, detail="invalid signature")

    headers = {k.lower(): v for k, v in request.headers.items()}
    if not verify_webhook(job.webhook, secret, body, headers):
        raise HTTPException(status_code=401, detail="invalid signature")

    event_header = headers.get("x-github-event")
    if not event_allowed(job.webhook, event_header):
        return Response(status_code=204)

    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        payload = {}

    mapped = resolve_mapping(payload, job.webhook.mapping)
    try:
        compose_argv(job, mapped)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    def _persist_webhook_payload(run_dir: Any) -> None:
        jobs_mod.write_webhook_payload(
            run_dir,
            provider=job.webhook.provider,
            event=event_header,
            headers=headers,
            payload=payload,
        )

    result = await _admit_and_spawn(
        job,
        cfg,
        mapped,
        "webhook",
        extra_run_meta={"trigger_source": f"webhook:{job.webhook.provider}"},
        on_run_dir=_persist_webhook_payload,
    )
    return JSONResponse(content=result)
