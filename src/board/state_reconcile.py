"""Mark hook-state rows whose process is gone as ``ended``.

The hook writer removes a row on ``sessionEnd`` and prunes the rest after 24
hours, but a hard kill, a reboot or a session-host restart fires neither, so
dead rows pile up (75 on a slow machine, 72 of them ``needs-you``) and every
Board, session-list and transcript build keeps walking them. This pass checks
each row against the session-host's live list and any recorded PID, and stamps
the dead ones :data:`~src.board.state.STATUS_ENDED`;
:func:`~src.board.state.read_sessions_state` then leaves them out of ``rows``.

Each row lands in one of three verdicts, and only ``ended`` changes the file:

* ``live``: the session-host lists its ``launcher_session_id`` as alive, or its
  recorded ``pid`` is a running process.
* ``ended``: its ``launcher_session_id`` is absent from the session-host (the
  host is the authority on which PTYs it owns) or its recorded ``pid`` is gone,
  and nothing says it is alive.
* ``unknown``: nothing can be checked (no launcher id, no pid). It stays as it
  is, never ``idle`` or ``finished``.

An unreadable session-host list, an unreadable state file, or a row touched in
the last :data:`MIN_AGE_SECONDS` changes nothing: not being able to tell is not
"gone". It runs on a slow timer and once shortly after startup, never inside a
poll.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src._json_io import atomic_write_json
from src.board.state import STATUS_ENDED, iso_z, parse_iso, utc_now
from src.clients import session as session_client
from src.diagnostics import is_pid_alive
from src.request_timing import TimedExecutor

logger = logging.getLogger(__name__)

VERDICT_LIVE = "live"
VERDICT_ENDED = "ended"
VERDICT_UNKNOWN = "unknown"

FIRST_DELAY_SECONDS = 20.0
TICK_SECONDS = 300.0
# A row is written after its session exists, but a list read a moment earlier
# than the row can still miss a session started in between; a row this fresh is
# left for the next pass.
MIN_AGE_SECONDS = 120.0
# The tick's own thread: a stalled state-file write or session-host call then
# stalls only the reconcile, never a request on the loop's shared default pool.
_TICK_POOL = TimedExecutor("state-reconcile-tick", max_workers=1)


@dataclass(frozen=True)
class Reconciled:
    """What one pass found. ``skipped`` names why nothing was judged, else ``None``."""

    live: int = 0
    ended: int = 0
    unknown: int = 0
    skipped: Optional[str] = None


def _recorded_pid(row: Dict[str, Any]) -> Optional[int]:
    pid = row.get("pid")
    return pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else None


def classify_row(
    row: Dict[str, Any],
    live_session_ids: "set[str]",
    *,
    pid_alive: Callable[[int], bool] = is_pid_alive,
) -> str:
    """``live`` / ``ended`` / ``unknown`` for one hook-state row."""
    launcher_sid = str(row.get("launcher_session_id") or "")
    if launcher_sid and launcher_sid in live_session_ids:
        return VERDICT_LIVE
    pid = _recorded_pid(row)
    if pid is not None:
        try:
            if pid_alive(pid):
                return VERDICT_LIVE
        except Exception:  # noqa: BLE001 -- a probe that fails could not tell
            return VERDICT_UNKNOWN
        return VERDICT_ENDED
    return VERDICT_ENDED if launcher_sid else VERDICT_UNKNOWN


def _read_raw(path: Path) -> Optional[Dict[str, Any]]:
    """The file's rows, or ``None`` when it cannot be read as a JSON object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _fresh(row: Dict[str, Any], now: datetime) -> bool:
    stamp = parse_iso(row.get("updated_at"))
    return stamp is None or (now - stamp).total_seconds() < MIN_AGE_SECONDS


def reconcile_rows(
    path: Path,
    list_live: Callable[[], List[Dict[str, Any]]],
    *,
    now: Optional[datetime] = None,
    pid_alive: Callable[[int], bool] = is_pid_alive,
) -> Reconciled:
    """Stamp every provably dead row in ``path`` as ended; returns what it found.

    ``list_live`` is called after the rows are read, so a session whose row is
    in the snapshot was already running when the list was taken.
    """
    moment = now or utc_now()
    rows = _read_raw(path)
    if rows is None:
        return Reconciled(skipped="state file unreadable or absent")
    try:
        live = list_live()
    except session_client.SessionHostError as exc:
        return Reconciled(skipped=f"session-host list unreadable: {exc}")
    live_ids = {
        str(sess.get("session_id")) for sess in live if sess.get("alive") and sess.get("session_id")
    }

    counts = {VERDICT_LIVE: 0, VERDICT_ENDED: 0, VERDICT_UNKNOWN: 0}
    to_end: Dict[str, Optional[str]] = {}
    for sid, row in rows.items():
        if not isinstance(row, dict) or row.get("status") == STATUS_ENDED:
            continue
        if _fresh(row, moment):
            counts[VERDICT_LIVE] += 1
            continue
        verdict = classify_row(row, live_ids, pid_alive=pid_alive)
        counts[verdict] += 1
        if verdict == VERDICT_ENDED:
            to_end[sid] = row.get("updated_at")

    ended = _stamp_ended(path, to_end, moment) if to_end else 0
    # Rows another writer touched between the read and the write stay as they are.
    counts[VERDICT_LIVE] += len(to_end) - ended
    return Reconciled(
        live=counts[VERDICT_LIVE], ended=ended, unknown=counts[VERDICT_UNKNOWN],
    )


def _stamp_ended(path: Path, candidates: Dict[str, Optional[str]], moment: datetime) -> int:
    """Stamp ``ended`` on each candidate row that no hook touched since it was judged.

    Re-reads the file right before the write so a hook event landing during the
    pass is kept: a row whose ``updated_at`` moved is a session that spoke up.
    """
    fresh = _read_raw(path)
    if fresh is None:
        return 0
    changed = 0
    for sid, judged_at in candidates.items():
        row = fresh.get(sid)
        if isinstance(row, dict) and row.get("updated_at") == judged_at and row.get("status") != STATUS_ENDED:
            fresh[sid] = {**row, "status": STATUS_ENDED, "ended_at": iso_z(moment)}
            changed += 1
    if changed:
        atomic_write_json(path, fresh)
    return changed


def reconcile_from_config(cfg: Any) -> Reconciled:
    """One pass over the state file ``cfg`` names, against its session-host."""
    result = reconcile_rows(
        Path(cfg.sessions_state_file),
        lambda: session_client.list_sessions(cfg.session_host_port),
    )
    if result.skipped:
        logger.info(f"ℹ️ session state reconcile undetermined, nothing changed: {result.skipped}")
    elif result.ended:
        logger.info(
            f"🧹 session state reconcile: {result.ended} dead row(s) marked ended, "
            f"{result.live} live, {result.unknown} unknown"
        )
    else:
        logger.debug(
            f"session state reconcile: nothing ended ({result.live} live, {result.unknown} unknown)"
        )
    return result


async def tick_forever(get_cfg: Callable[[], Any]) -> None:
    """The lifespan loop, canonical instance only: a gate webapp shares no real state file.

    ``get_cfg`` is re-read every pass so a Settings save moves the paths without a restart.
    """
    await asyncio.sleep(FIRST_DELAY_SECONDS)
    while True:
        cfg = get_cfg()
        if cfg is not None:
            try:
                await asyncio.get_running_loop().run_in_executor(_TICK_POOL, reconcile_from_config, cfg)
            except Exception:  # noqa: BLE001 -- never take the lifespan down
                logger.exception("❌ session state reconcile failed")
        await asyncio.sleep(TICK_SECONDS)
