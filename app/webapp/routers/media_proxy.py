"""Voice-dictation, screenshot-OCR, and hub read-aloud proxy endpoints.

Split off ``app/webapp/routers/sessions.py`` (a single-file god-router
candidate flagged by ``/codebase-audit``) — mounted into the parent
``sessions.router`` via ``include_router`` so ``app/webapp/server.py``
still registers one ``sessions.router``, the same pattern
``app.webapp.routers.jobs`` uses for its own webhook/run-store split.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

import httpx
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from starlette.responses import StreamingResponse

from src import audit, llm_client, photo_ocr_client, tts_client, voice_client
from src.webapp_config import WebappConfig

from app.webapp.routers._helpers import client_ip, maybe_json

logger = logging.getLogger(__name__)
router = APIRouter()


def _voice_base(request: Request) -> str:
    """Return the configured voice-transcriber base URL or 503."""
    cfg: WebappConfig = request.app.state.webapp_config
    base = (cfg.voice_transcriber_url or "").strip()
    if not base:
        raise HTTPException(
            status_code=503,
            detail="voice dictation is disabled (voice_transcriber_url unset)",
        )
    return base


@router.post("/api/transcribe/sessions")
async def transcribe_create(request: Request) -> Dict[str, Any]:
    """Create a streamed dictation session (Tailscale-only + passkey, #168)."""
    base = _voice_base(request)
    language = (request.query_params.get("language") or "").strip() or None
    try:
        result = await asyncio.to_thread(voice_client.create_session, base, language)
    except voice_client.VoiceTranscriberError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    audit.audit_event("transcribe_create", client=client_ip(request))
    return result


@router.post("/api/transcribe/sessions/{vid}/chunk")
async def transcribe_chunk(vid: str, request: Request) -> Dict[str, Any]:
    """Forward one raw audio chunk to a streamed session (#168)."""
    base = _voice_base(request)
    content = await request.body()
    if not content:
        return {"session_id": vid, "raw_bytes": 0}
    content_type = request.headers.get("content-type") or "audio/webm"
    try:
        result = await asyncio.to_thread(
            voice_client.send_chunk, base, vid, content, content_type
        )
    except voice_client.VoiceTranscriberError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    return result


@router.post("/api/transcribe/sessions/{vid}/finish")
async def transcribe_finish(vid: str, request: Request) -> Dict[str, Any]:
    """Close a streamed session and return the canonical transcript (#168)."""
    base = _voice_base(request)
    language = (request.query_params.get("language") or "").strip() or None
    try:
        result = await asyncio.to_thread(voice_client.finish, base, vid, language)
    except voice_client.VoiceTranscriberError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    audit.audit_event(
        "transcribe_finish", silent=bool(result.get("silent")), client=client_ip(request)
    )
    return result


@router.get("/api/transcribe/sessions/{vid}/events")
async def transcribe_events(vid: str, request: Request) -> StreamingResponse:
    """Proxy the voice-transcriber's rolling-partial SSE stream (#168).

    ``EventSource`` can't set headers, so the bearer + passkey ``tt`` ride
    the query string (both gates read query params). The upstream stream is
    forwarded chunk-for-chunk so partials reach the phone live; buffering is
    disabled so a proxy can't hold events back.
    """
    base = _voice_base(request)
    url = voice_client.events_url(base, vid)

    async def _pump():
        try:
            async with httpx.AsyncClient(verify=False, timeout=None) as client:
                async with client.stream("GET", url) as upstream:
                    if upstream.status_code >= 400:
                        yield (
                            f"event: error\ndata: upstream HTTP "
                            f"{upstream.status_code}\n\n"
                        ).encode()
                        return
                    async for chunk in upstream.aiter_raw():
                        if chunk:
                            yield chunk
        except httpx.HTTPError as exc:
            logger.debug(f"transcribe SSE proxy {vid} ended: {exc}")
            yield b"event: error\ndata: voice-transcriber unreachable\n\n"

    return StreamingResponse(
        _pump(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/transcribe")
async def transcribe_single_shot(
    request: Request, file: UploadFile = File(...)
) -> Dict[str, Any]:
    """Single-shot transcription fallback for the compose bar (#165).

    The streamed path (#168) is preferred for live partials; this remains
    the no-streaming fallback. The phone records audio and POSTs it here;
    the webapp proxies the blob to the voice-transcriber over loopback and
    returns the transcript for review in the compose textarea.
    """
    base = _voice_base(request)
    language = (request.query_params.get("language") or "").strip() or None
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty recording")
    try:
        result = await asyncio.to_thread(
            voice_client.transcribe,
            base,
            file.filename or "recording.webm",
            content,
            file.content_type or "audio/webm",
            language,
        )
    except voice_client.VoiceTranscriberError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    audit.audit_event(
        "transcribe",
        bytes=len(content),
        silent=bool(result.get("silent")),
        client=client_ip(request),
    )
    return result


def _photo_ocr_base(request: Request) -> str:
    """Return the configured photo-ocr base URL or 503."""
    cfg: WebappConfig = request.app.state.webapp_config
    base = (cfg.photo_ocr_url or "").strip()
    if not base:
        raise HTTPException(
            status_code=503,
            detail="screenshot OCR is disabled (photo_ocr_url unset)",
        )
    return base


@router.post("/api/ocr")
async def ocr_screenshot(
    request: Request, files: List[UploadFile] = File(...)
) -> Dict[str, Any]:
    """Single-shot screenshot OCR for the compose bar (#171).

    The phone captures one or more screenshots and POSTs them here; the
    webapp proxies the images to the sibling photo-ocr over loopback (its
    consumable ``POST /api/extract``) and returns the extracted text for
    review in the compose textarea — the pixel counterpart to
    ``/api/transcribe``. Multiple shots of one document are collated into a
    single deduplicated text by photo-ocr (its whole point). Model/prompt
    are left unset so photo-ocr's own configured defaults apply.
    """
    base = _photo_ocr_base(request)
    model = (request.query_params.get("model") or "").strip() or None
    prompt_id = (request.query_params.get("prompt_id") or "").strip() or None
    blobs = []
    for upload in files:
        content = await upload.read()
        if content:
            blobs.append(
                (
                    upload.filename or "screenshot.png",
                    content,
                    upload.content_type or "image/png",
                )
            )
    if not blobs:
        raise HTTPException(status_code=400, detail="empty image")
    try:
        result = await asyncio.to_thread(
            photo_ocr_client.extract, base, blobs, model, prompt_id
        )
    except photo_ocr_client.PhotoOcrError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    audit.audit_event(
        "ocr",
        images=len(blobs),
        bytes=sum(len(b[1]) for b in blobs),
        chars=int(result.get("chars") or 0),
        client=client_ip(request),
    )
    return result


def _tts_base(request: Request) -> str:
    """Return the configured local-llm-hub base URL or 503."""
    cfg: WebappConfig = request.app.state.webapp_config
    base = (cfg.llm_hub_url or "").strip()
    if not base:
        raise HTTPException(
            status_code=503,
            detail="hub read-aloud is disabled (llm_hub_url unset)",
        )
    return base


@router.get("/api/tts/health")
async def tts_health(request: Request) -> Dict[str, Any]:
    """Is the hub's high-quality read-aloud voice reachable right now (#203)?

    The 🔊 button uses this to decide whether to route through the hub or fall
    back to the on-device Web Speech voice. Degrades to ``available: False``
    (never an error) when the hub is unconfigured or down, so the button is
    always safe to gate on it.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    base = (cfg.llm_hub_url or "").strip()
    if not base:
        return {"available": False}
    try:
        ok = await asyncio.to_thread(tts_client.health, base)
    except tts_client.TtsError as exc:
        logger.debug(f"tts health probe failed: {exc}")
        return {"available": False}
    return {"available": bool(ok)}


@router.post("/api/tts/speak")
async def tts_speak(request: Request) -> StreamingResponse:
    """Stream the read-aloud reply as headerless PCM16 from the hub (#203, #206).

    Body is JSON ``{text, voice?, speed?}``. The webapp forwards it to the
    hub's OpenAI-shape ``POST /v1/audio/speech`` with ``response_format="pcm"``
    + ``stream_format="audio"`` and Orpheus as the default model, then streams
    the raw PCM16 bytes to the browser as they synthesize — the client plays
    them through the Web Audio API for low time-to-first-audio. The hub's
    ``X-Sample-Rate`` is forwarded so the client knows the PCM rate. PCM (not
    WAV) because the hub's streaming WAV uses an open-ended RIFF header an
    ``<audio>`` element can't play progressively (issue #206). Carries the
    terminal's Tailscale-only + passkey gate (the reply text is terminal
    content).
    """
    base = _tts_base(request)
    body = await maybe_json(request)
    text = str(body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    voice = (str(body.get("voice") or "").strip()) or None
    speed = body.get("speed")
    payload = tts_client.build_speech_payload(
        text, voice=voice, speed=speed if isinstance(speed, (int, float)) else None
    )
    upstream_url = tts_client.speech_url(base)

    # Open the upstream stream first so the hub's X-Sample-Rate header can be
    # forwarded on the response (it must be set before streaming begins). This
    # mirrors the hub's own /v1/audio/speech streaming proxy.
    client = httpx.AsyncClient(timeout=None)
    stream_cm = client.stream("POST", upstream_url, json=payload)
    try:
        upstream = await stream_cm.__aenter__()
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"tts upstream error: {exc}")
    if upstream.status_code >= 400:
        await upstream.aread()
        await stream_cm.__aexit__(None, None, None)
        await client.aclose()
        raise HTTPException(
            status_code=502, detail=f"tts hub HTTP {upstream.status_code}"
        )
    sample_rate = upstream.headers.get("x-sample-rate", "24000")

    async def _forward():
        try:
            async for chunk in upstream.aiter_bytes():
                if chunk:
                    yield chunk
        except httpx.HTTPError as exc:
            logger.debug(f"tts speak proxy ended: {exc}")
        finally:
            await stream_cm.__aexit__(None, None, None)
            await client.aclose()

    audit.audit_event("tts_speak", chars=len(text), client=client_ip(request))
    return StreamingResponse(
        _forward(),
        media_type="audio/L16",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-Sample-Rate": str(sample_rate),
        },
    )


@router.post("/api/tts/summarize")
async def tts_summarize(request: Request) -> Dict[str, Any]:
    """Summarize the agent's last reply for hands-free / driving listening (#210).

    Body is JSON ``{text}``. Forwards it to the hub's ``claude-haiku-4-5`` with
    a driving-mode prompt (the essence + any decision to take) and returns
    ``{summary}``; the client then reads the summary aloud through the same
    voice path as ``/api/tts/speak``. Carries the terminal's Tailscale-only +
    passkey gate (the input text is the agent's reply — terminal content).
    """
    base = _tts_base(request)
    body = await maybe_json(request)
    text = str(body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    try:
        summary = await asyncio.to_thread(llm_client.summarize, base, text)
    except llm_client.LlmError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    audit.audit_event(
        "tts_summarize",
        chars=len(text),
        summary_chars=len(summary),
        client=client_ip(request),
    )
    return {"summary": summary}
