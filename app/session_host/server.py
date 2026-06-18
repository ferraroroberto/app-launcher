"""Loopback-only HTTP + WebSocket surface for launcher-owned PTY sessions.

Binds ``127.0.0.1`` exclusively — it is **never** directly reachable from
the network. The main webapp (which owns all auth, Tailscale gating, and
WebAuthn) proxies to it. Keeping the PTYs in this separate long-lived
process means a webapp restart doesn't kill running Claude sessions.

Routes:

    POST   /sessions                  → spawn `claude` (kind=pty|remote)
    GET    /sessions                  → list live sessions
    GET    /sessions/{sid}            → one session's detail
    POST   /sessions/{sid}/input      → write text to the PTY
    POST   /sessions/{sid}/resize     → resize the PTY
    POST   /sessions/{sid}/stop       → interrupt | quit | kill
    POST   /sessions/{sid}/image      → save an uploaded image, type its path
    WS     /sessions/{sid}/ws?role=   → scrollback snapshot + live duplex stream

Only ``kind=pty`` sessions have a WebSocket. ``role`` (``pc`` | ``phone``,
default ``phone``) marks who the client is: ``resize`` frames are honoured
only from the phone, so the phone and the PC mirror window never fight
over the single PTY's dimensions.

WebSocket protocol — server→client frames are raw terminal output;
client→server frames are JSON: ``{"type":"input","data":"…"}`` or
``{"type":"resize","rows":N,"cols":N}``.

Run standalone: ``python -m app.session_host.server`` (or the
``session-host`` CLI subcommand).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect

from src.agents import AGENTS, DEFAULT_AGENT, is_fullscreen
from src.session_host import _EOF, SessionManager

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8446

# Background repaint-nudge tasks, kept referenced so the event loop doesn't
# GC them before they run (issue #128).
_repaint_tasks: "set[asyncio.Task]" = set()

# Repaint-nudge timing (issue #128). The initial delay lets the client's
# real-size resize land first (so we toggle around the right dimensions);
# the gap between the two setwinsize calls stops ConPTY coalescing them
# into a net-zero change that would fire no SIGWINCH.
_REPAINT_SETTLE = 0.15
_REPAINT_TOGGLE_GAP = 0.05

# Where uploaded images land inside the project so `claude` can read them.
_IMAGE_DIR_NAME = ".launcher-tmp"
_SAFE_IMAGE_EXT = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
_MAX_IMAGE_BYTES = 12 * 1024 * 1024

manager = SessionManager()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    manager.attach_loop(asyncio.get_running_loop())
    reaper = asyncio.create_task(_reap_loop())
    try:
        yield
    finally:
        reaper.cancel()
        manager.shutdown()


async def _reap_loop() -> None:
    """Drop exited sessions every 30 s so the list stays honest."""
    try:
        while True:
            await asyncio.sleep(30)
            reaped = manager.reap_dead()
            if reaped:
                logger.info(f"🧹 Reaped {reaped} dead PTY session(s)")
    except asyncio.CancelledError:  # pragma: no cover
        pass


def create_app() -> FastAPI:
    app = FastAPI(title="Launcher session-host", version="0.1.0", lifespan=_lifespan)

    @app.get("/healthz")
    async def healthz() -> Dict[str, Any]:
        return {"ok": True, "service": "session-host", "sessions": len(manager.list())}

    @app.post("/sessions")
    async def create_session(request: Request) -> Dict[str, Any]:
        body = await _json(request)
        project_dir = str(body.get("project_dir") or "").strip()
        name = str(body.get("name") or "claude").strip() or "claude"
        flags = str(body.get("flags") or "").strip()
        kind = str(body.get("kind") or "pty").strip().lower()
        agent = str(body.get("agent") or DEFAULT_AGENT).strip().lower()
        # Phone-supplied spawn dimensions (issue #126): size the PTY to the
        # real viewport before first paint so a ratatui TUI isn't cut.
        # Omitted → the manager's legacy 40×120 default.
        rows = int(body.get("rows") or 40)
        cols = int(body.get("cols") or 120)
        if not project_dir:
            raise HTTPException(status_code=400, detail="project_dir is required")
        if agent not in AGENTS:
            raise HTTPException(status_code=400, detail=f"unknown agent: {agent}")
        try:
            if kind == "remote":
                session = manager.create_remote(project_dir, name, flags, agent)
            else:
                session = manager.create(
                    project_dir, name, flags, agent, rows=rows, cols=cols
                )
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return session.to_api()

    @app.get("/sessions")
    async def list_sessions() -> Dict[str, Any]:
        return {"sessions": [s.to_api() for s in manager.list()]}

    @app.get("/sessions/{sid}")
    async def get_session(sid: str) -> Dict[str, Any]:
        session = manager.get(sid)
        if session is None:
            raise HTTPException(status_code=404, detail=f"unknown session {sid}")
        return session.to_api()

    @app.post("/sessions/{sid}/input")
    async def session_input(sid: str, request: Request) -> Dict[str, Any]:
        session = manager.get(sid)
        if session is None:
            raise HTTPException(status_code=404, detail=f"unknown session {sid}")
        body = await _json(request)
        session.write(str(body.get("data") or ""))
        return {"ok": True}

    @app.post("/sessions/{sid}/resize")
    async def session_resize(sid: str, request: Request) -> Dict[str, Any]:
        session = manager.get(sid)
        if session is None:
            raise HTTPException(status_code=404, detail=f"unknown session {sid}")
        body = await _json(request)
        session.resize(int(body.get("rows") or 40), int(body.get("cols") or 120))
        return {"ok": True}

    @app.post("/sessions/{sid}/stop")
    async def session_stop(sid: str, request: Request) -> Dict[str, Any]:
        session = manager.get(sid)
        if session is None:
            raise HTTPException(status_code=404, detail=f"unknown session {sid}")
        body = await _json(request)
        mode = str(body.get("mode") or "quit")
        # The graceful stop polls for exit up to a few seconds — run it off
        # the event loop so the session-host stays responsive (issue #253).
        await asyncio.to_thread(session.stop, mode)
        return {"ok": True, "mode": mode}

    @app.post("/sessions/{sid}/image")
    async def session_image(
        sid: str, file: UploadFile, inline: bool = False
    ) -> Dict[str, Any]:
        session = manager.get(sid)
        if session is None:
            raise HTTPException(status_code=404, detail=f"unknown session {sid}")
        path = await _save_image(session.project_dir, file)
        # inline=1 (compose bar open): skip the paste — the caller drops the
        # returned path into the textarea for review-before-send (issue #41).
        if not inline:
            # Bracketed paste so the Claude TUI takes the path as one unit.
            session.write(f"\x1b[200~{path}\x1b[201~")
        return {"ok": True, "path": path, "inline": inline}

    @app.websocket("/sessions/{sid}/ws")
    async def session_ws(websocket: WebSocket, sid: str) -> None:
        session = manager.get(sid)
        if session is None:
            await websocket.close(code=4404)
            return
        # Remote sessions are detached console windows — no PTY to stream.
        if getattr(session, "kind", "pty") != "pty":
            await websocket.close(code=4404, reason="remote session has no terminal")
            return
        role = (websocket.query_params.get("role") or "phone").strip().lower()
        await websocket.accept()
        snapshot, queue = session.subscribe()
        try:
            if is_fullscreen(getattr(session, "agent", DEFAULT_AGENT)):
                # Full-screen differential TUI (Codex/ratatui): do NOT replay
                # the raw scrollback ring. Replaying its stale move-cursor /
                # clear deltas garbles a fresh xterm, and replaying the
                # agent's startup terminal queries makes xterm re-answer them
                # as input — the `[?1;2c` DA leak (issue #128). Force a clean
                # repaint at the current size instead. Role-independent: the
                # leak hits the PC mirror too.
                task = asyncio.create_task(_force_repaint(session))
                _repaint_tasks.add(task)
                task.add_done_callback(_repaint_tasks.discard)
            elif snapshot:
                await websocket.send_text(snapshot)
            await asyncio.gather(
                _pump_to_client(websocket, queue),
                _pump_from_client(websocket, session, role),
            )
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"WS {sid[:8]} ended: {exc}")
        finally:
            session.unsubscribe(queue)

    return app


# ----------------------------------------------------------------- helpers


async def _json(request: Request) -> Dict[str, Any]:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


async def _force_repaint(session) -> None:
    """Nudge a full-screen TUI into repainting a clean frame after a
    (re)connect (issue #128).

    We skip the raw differential-ring replay for these agents, so the
    viewport is blank until the agent next draws. Toggle the PTY width by
    one column and back: each ``setwinsize`` fires a SIGWINCH-equivalent,
    so ratatui clears and redraws the *current* frame at the real size.
    The toggle guarantees a change even on a same-size reconnect (where a
    single ``setwinsize`` to the unchanged size is a no-op). Best-effort —
    a dead PTY's ``resize`` already swallows its own errors.
    """
    try:
        await asyncio.sleep(_REPAINT_SETTLE)
        rows, cols = session.rows, session.cols
        session.resize(rows, max(1, cols - 1))
        await asyncio.sleep(_REPAINT_TOGGLE_GAP)
        session.resize(rows, cols)
    except asyncio.CancelledError:  # pragma: no cover
        raise
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"force_repaint failed: {exc}")


async def _pump_to_client(websocket: WebSocket, queue: "asyncio.Queue") -> None:
    """Forward PTY output (and the EOF sentinel) to the browser."""
    while True:
        chunk = await queue.get()
        if chunk is _EOF:
            await websocket.close(code=4000)
            return
        await websocket.send_text(chunk)


async def _pump_from_client(
    websocket: WebSocket, session, role: str = "phone"
) -> None:
    """Apply JSON control frames coming from the browser to the PTY.

    ``resize`` frames are honoured only from the phone (``role != "pc"``) —
    the phone is the size authority. The PC mirror window connects with
    ``role=pc`` and renders whatever size the phone set, so the two never
    fight over the single PTY's dimensions.
    """
    while True:
        raw = await websocket.receive_text()
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(msg, dict):
            continue
        kind = msg.get("type")
        if kind == "input":
            session.write(str(msg.get("data") or ""))
        elif kind == "resize" and role != "pc":
            session.resize(int(msg.get("rows") or 40), int(msg.get("cols") or 120))


async def _save_image(project_dir: str, file: UploadFile) -> str:
    """Persist an uploaded image under ``<project>/.launcher-tmp`` and return
    its absolute path. Rejects oversize files and non-image extensions."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _SAFE_IMAGE_EXT:
        raise HTTPException(status_code=400, detail=f"unsupported image type {suffix!r}")
    data = await file.read()
    if len(data) > _MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="image exceeds 12 MB")
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    target_dir = Path(project_dir) / _IMAGE_DIR_NAME
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^a-zA-Z0-9._-]", "_", Path(file.filename or "img").stem)[:40]
        out = target_dir / f"{stamp}-{uuid.uuid4().hex[:6]}-{safe}{suffix}"
        out.write_bytes(data)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"could not save image: {exc}")
    return str(out)


def run_session_host(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
    """Run the session-host uvicorn server (loopback-only)."""
    import uvicorn

    # Force loopback — this surface must never be network-reachable.
    bind = host if host in ("127.0.0.1", "::1", "localhost") else DEFAULT_HOST
    logger.info(f"🧩 session-host on http://{bind}:{port}")
    uvicorn.run(app, host=bind, port=port, log_level="warning")
    return 0


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_session_host())
