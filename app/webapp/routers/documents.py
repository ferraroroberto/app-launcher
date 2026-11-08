"""Settings > Documents: the fleet's sources of truth, read-only.

``GET /api/documents``       -> ``{documents: [{id, title, repo, updated}]}``
``GET /api/documents/{id}``  -> one document's text (Tailscale + passkey)

The browser never sends a path: it names an ``id`` from the ``documents``
allowlist in the webapp config, and ``src.documents`` turns that into a file
under one repo's primary checkout. The list carries titles and times only and
stays token-gated like the Life OS skills list; the text itself is gated like
the Life OS file reader (see ``src/middleware/routes.py``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from app.webapp.routers._helpers import audit_off_loop, client_ip
from src import audit, documents
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)
router = APIRouter()

# Which HTTP status each way a read can fail maps to; the detail text is the
# phone's own wording for the condition.
_STATUS_BY_KIND = {
    "not_found": 404,
    "outside": 403,
    "too_large": 413,
    "unreadable": 500,
}


@router.get("/api/documents")
async def list_documents(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    allowed = documents.parse_documents(cfg.documents)
    rows = await asyncio.to_thread(documents.list_documents, cfg.projects_dir, allowed)
    return {"documents": rows}


@router.get("/api/documents/{doc_id}")
async def get_document(doc_id: str, request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    doc = documents.find_document(documents.parse_documents(cfg.documents), doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="no such document")
    try:
        body = await asyncio.to_thread(documents.read_document, cfg.projects_dir, doc)
    except documents.DocumentError as exc:
        logger.warning("⚠️  document %s not served (%s): %s", doc.id, exc.kind, exc.detail)
        raise HTTPException(status_code=_STATUS_BY_KIND[exc.kind], detail=exc.detail)
    await audit_off_loop(
        audit.audit_event, "document_read", id=doc.id, bytes=body["bytes"], client=client_ip(request)
    )
    return body
