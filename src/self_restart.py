"""Restart this app from its own page: the server half of the page-foot restart.

Tapping the build readout in the page foot opens a sheet that posts here
(``app/webapp/static/_vendored/page-foot/restart-sheet.js``). This module runs
one of the app's declared restart recipes and records how it ended, so the
page can tell a finished restart from a failed or vanished one.

**Only declared recipes run.** The app passes its targets in code, each one a
fixed argv (normally :data:`TRAY_RESTART`, the repo's own ``tray.bat
--restart``). The client sends a target id and nothing else: never a command,
a path or a pid. This module kills nothing itself; the recipe owns that.

**The runner survives the app's exit.** ``tray.bat --restart`` kills the
tray's whole process tree with ``taskkill /T``, and this webapp is in that
tree. A plain child would die mid-restart, and ``DETACHED_PROCESS`` or
``CREATE_NEW_PROCESS_GROUP`` do not take it out of the tree; only
re-parenting does. So the runner starts through ``cmd /c start``: the
intermediate ``cmd`` exits, the runner has no live parent, and ``/T`` cannot
reach it. The runner inherits no handle but its standard ones, so it never
holds the app's listening socket (a port reclaim would otherwise kill it).

**The recipe runs from the path the app was launched from.** A fleet root
reached through a junction has two spellings, and the tray recipe finds its
processes by command line. :func:`launch_root` takes the checkout from this
process's own ``sys.prefix``, which keeps the spelling the tray used;
``Path.resolve()`` would swap it for the junction target.

**Every ending is recorded as its own state:** ``ok``, ``failed`` (non-zero
exit), ``timeout``, ``error`` (the recipe could not start), and ``unknown``
for a run still marked ``running`` past its timeout (the runner vanished).
None of them is folded into success. The record is ``<status_dir>/last.json``
and the recipe's output ``<status_dir>/last.log``.

**Auth belongs to the app.** :func:`restart_router` requires a ``guard``
dependency, the same one the app's other write routes use, and has no
default, so the routes cannot be mounted open. The POST also requires an
``Origin`` that matches the request's own host, so another site cannot post
a restart from a signed-in browser.

Wiring (in the app, not here):

    from src.runtime_data import runtime_data_dir
    from src.self_restart import TRAY_RESTART, RestartTarget, restart_router

    app.include_router(restart_router(
        targets=[RestartTarget("app", "Restart app", TRAY_RESTART)],
        status_dir=runtime_data_dir("my-app", create=True) / "restart",
        guard=require_auth,   # the dependency the app's write routes use
    ))

Vendor-verbatim: adopters copy this file byte-identical into their own
``src/`` (manifest key ``self_restart``). It imports nothing from the app or
from ``src.runtime_data``: the status folder is a call-site argument. The
runner is ``python -m src.self_restart run <spec>``. Stdlib-only at import;
FastAPI is imported only inside :func:`restart_router`.
"""

# No ``from __future__ import annotations`` here, deliberately: FastAPI reads
# the handlers' annotations at runtime, and ``Request`` is imported inside
# ``restart_router``, so a stringified annotation would not resolve.
# Python 3.9 stays supported: ``Optional[X]`` not ``X | None``.

import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: ``CREATE_NO_WINDOW`` on Windows, a no-op elsewhere. Derived locally so a
#: byte-identical copy resolves without ``src.no_window``.
_NO_WINDOW: int = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

#: The fleet tray's one canonical restart, run from the checkout root.
#: Spelled through ``cmd.exe /c`` because CreateProcess does not run a batch
#: file reliably on its own. The batch file is path-qualified because ``cmd``
#: skips the working directory when ``NoDefaultCurrentDirectoryInExePath`` is
#: set (this fleet's Windows host sets it), so a bare ``tray.bat`` is not found.
TRAY_RESTART: tuple[str, ...] = ("cmd.exe", "/c", ".\\tray.bat", "--restart")

#: A tray restart reclaims ports, starts the tray and waits for the new build
#: to answer; three minutes covers a cold start on a busy box.
DEFAULT_TIMEOUT_S = 180
#: How long past its timeout a ``running`` record is still believed before it
#: reads ``unknown``: the runner records the timeout itself, so a record still
#: running after this margin means the runner is gone.
_STALE_GRACE_S = 30
#: Lets the 202 answer leave before the recipe starts tearing the app down.
_START_DELAY_S = 1.0
#: Enough of the recipe's output to say why it failed, no more.
_LOG_TAIL_LINES = 20

#: A run's state: ``running`` until the runner records one of the final
#: states; ``unknown`` is never written, only derived (see
#: :func:`derive_restart_state`). Declared in ``.fleet.toml``
#: ``[[state_machines]]`` as this module's one state machine.
RestartState = Literal["running", "ok", "failed", "timeout", "error", "unknown"]
FINAL_STATES: tuple[RestartState, ...] = ("ok", "failed", "timeout", "error")
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


@dataclass(frozen=True)
class RestartTarget:
    """One declared restart: what the sheet offers and the argv it runs."""

    id: str
    label: str
    command: tuple[str, ...]
    #: Non-empty means the sheet asks a second time and shows this text.
    warning: str = ""
    timeout_s: int = DEFAULT_TIMEOUT_S


def launch_root() -> Path:
    """The checkout as this process's launch spelled it (see module docstring)."""
    if sys.prefix != sys.base_prefix:
        return Path(os.path.abspath(sys.prefix)).parent
    return Path(os.path.abspath(__file__)).parent.parent


def derive_restart_state(record: dict[str, Any], now: float) -> RestartState:
    """The record's state as of ``now``: a ``running`` one past its timeout is ``unknown``."""
    state = record.get("state")
    if state in FINAL_STATES:
        return state  # type: ignore[no-any-return]
    if state != "running":
        return "unknown"
    deadline = float(record.get("started_ts", 0)) + float(record.get("timeout_s", 0)) + _STALE_GRACE_S
    return "unknown" if now > deadline else "running"


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


class RestartStore:
    """The one restart record (``last.json``) and its output (``last.log``)."""

    def __init__(self, status_dir: Path) -> None:
        self.status_dir = Path(status_dir)
        self.record_path = self.status_dir / "last.json"
        self.spec_path = self.status_dir / "spec.json"
        self.log_path = self.status_dir / "last.log"

    def _write_json(self, path: Path, data: dict[str, Any]) -> None:
        # runtime-write-exempt: status_dir comes from the app's runtime_data_dir
        self.status_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        # runtime-write-exempt: under status_dir, from the app's runtime_data_dir
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)

    def read(self) -> Optional[dict[str, Any]]:
        try:
            data = json.loads(self.record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def write(self, record: dict[str, Any]) -> None:
        self._write_json(self.record_path, record)

    def write_spec(self, spec: dict[str, Any]) -> None:
        self._write_json(self.spec_path, spec)

    def current(self, now: Optional[float] = None) -> Optional[dict[str, Any]]:
        """The record, with a ``running`` one past its timeout read as ``unknown``."""
        record = self.read()
        if record is None:
            return None
        return dict(record, state=derive_restart_state(record, time.time() if now is None else now))

    def finish(self, restart_id: str, state: str, exit_code: Optional[int]) -> None:
        record = self.read() or {}
        if record.get("id") != restart_id:
            logger.warning("⚠️ restart record moved on before %s finished; leaving it", restart_id)
            return
        record.update(state=state, exit_code=exit_code, finished_at=_now_iso())
        self.write(record)

    def log_tail(self, lines: int = _LOG_TAIL_LINES) -> list[str]:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return [line for line in text.splitlines() if line.strip()][-lines:]


def spawn_detached(spec_path: Path, root: Path, python: Optional[str] = None) -> None:
    """Start the runner outside this process's tree (see module docstring)."""
    argv = [python or sys.executable, "-m", "src.self_restart", "run", str(spec_path)]
    devnull = subprocess.DEVNULL
    if sys.platform == "win32":
        # A string, not a list: ``start`` takes its window title as a bare
        # ``""``, which list2cmdline would escape into ``\"\"``.
        cmdline = 'cmd.exe /c start "" /b ' + subprocess.list2cmdline(argv)
        subprocess.Popen(
            cmdline, cwd=str(root), stdin=devnull, stdout=devnull, stderr=devnull,
            close_fds=True, creationflags=_NO_WINDOW,
        )
    else:
        subprocess.Popen(
            argv, cwd=str(root), stdin=devnull, stdout=devnull, stderr=devnull,
            close_fds=True, start_new_session=True, creationflags=_NO_WINDOW,
        )


def run_spec(spec_path: Path) -> str:
    """The runner: run the recipe in the spec, record how it ended, return the state."""
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    store = RestartStore(Path(spec_path).parent)
    restart_id = str(spec["id"])
    time.sleep(float(spec.get("delay_s", _START_DELAY_S)))
    exit_code: Optional[int] = None
    # runtime-write-exempt: under status_dir, from the app's runtime_data_dir
    with open(store.log_path, "w", encoding="utf-8") as out:
        try:
            done = subprocess.run(
                list(spec["command"]), cwd=spec["root"], stdin=subprocess.DEVNULL,
                stdout=out, stderr=subprocess.STDOUT, timeout=float(spec["timeout_s"]),
                creationflags=_NO_WINDOW, check=False,
            )
            exit_code = done.returncode
            state = "ok" if exit_code == 0 else "failed"
        except subprocess.TimeoutExpired:
            state = "timeout"
            out.write(f"restart recipe did not finish within {spec['timeout_s']}s\n")
        except OSError as exc:
            state = "error"
            out.write(f"restart recipe could not start: {exc}\n")
    store.finish(restart_id, state, exit_code)
    logger.info("ℹ️ restart %s (%s) ended: %s exit=%s", restart_id, spec.get("target"), state, exit_code)
    return state


def _same_origin(origin: Optional[str], host: Optional[str]) -> bool:
    if not origin or not host:
        return False
    return urlsplit(origin).netloc.lower() == host.lower()


def _check_targets(targets: Sequence[RestartTarget]) -> dict[str, RestartTarget]:
    by_id: dict[str, RestartTarget] = {}
    for target in targets:
        if not _ID_PATTERN.match(target.id):
            raise ValueError(f"restart target id {target.id!r} must match {_ID_PATTERN.pattern}")
        if target.id in by_id:
            raise ValueError(f"restart target id {target.id!r} is declared twice")
        if not target.command:
            raise ValueError(f"restart target {target.id!r} has no command")
        by_id[target.id] = target
    return by_id


def restart_router(
    *,
    targets: Sequence[RestartTarget],
    status_dir: Path,
    guard: Callable[..., Any],
    root: Optional[Path] = None,
    spawn: Callable[[Path, Path], None] = spawn_detached,
) -> Any:
    """``GET``/``POST /api/restart`` behind ``guard``; see the module docstring."""
    from fastapi import APIRouter, Depends, Request
    from fastapi.responses import JSONResponse

    by_id = _check_targets(targets)
    store = RestartStore(status_dir)
    checkout = root if root is not None else launch_root()
    router = APIRouter(dependencies=[Depends(guard)])

    def public(record: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if record is None:
            return None
        keys = ("id", "target", "state", "exit_code", "started_at", "finished_at")
        shown = {key: record.get(key) for key in keys}
        if shown["state"] in ("failed", "timeout", "error", "unknown"):
            shown["log_tail"] = store.log_tail()
        return shown

    @router.get("/api/restart")
    def restart_status() -> dict[str, Any]:
        return {
            "targets": [{"id": t.id, "label": t.label, "warning": t.warning} for t in targets],
            "last": public(store.current()),
        }

    @router.post("/api/restart")
    async def restart_start(request: Request) -> JSONResponse:
        if not _same_origin(request.headers.get("origin"), request.headers.get("host")):
            return JSONResponse({"error": "origin does not match this app"}, status_code=403)
        try:
            body = await request.json()
        except ValueError:
            body = None
        requested = body.get("target") if isinstance(body, dict) else None
        target = by_id.get(requested) if isinstance(requested, str) else None
        if target is None:
            return JSONResponse({"error": "unknown restart target"}, status_code=400)
        running = store.current()
        if running is not None and running.get("state") == "running":
            return JSONResponse(
                {"error": "a restart is already running", "restart_id": running.get("id")},
                status_code=409,
            )
        restart_id = uuid.uuid4().hex[:12]
        record = {
            "id": restart_id, "target": target.id, "state": "running",
            "started_at": _now_iso(), "started_ts": time.time(), "timeout_s": target.timeout_s,
        }
        store.write(record)
        store.write_spec({
            "id": restart_id, "target": target.id, "command": list(target.command),
            "root": str(checkout), "timeout_s": target.timeout_s,
        })
        try:
            spawn(store.spec_path, checkout)
        except OSError as exc:
            logger.error("❌ restart %s could not start its runner: %s", restart_id, exc)
            store.finish(restart_id, "error", None)
            return JSONResponse({"error": "could not start the restart", "restart_id": restart_id}, status_code=500)
        logger.info("ℹ️ restart %s (%s) started from %s", restart_id, target.id, checkout)
        return JSONResponse(
            {"restart_id": restart_id, "target": target.id, "started_at": record["started_at"]},
            status_code=202,
        )

    return router


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "run":
        sys.stderr.write("usage: python -m src.self_restart run <spec.json>\n")
        return 2
    spec_path = Path(args[1])
    # The runner is console-less and detached: its own breadcrumbs go to a
    # file beside the record, or a crash before ``finish`` would leave only an
    # ``unknown`` state with nothing to explain it.
    # runtime-write-exempt: beside the spec, under the app's runtime_data_dir
    logging.basicConfig(
        filename=str(spec_path.parent / "runner.log"), level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
    )
    try:
        state = run_spec(spec_path)
    except Exception:
        logger.exception("❌ restart runner failed before recording an ending")
        return 1
    return 0 if state == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
