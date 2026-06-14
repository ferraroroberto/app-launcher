"""Live PTY sessions — list, stop, upload image, WebSocket proxy.

The WS proxy is the only endpoint where auth is re-applied inline:
Starlette middleware doesn't see WebSocket handshakes, so the Tailscale
+ bearer + passkey checks live here.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Any, Dict, List

import httpx
from fastapi import APIRouter, File, HTTPException, Request, UploadFile, WebSocket
from starlette.responses import StreamingResponse
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import InvalidHandshake

from src import (
    audit,
    launcher,
    photo_ocr_client,
    session_client,
    tts_client,
    voice_client,
)
from src.webapp_config import WebappConfig
from src.webauthn_gate import WebAuthnGate

from app.webapp.middleware import (
    LOOPBACK_HOSTS,
    client_in_tailnet,
    via_cloudflare,
)
from app.webapp.routers._helpers import client_ip, client_ip_ws, maybe_json

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/claude-code/sessions")
async def claude_sessions(request: Request) -> Dict[str, Any]:
    """List launcher-owned PTY sessions (public, token-gated)."""
    cfg: WebappConfig = request.app.state.webapp_config
    try:
        sessions = await asyncio.to_thread(
            session_client.list_sessions, cfg.session_host_port
        )
    except session_client.SessionHostError as exc:
        logger.debug(f"session list failed: {exc}")
        sessions = []
    return {"sessions": sessions}


@router.post("/api/claude-code/sessions/{sid}/stop")
async def stop_claude_session(sid: str, request: Request) -> Dict[str, Any]:
    """Stop a PTY session — quit | interrupt | kill (public, token-gated)."""
    cfg: WebappConfig = request.app.state.webapp_config
    body = await maybe_json(request)
    mode = str(body.get("mode") or "quit")
    close_window = bool(body.get("close_window", False))
    # Win32 close of the PC mirror window — primary mechanism for Stop &
    # Close (issue #20). close_mirror_window first tries the HWND stashed
    # at spawn time, then falls back to a fresh title-scan of live windows
    # (issue #199) so it works even after a webapp restart wiped the
    # in-memory registry. Best-effort: swallow any exception so a busted
    # HWND can't keep the session alive. The cooperative WS shutdown below
    # is a further fallback for when no matching window is on the desktop.
    if close_window:
        try:
            posted = launcher.close_mirror_window(sid)
            logger.debug(
                f"close_mirror_window({sid[:8]}) returned {posted}; "
                f"forwarding stop({mode!r}) to session-host"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"🛑 mirror window close raised for {sid[:8]}: {exc}"
            )
    try:
        result = await asyncio.to_thread(
            session_client.stop, cfg.session_host_port, sid, mode, close_window
        )
    except session_client.SessionHostError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    audit.audit_event(
        "session_stop", session=sid, mode=mode, close_window=close_window, client=client_ip(request)
    )
    audit.session_log(sid, "stop", mode=mode, close_window=close_window)
    return result


@router.post("/api/claude-code/sessions/{sid}/image")
async def session_image(
    sid: str, request: Request, file: UploadFile = File(...)
) -> Dict[str, Any]:
    """Upload an image into a session (Tailscale-only + passkey).

    ``?inline=1`` (compose bar open) tells the session-host to skip the
    paste-into-PTY step and just return the stored path so the browser
    can drop it into the compose textarea for review (issue #41).
    """
    cfg: WebappConfig = request.app.state.webapp_config
    inline = request.query_params.get("inline") in ("1", "true")
    content = await file.read()
    try:
        result = await asyncio.to_thread(
            session_client.upload_image,
            cfg.session_host_port,
            sid,
            file.filename or "image.png",
            content,
            file.content_type or "application/octet-stream",
            inline,
        )
    except session_client.SessionHostError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    audit.session_log(
        sid, "image", path=result.get("path"), bytes=len(content), inline=inline
    )
    return result


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


@router.websocket("/api/claude-code/sessions/{sid}/ws")
async def proxy_session_ws(websocket: WebSocket, sid: str) -> None:
    """Tailscale-only + passkey-gated WebSocket proxy to the session-host.

    Browser ⇄ webapp ⇄ session-host. The webapp is the single auth
    choke point — WebSockets bypass the HTTP middleware, so the same
    Tailscale + bearer + passkey checks are re-applied here.
    """
    cfg: WebappConfig = websocket.app.state.webapp_config
    gate: WebAuthnGate = websocket.app.state.webauthn_gate
    client_host = websocket.client.host if websocket.client else ""

    # Accept first, *then* gate — so the browser receives the close
    # code + reason and can show a clear message instead of a bare
    # "Disconnected" (closing before accept just fails the handshake).
    await websocket.accept()

    if client_host not in LOOPBACK_HOSTS:
        if via_cloudflare(websocket.headers):
            await websocket.close(
                code=4403,
                reason="terminal is Tailscale-only — blocked on the public tunnel",
            )
            return
        if not client_in_tailnet(
            client_host, getattr(cfg, "tailnet_allowlist", [])
        ):
            await websocket.close(
                code=4403, reason="terminal is Tailscale-only"
            )
            return
        token = (cfg.auth_token or "").strip()
        if token:
            presented = websocket.query_params.get("token", "").strip()
            if not (presented and hmac.compare_digest(presented, token)):
                await websocket.close(
                    code=4401, reason="missing or invalid bearer token"
                )
                return
        if WebAuthnGate.configured(cfg):
            tt = websocket.query_params.get("tt", "").strip()
            if not gate.valid_terminal_token(tt):
                await websocket.close(
                    code=4401, reason="passkey unlock required"
                )
                return

    # The phone drives the PTY size; the loopback PC mirror window
    # connects as role=pc and never resizes it (see session-host).
    role = "pc" if client_host in LOOPBACK_HOSTS else "phone"
    upstream_url = session_client.ws_url(cfg.session_host_port, sid, role)
    try:
        async with ws_connect(upstream_url) as upstream:
            audit.audit_event(
                "ws_open", session=sid, client=client_ip_ws(websocket)
            )
            audit.session_log(sid, "ws_open", client=client_ip_ws(websocket))
            await _proxy_websocket(websocket, upstream, sid)
    except (OSError, WebSocketDisconnect, InvalidHandshake) as exc:
        # InvalidHandshake covers an upstream WS upgrade rejected at the
        # HTTP layer — e.g. the session-host answering 403 for a reaped
        # or unknown session (InvalidStatus). Same "upstream not usable"
        # condition as OSError; map it to the clean 4502 close instead of
        # letting it escape as an unhandled ASGI traceback (issue #61).
        logger.debug(f"WS proxy {sid[:8]} ended: {exc}")
        try:
            await websocket.close(
                code=4502, reason="session-host unreachable"
            )
        except RuntimeError:
            pass
    finally:
        audit.session_log(sid, "ws_close")


async def _proxy_websocket(client: WebSocket, upstream, sid: str) -> None:
    """Pump frames both ways between the browser and the session-host.

    Server→client frames are raw terminal output. Client→server frames are
    JSON control messages — ``input`` frames are tee'd to the per-session
    audit log on the way through.
    """

    async def client_to_upstream() -> None:
        while True:
            raw = await client.receive_text()
            try:
                msg = json.loads(raw)
                if isinstance(msg, dict) and msg.get("type") == "input":
                    audit.session_input(sid, str(msg.get("data") or ""))
            except (ValueError, TypeError):
                pass
            await upstream.send(raw)

    async def upstream_to_client() -> None:
        async for message in upstream:
            if isinstance(message, bytes):
                message = message.decode("utf-8", errors="replace")
            await client.send_text(message)
        # session-host closed its side (session ended) — close the browser.
        await client.close(code=4000)

    c2u = asyncio.create_task(client_to_upstream())
    u2c = asyncio.create_task(upstream_to_client())
    done, pending = await asyncio.wait(
        {c2u, u2c}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    for task in done:
        exc = task.exception()
        if exc and not isinstance(exc, WebSocketDisconnect):
            logger.debug(f"WS proxy {sid[:8]} task ended: {exc}")
