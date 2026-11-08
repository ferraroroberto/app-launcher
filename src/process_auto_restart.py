"""Night auto-restart of an allowlisted process the hub flags.

The PC-health sheet restarts a leaking process on a tap (``process_restart``).
With ``process_auto_restart`` on, this loop does the same on its own, but only
inside the quiet window (default 21:00 to 05:00 local): restarting an audio app
at midday would cut a call or a recording, so daytime stays manual.

It restarts only when all of these hold, and logs the first that does not:
the switch is on, the local time is inside the window, the hub's verdict is
``watch`` or ``act`` (``unknown`` never counts), the verdict names a process on
the code allowlist, and that process has not already been restarted tonight.
A night is the window's start date, so 02:00 belongs to the evening before.
"Tonight" survives a webapp restart through a small state file next to
``webapp_config.json``, which also feeds the sheet's "Restarted automatically
at 02:14" line.

The live webapp's own logger is discarded by the tray, so the breadcrumbs go to
``launcher.slowreq`` (persisted to ``webapp/slow-requests.log``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from src import process_restart, restart_window, runtime_files
from src._json_io import atomic_write_json
from src.clients import hub_health
from src.webapp_config import DEFAULT_CONFIG_PATH
from src.webapp_settings.values import WEBAPP_CONFIG_PATH_ENV

breadcrumbs = logging.getLogger("launcher.slowreq")

TICK_SECONDS = 60.0
STATE_FILE_NAME = "process_auto_restart.json"
# How long the sheet keeps announcing an automatic restart.
SHOW_FOR = timedelta(hours=24)

_lock = threading.Lock()
_last_skip: Optional[str] = None
# Each restart id's last night restarted, held in memory beside the state file.
_tonight: Dict[str, str] = {}


def state_path() -> Path:
    """Next to the live ``webapp_config.json``, or the isolated copy the e2e gate points at."""
    env = os.environ.get(WEBAPP_CONFIG_PATH_ENV, "").strip()
    return (Path(env) if env else DEFAULT_CONFIG_PATH).with_name(STATE_FILE_NAME)


def _load() -> Dict[str, Dict[str, str]]:
    """Each restart id's last automatic restart (``night``, ``at``, ``outcome``).
    An unreadable file reads as empty, which can only allow one extra restart."""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    done = data.get("done") if isinstance(data, dict) else None
    return {k: v for k, v in done.items() if isinstance(v, dict)} if isinstance(done, dict) else {}


def _record(restart_id: str, night: str, now: datetime, outcome: str) -> None:
    """Remember the restart in memory first, so a state file that cannot be written
    never lets the next tick restart the same process again tonight."""
    with _lock:
        _tonight[restart_id] = night
        done = _load()
        done[restart_id] = {"night": night, "at": now.isoformat(timespec="minutes"), "outcome": outcome}
        path = state_path()
        try:
            runtime_files.make_dirs(path.parent)
            atomic_write_json(path, {"done": done})
        except OSError as exc:
            breadcrumbs.warning("⚠️ night auto-restart state not saved (%s); guarding in memory until the webapp restarts", exc)


def last_auto_restart(now: Optional[datetime] = None) -> Optional[Dict[str, str]]:
    """The newest automatic restart within :data:`SHOW_FOR`, for the sheet:
    ``owner``, ``at`` (HH:MM) and ``outcome``; None when there is none."""
    now = now or datetime.now()
    best: Optional[Dict[str, str]] = None
    for restart_id, rec in _load().items():
        try:
            when = datetime.fromisoformat(rec["at"])
        except (KeyError, ValueError):
            continue
        target = process_restart.get_target(restart_id)
        if target is None or not timedelta(0) <= now - when <= SHOW_FOR:
            continue
        if best is None or rec["at"] > best["_at"]:
            best = {"owner": target.owner, "at": when.strftime("%H:%M"),
                    "outcome": rec.get("outcome", ""), "_at": rec["at"]}
    if best is not None:
        best.pop("_at")
    return best


def _skip(reason: str) -> None:
    """One breadcrumb when the reason a tick did nothing changes, not one per tick."""
    global _last_skip
    if reason != _last_skip:
        _last_skip = reason
        breadcrumbs.info("🔕 night auto-restart skip: %s", reason)


def tick_once(
    cfg: Any,
    now: Optional[datetime] = None,
    *,
    read_health: Callable[[str], Dict[str, Any]] = hub_health.read,
    annotate: Callable[[Dict[str, Any]], Dict[str, Any]] = process_restart.annotate,
    restart: Callable[[str], Dict[str, Any]] = process_restart.restart,
) -> Optional[str]:
    """One decision. Returns the restart id it restarted, else None. Blocking."""
    global _last_skip
    now = now or datetime.now()
    if not getattr(cfg, "process_auto_restart", False):
        return _skip("switch is off")
    start, end = cfg.process_auto_restart_start, cfg.process_auto_restart_end
    if not restart_window.in_window(now, start, end):
        return _skip(f"outside the {start}-{end} window")
    health = annotate(read_health(cfg.llm_hub_url))
    if health.get("verdict") not in ("watch", "act"):
        return _skip(f"verdict is {health.get('verdict')}, not a warning")
    restart_id = health.get("restart_id")
    if not restart_id:
        return _skip("the flagged process is not on the restart allowlist")
    night = restart_window.night_of(now, start, end).isoformat()
    if night in (_tonight.get(restart_id), _load().get(restart_id, {}).get("night")):
        return _skip(f"{restart_id} was already restarted tonight")
    _last_skip = None
    try:
        result = restart(restart_id)
    except process_restart.RestartError as exc:
        return _skip(f"{restart_id} was not restarted: {exc}")
    outcome = result.get("relaunch") or "restarted"
    _record(restart_id, night, now, outcome)
    hub_health.invalidate()
    breadcrumbs.info("✅ night auto-restart of %s at %s: stopped %s, relaunch %s",
                     restart_id, now.strftime("%H:%M"), result.get("stopped"), outcome)
    return restart_id


async def tick_forever(app: Any) -> None:
    """The lifespan loop. Reads ``app.state.webapp_config`` every tick so a
    Settings save applies live."""
    while True:
        try:
            await asyncio.to_thread(tick_once, getattr(app.state, "webapp_config", None))
        except Exception:  # noqa: BLE001 — never take the lifespan down
            breadcrumbs.exception("❌ night auto-restart tick failed")
        await asyncio.sleep(TICK_SECONDS)
