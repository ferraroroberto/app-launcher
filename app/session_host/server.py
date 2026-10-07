"""Loopback-only HTTP + WebSocket surface for launcher-owned PTY sessions.

Binds ``127.0.0.1`` exclusively — it is **never** directly reachable from
the network. The main webapp (which owns all auth, Tailscale gating, and
WebAuthn) proxies to it. Keeping the PTYs in this separate long-lived
process means a webapp restart doesn't kill running Claude sessions.

Routes:

    POST   /sessions                  → spawn a registered terminal command
    GET    /sessions                  → list live sessions
    GET    /sessions/{sid}            → one session's detail
    POST   /sessions/{sid}/input      → write text to the PTY (or, for a
                                        detached session, type it into its
                                        console — delivered "unconfirmed", #967)
    POST   /sessions/{sid}/resize     → resize the PTY
    POST   /sessions/{sid}/stop       → interrupt | quit | kill
    POST   /sessions/{sid}/rename     → set/clear a manual title override
    POST   /sessions/{sid}/image      → save an uploaded image, type its path
    POST   /sessions/{sid}/large-file → stream a large file to disk, return
                                        its path (never typed, #1430)
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
import os
import re
import stat
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect

from src.agents import DEFAULT_AGENT, SESSION_HOST_AGENTS, is_fullscreen
from src.build_info import build_identity
from src.session_host import _EOF, PTY_MIN_COLS, SessionManager
from src.session_host_input import (
    INPUT_CONSOLE_FAILED,
    INPUT_DEFERRED,
    INPUT_DROPPED,
    INPUT_NOT_INGESTED,
)

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

# Clean-frame preamble for a full-screen TUI (re)connect (#270 tail-jump).
# A no-alt-screen agent (Codex/ratatui) paints the *main* buffer, so on a
# reconnect — where the client reuses the same xterm instance with the stale
# frame still in its buffer — the forced repaint is appended *below* that
# stale content, and xterm auto-follows to the bottom, scrolling through the
# old frame to reach the prompt (the "crawl"). Sending an erase-scrollback +
# clear-screen + home preamble before the repaint nudge wipes the client's
# buffer so the fresh frame lands on an empty screen — the reopened session
# jumps straight to the current frame. CSI only (no OSC/DA), so it can't
# reintroduce the query leak the #128/#270 strip removed.
_CLEAR_FRAME = "\x1b[H\x1b[2J\x1b[3J"

# Where uploaded files land inside the project so `claude` can read them.
# Any file type is accepted (issue #366 — the compose-bar attach covers
# documents, not just photos): the file is only ever *stored* here and its
# path pasted into the user's own prompt, nothing executes it; the surface
# is loopback-only and the size cap below still applies. A suffix that
# doesn't look like a plain extension is dropped rather than trusted.
_IMAGE_DIR_NAME = ".launcher-tmp"
_SAFE_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,10}$")
_MAX_IMAGE_BYTES = 12 * 1024 * 1024
# Harness-only override for the *root* those uploads hang off (issue #922),
# the same shape as `LAUNCHER_AUDIT_DIR` (#913). Nothing in production sets
# it, so where a real upload lands is unchanged; the e2e gate points it at a
# per-run temp dir, because the disposable session-host it spawns runs
# sessions whose project dir is this very checkout — so every image-upload
# test wrote into the checkout's own `.launcher-tmp`, where 3,423 of the
# 3,600 files turned out to be its 1x1 test PNGs, with nothing pruning them.
UPLOAD_ROOT_ENV = "LAUNCHER_UPLOAD_ROOT"

# Large-file uploads (#1430): a file put *on the machine* for the agent to
# move, archive or process by path, never read into its context. The body
# streams to disk in chunks and is never held whole in memory. Each session
# gets its own leaf, `.launcher-tmp/large/<sid>/`, so a session's end deletes
# exactly its own uploads and nobody else's. The size limit is the caller's
# (`max_bytes`, from the webapp's `large_upload_max_mb` setting): the webapp
# owns that setting, and passing it per call means a Settings change applies
# without restarting this process.
_LARGE_DIR_NAME = "large"
# Buffered up to this much before each off-loop disk write, so a multi-GB
# upload costs a few thousand thread hops rather than one per network chunk.
_LARGE_FLUSH_BYTES = 1024 * 1024
# Advertised on /healthz so the webapp can tell a session-host that predates
# the route ("restart needed") from one that is down ("unreachable").
_FEATURES = ("large_upload",)
# TTL fallbacks for whatever session-end cleanup misses: a host restart or
# crash loses the end of a session, and ordinary attachments had no pruning
# at all before #1430. A large upload is meant for the session that took it,
# so a day is plenty. An ordinary attachment is a path in a prompt that a
# resumed conversation may still point at, so it is kept for a week.
_LARGE_TTL_SECONDS = 24 * 3600
_UPLOAD_TTL_SECONDS = 7 * 24 * 3600

# Captured once at import — the whole point is that this does NOT track live
# git state (#615): it's what this specific process loaded when it started,
# which is what a caller needs to know when this process is excluded from
# the webapp's own restart and can keep running for days unattended.
_IDENTITY = build_identity()

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
                logger.info(f"🧹 Reaped {len(reaped)} dead PTY session(s)")
                await asyncio.to_thread(_cleanup_ended_sessions, reaped)
    except asyncio.CancelledError:  # pragma: no cover
        pass


def create_app() -> FastAPI:
    app = FastAPI(title="Launcher session-host", version="0.1.0", lifespan=_lifespan)

    @app.get("/healthz")
    async def healthz() -> Dict[str, Any]:
        return {
            "ok": True,
            "service": "session-host",
            "sessions": len(manager.list()),
            "git_sha": _IDENTITY["git_sha"],
            "started_at": _IDENTITY["captured_at"],
            "features": list(_FEATURES),
        }

    @app.post("/sessions")
    async def create_session(request: Request) -> Dict[str, Any]:
        body = await _json(request)
        project_dir = str(body.get("project_dir") or "").strip()
        name = str(body.get("name") or "claude").strip() or "claude"
        flags = str(body.get("flags") or "").strip()
        kind = str(body.get("kind") or "pty").strip().lower()
        agent = str(body.get("agent") or DEFAULT_AGENT).strip().lower()
        # Role tag (e.g. "chief", #245) so callers can find a purpose-built
        # session deterministically. PTY-only; remote sessions ignore it.
        label = str(body.get("label") or "").strip().lower()
        # Phone-supplied spawn dimensions (issue #126): size the PTY to the
        # real viewport before first paint so a ratatui TUI isn't cut.
        # Omitted → the manager's legacy 40×120 default.
        rows = int(body.get("rows") or 40)
        cols = int(body.get("cols") or 120)
        # User-configurable scrollback depth for full-screen agents (issue
        # #435 follow-up, Settings tab). Omitted/absent → SessionManager's
        # own default (older webapp builds, or a caller that doesn't set it).
        history_lines_raw = body.get("history_lines")
        history_lines = int(history_lines_raw) if history_lines_raw else None
        if not project_dir:
            raise HTTPException(status_code=400, detail="project_dir is required")
        if agent not in SESSION_HOST_AGENTS:
            raise HTTPException(status_code=400, detail=f"unknown agent: {agent}")
        try:
            # Off the event loop (issue #610): SessionManager.create/
            # create_remote block on a real OS spawn (PtyProcess.spawn / a
            # PowerShell Start-Process). Left un-threaded, one slow spawn
            # freezes every other coroutine sharing this loop — including
            # an already-attached terminal's _pump_to_client, which is a
            # concrete contributor to "terminal opens blank" under a
            # multi-session burst (a fixed-delay mitigation was already
            # ruled insufficient for the analogous #499 readiness problem).
            if kind == "remote":
                session = await asyncio.to_thread(
                    manager.create_remote, project_dir, name, flags, agent
                )
            else:
                session = await asyncio.to_thread(
                    manager.create, project_dir, name, flags, agent,
                    rows=rows, cols=cols, history_lines=history_lines,
                    label=label,
                )
        except (OSError, RuntimeError, ValueError) as exc:
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
        data = str(body.get("data") or "")
        submit = bool(body.get("submit", True))
        # submit_input() blocks in real time — a bulk payload's settle wait
        # (issue #611) plus its ingest verification (#760) plus write()'s own
        # chunk-and-pace pauses over ~512 bytes — so offload it like .stop
        # (issue #253) so it doesn't stall every other live session's WS pump.
        # Both session kinds implement submit_input (issue #967): a PtySession
        # writes the PTY with the framing/settle protocol, a RemoteSession
        # spawns the console-input helper against its console PID and can
        # only ever answer ``delivered: "unconfirmed"``.
        outcome = await asyncio.to_thread(session.submit_input, data, submit)
        if outcome.reason == INPUT_DROPPED:
            # The session had already exited (or the PTY write raised) — it
            # can still be sitting in manager.get() for up to the 30s reap
            # window. Report the drop instead of the previous unconditional
            # {"ok": true}, which let a caller believe a message landed when
            # it never reached the PTY at all (issue #607).
            raise HTTPException(
                status_code=409, detail=f"session {sid} not accepting input (exited)"
            )
        if outcome.reason == INPUT_NOT_INGESTED:
            # Written, but the terminal never painted it back — the silent
            # drop #760 was filed over. A distinct status from the 409 above:
            # the session is alive and may well still take keyboard input, it
            # just did not take *this*. No CR was sent, so nothing is
            # half-done and a caller can safely resend once the terminal is
            # back in a state that accepts a paste.
            raise HTTPException(
                status_code=502,
                detail=(
                    f"session {sid} never echoed the input after "
                    f"{outcome.waited_ms}ms — NOT delivered, and no submit was "
                    f"sent; the terminal may be in a modal/dialog state"
                ),
            )
        if outcome.reason == INPUT_CONSOLE_FAILED:
            # Detached target (issue #967): the helper could not attach to or
            # type into the console — the session is alive by PID but its
            # console did not take the keystrokes (or the agent was never
            # probed for console input). Nothing was typed, nothing was
            # submitted; a caller may retry once the console is back.
            raise HTTPException(
                status_code=502,
                detail=(
                    f"session {sid} console input failed — NOT typed, no "
                    f"submit was sent: {outcome.error}"
                ),
            )
        if outcome.reason == INPUT_DEFERRED:
            # The payload is in the composer but the agent was still working,
            # so the submitting CR is with a background watcher (#763) rather
            # than fired blind mid-repaint. 202 is the honest status for that:
            # accepted, not yet completed. The final verdict lands on the
            # session's ``last_input`` (GET /sessions/{sid}), so a caller can
            # follow it up without holding the request open.
            return JSONResponse(status_code=202, content={"ok": True, **outcome.to_api()})
        # 200 carries the whole verdict: a submit that went out unverified is
        # a success-shaped response that must still say so
        # (submit_state="unconfirmed") rather than imply a confirmed landing.
        # ``delivered`` is never true for a submit that was not sent (#929).
        return {"ok": True, **outcome.to_api()}

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

    @app.post("/sessions/{sid}/rename")
    async def session_rename(sid: str, request: Request) -> Dict[str, Any]:
        session = manager.get(sid)
        if session is None:
            raise HTTPException(status_code=404, detail=f"unknown session {sid}")
        body = await _json(request)
        manager.rename(sid, str(body.get("title") or ""))
        return session.to_api()

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

    @app.post("/sessions/{sid}/large-file")
    async def session_large_file(
        sid: str, request: Request, max_bytes: int, name: str = ""
    ) -> Dict[str, Any]:
        """Stream a raw request body to disk (#1430). The body is the file
        itself, not a multipart form, so it can be written as it arrives;
        ``name`` carries the original filename. The path is only returned,
        never typed into the PTY: the caller drops it into the composer with
        a note telling the agent not to read the file."""
        session = manager.get(sid)
        if session is None:
            raise HTTPException(status_code=404, detail=f"unknown session {sid}")
        path, size = await _save_large_upload(
            session.project_dir, sid, name, request, max_bytes
        )
        return {"ok": True, "path": path, "bytes": size}

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
        # Breadcrumb for #610 ("terminal opens blank and never paints"): an
        # empty ring at attach is expected for a session that hasn't
        # printed yet, but if a future report recurs, this timestamp +
        # ring size lets it be correlated against a concurrent create burst
        # (see #610's asyncio.to_thread fix above) without guessing blind.
        logger.info(
            f"🔌 WS attach {sid[:8]} role={role} ring_chars={len(snapshot)}"
        )
        try:
            if is_fullscreen(getattr(session, "agent", DEFAULT_AGENT)):
                # Full-screen differential TUI (Codex/ratatui): do NOT replay
                # the raw scrollback ring. Replaying its stale move-cursor /
                # clear deltas garbles a fresh xterm, and replaying the
                # agent's startup terminal queries makes xterm re-answer them
                # as input — the `[?1;2c` DA leak (issue #128).
                #
                # Wipe the client's buffer first (#270 tail-jump): the xterm
                # instance is reused across a reconnect, so without this a
                # repaint appends below the stale frame and crawls through it.
                await websocket.send_text(_CLEAR_FRAME)
                # Serve the headless-VT current-frame snapshot (issue #432):
                # no winsize toggle, no SIGWINCH, no agent re-emission — a
                # ratatui agent re-emits its ENTIRE transcript on any resize
                # (issue #430), which is exactly what the old toggle-based
                # repaint nudge below used to trigger on every (re)connect,
                # visible to every subscriber including the PC mirror.
                frame = session.snapshot_frame()
                if frame:
                    await websocket.send_text(frame)
                else:
                    # No VT frame yet (nothing painted, or an older session
                    # from before this build) — fall back to the toggle nudge.
                    task = asyncio.create_task(_force_repaint(session))
                    _repaint_tasks.add(task)
                    task.add_done_callback(_repaint_tasks.discard)
            elif snapshot:
                # Wipe the client's buffer before the raw-ring replay
                # (#444). The phone reuses the same xterm instance across
                # reconnects — the #28 backoff re-runs connectTerminalWs on
                # the same terminal, and the #430 warm cache keeps it alive
                # across overlay close/re-open — so without the wipe this
                # full-ring replay is APPENDED below the stale buffer,
                # duplicating the conversation tail on every reconnect
                # (worst on iOS, which drops the WS each time the PWA is
                # backgrounded). Same _CLEAR_FRAME the fullscreen branch
                # sends; prepended into the same frame so wipe + replay
                # render atomically.
                await websocket.send_text(_CLEAR_FRAME + snapshot)
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
    single ``setwinsize`` to the unchanged size is a no-op). A PTY already at
    the width floor toggles one column *up* instead — ``resize`` clamps to
    the floor and skips an unchanged size (#930), so a toggle down would be
    swallowed and fire no SIGWINCH. Best-effort — a dead PTY's ``resize``
    already swallows its own errors.
    """
    try:
        await asyncio.sleep(_REPAINT_SETTLE)
        rows, cols = session.rows, session.cols
        session.resize(rows, cols - 1 if cols - 1 >= PTY_MIN_COLS else cols + 1)
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
            # Offload for the same reason as POST /sessions/{sid}/input —
            # write() blocks in real time on large payloads.
            await asyncio.to_thread(session.write, str(msg.get("data") or ""))
        elif kind == "resize" and role != "pc":
            session.resize(int(msg.get("rows") or 40), int(msg.get("cols") or 120))


def _upload_dir(project_dir: str) -> Path:
    """Where an upload for ``project_dir`` lands.

    Normally ``<project>/.launcher-tmp`` — beside the code the agent is
    working on, so the path pasted into its prompt is already inside the
    directory it has access to. ``LAUNCHER_UPLOAD_ROOT`` replaces the *root*
    only and keeps the ``.launcher-tmp`` leaf, so a redirected upload is
    still recognisably one. Blank or whitespace counts as unset, so an empty
    variable can't silently turn the path into a relative one off the CWD.

    Read per call rather than resolved at import — unlike ``src.audit`` this
    module builds no import-time state from it, and per-call keeps the
    override exercisable without reloading a module that owns the FastAPI app
    and the live ``SessionManager``.
    """
    override = os.environ.get(UPLOAD_ROOT_ENV, "").strip()
    return Path(override or project_dir) / _IMAGE_DIR_NAME


def _upload_name(filename: str) -> str:
    """A stored upload's file name: ``<stamp>-<id>-<stem><suffix>``. The stem
    is reduced to safe characters and an odd-looking extension is dropped
    rather than trusted, so nothing the client sends shapes the path beyond
    those characters."""
    suffix = Path(filename or "").suffix.lower()
    if not _SAFE_SUFFIX_RE.match(suffix):
        suffix = ""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", Path(filename or "img").stem)[:40]
    return f"{stamp}-{uuid.uuid4().hex[:6]}-{safe}{suffix}"


def _large_dir(project_dir: str, sid: str) -> Path:
    """Where session ``sid``'s large uploads land (#1430):
    ``<upload dir>/large/<sid>``."""
    return _upload_dir(project_dir) / _LARGE_DIR_NAME / re.sub(r"[^A-Za-z0-9_-]", "_", sid)


async def _save_image(project_dir: str, file: UploadFile) -> str:
    """Persist an uploaded file under ``<project>/.launcher-tmp`` (see
    :func:`_upload_dir`) and return its absolute path. Any type is stored
    (issue #366); oversize and empty uploads are rejected, and an odd-looking
    extension is stripped rather than written."""
    data = await file.read()
    if len(data) > _MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="file exceeds 12 MB")
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    target_dir = _upload_dir(project_dir)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        out = target_dir / _upload_name(file.filename or "")
        out.write_bytes(data)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"could not save image: {exc}")
    await asyncio.to_thread(_prune_uploads, project_dir)
    return str(out)


async def _save_large_upload(
    project_dir: str, sid: str, filename: str, request: Request, max_bytes: int
) -> Tuple[str, int]:
    """Stream ``request``'s body to ``<upload dir>/large/<sid>/`` and return
    ``(path, size)`` (#1430).

    Written as it arrives, buffered to :data:`_LARGE_FLUSH_BYTES` and flushed
    off the event loop, which keeps serving every live PTY meanwhile. A body
    over ``max_bytes`` is refused as soon as it is known to be over: from
    ``Content-Length`` before a byte is written, or mid-stream when there is
    none. Any failure, including the client going away mid-upload, deletes
    the partial file.
    """
    if max_bytes <= 0:
        raise HTTPException(status_code=400, detail="max_bytes must be positive")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise HTTPException(status_code=413, detail="file exceeds the large-file limit")
    target_dir = _large_dir(project_dir, sid)
    out = target_dir / _upload_name(filename)
    try:
        await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)
        handle = await asyncio.to_thread(open, out, "xb")
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"could not save file: {exc}")
    size = 0
    buf = bytearray()
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(status_code=413, detail="file exceeds the large-file limit")
            buf += chunk
            if len(buf) >= _LARGE_FLUSH_BYTES:
                data, buf = buf, bytearray()
                await asyncio.to_thread(handle.write, data)
        if buf:
            await asyncio.to_thread(handle.write, buf)
        await asyncio.to_thread(handle.close)
        if not size:
            raise HTTPException(status_code=400, detail="empty upload")
    except BaseException as exc:
        # BaseException: a client that disconnects mid-upload cancels this
        # task, and its partial file must go too.
        handle.close()
        _unlink_quietly(out)
        if isinstance(exc, OSError):
            raise HTTPException(status_code=400, detail=f"could not save file: {exc}")
        logger.info(f"ℹ️ large upload for {sid[:8]} abandoned after {size} B: {exc!r}")
        raise
    logger.info(f"ℹ️ large upload for {sid[:8]}: {size} B → {out}")
    await asyncio.to_thread(_prune_uploads, project_dir)
    return str(out), size


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(f"⚠️ could not remove {path}: {exc}")


def _prune_files(directory: Path, ttl_seconds: float, now: float) -> int:
    """Delete the regular files directly in ``directory`` whose mtime is more
    than ``ttl_seconds`` old; return how many went.

    Never recurses and never follows a link or junction: only files this
    process wrote live at this level. What it can't establish it keeps — an
    entry it can't stat, or one dated in the future, is not "expired".
    """
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return 0
    except OSError as exc:
        logger.warning(f"⚠️ upload prune: cannot list {directory}: {exc}")
        return 0
    removed = 0
    for entry in entries:
        try:
            info = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or now - info.st_mtime <= ttl_seconds:
                continue
            os.unlink(entry.path)
            removed += 1
        except OSError as exc:
            logger.warning(f"⚠️ upload prune: kept {entry.path}: {exc}")
    return removed


def _prune_uploads(project_dir: str, now: Optional[float] = None) -> int:
    """TTL-prune one project's upload dir (#1430): ordinary attachments past
    :data:`_UPLOAD_TTL_SECONDS`, large uploads past :data:`_LARGE_TTL_SECONDS`,
    then any per-session large dir left empty. Returns the files removed."""
    now = time.time() if now is None else now
    root = _upload_dir(project_dir)
    removed = _prune_files(root, _UPLOAD_TTL_SECONDS, now)
    large_root = root / _LARGE_DIR_NAME
    try:
        session_dirs = [
            Path(e.path) for e in os.scandir(large_root)
            if e.is_dir(follow_symlinks=False)
        ]
    except FileNotFoundError:
        session_dirs = []
    except OSError as exc:
        logger.warning(f"⚠️ upload prune: cannot list {large_root}: {exc}")
        session_dirs = []
    for session_dir in session_dirs:
        removed += _prune_files(session_dir, _LARGE_TTL_SECONDS, now)
        _rmdir_if_empty(session_dir)
    if removed:
        logger.info(f"🧹 upload prune: removed {removed} expired file(s) under {root}")
    return removed


def _rmdir_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass  # not empty (the agent left something there), or already gone


def _drop_session_uploads(project_dir: str, sid: str) -> int:
    """Session end (#1430): delete the large uploads ``sid`` took, i.e. the
    regular files directly in its leaf. A file the agent moved elsewhere is
    simply no longer there; anything the agent created inside the leaf (a
    subfolder it extracted into) is left alone, and so is the leaf then."""
    session_dir = _large_dir(project_dir, sid)
    removed = _prune_files(session_dir, -1, float("inf"))
    _rmdir_if_empty(session_dir)
    if removed:
        logger.info(f"🧹 session {sid[:8]} ended: removed {removed} large upload(s)")
    return removed


def _cleanup_ended_sessions(sessions: Iterable[Any]) -> None:
    """Upload cleanup for sessions the reaper just dropped: each one's large
    uploads, then a TTL prune of every project they worked in."""
    projects = set()
    for session in sessions:
        project_dir = getattr(session, "project_dir", "")
        sid = getattr(session, "session_id", "")
        if not project_dir or not sid:
            continue
        _drop_session_uploads(project_dir, sid)
        projects.add(project_dir)
    for project_dir in projects:
        _prune_uploads(project_dir)


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
