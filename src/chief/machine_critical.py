"""Publish a machine in trouble into the chief's mailbox.

One ``kind: "machine-critical"`` v2 event per host + metric per incident, in
the format fleet-config ``docs/chief-mailbox.md`` defines; nothing here imports
from fleet-config. The hub's health watch judges (:mod:`src.clients.hub_health`
reads its verdict); this module only turns "``act`` on a critical metric" into
one file the doorbell can ring on.

An incident is a host + metric from the poll that first sees an ``act`` finding
on it until a poll that reads the verdict and no longer finds it. The open
incidents and their ``first_seen`` are kept in :data:`STATE_FILE`, so a webapp
restart in the middle of an incident publishes nothing new.

A verdict that could not be read (hub off, down, or itself ``unknown``) neither
publishes nor clears: "not confirmed" is not "fine", and an incident that is
only unreadable for a minute must not publish a second event when it is read
again. The event is written whether or not a chief is running, because the
chief learns of an open incident when it starts. Only the canonical instance
writes the real mailbox, and the lifespan loop starts only there.

Best-effort: every failure is logged and swallowed.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import psutil

from src import instance_role, runtime_files, system_load
from src._json_io import atomic_write_json, read_json_or
from src.chief_session_end import EVENT_VERSION
from src.chief_session_end import publish as publish_event
from src.clients import hub_health

logger = logging.getLogger(__name__)

KIND = "machine-critical"
EVENT_NAME = "MachineCritical"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Module-level so tests redirect it at a tmp_path (tests/conftest.py).
STATE_FILE = PROJECT_ROOT / "webapp" / "machine-critical-open.json"
# The hub's port watch samples every 30 s; the same beat as the hub reading's
# cache, so a tick is one fresh read and never a second request inside it.
TICK_SECONDS = hub_health.OK_CACHE_SECONDS
_MAX_OFFENDERS = 5
_WORKTREE_SUFFIX = re.compile(r"-wt-\d+$")
# Only an interpreter running a repo's script is that repo's app; any other
# process that happens to sit in a repo folder (an agent CLI) is not.
_APP_INTERPRETERS = frozenset({"python", "pythonw", "py", "node"})
# The offenders belong to a metric's resource: ports for the port metrics and
# the exhaustion events, memory for commit charge. The hub names no process
# behind a saturated disk, so a disk_load event carries none rather than the
# port holders, who are not its cause.
_OFFENDER_KEYS = {"commit_percent": "memory_holders", "disk_load": None}

OWNER_FLEET = "fleet"
OWNER_NON_FLEET = "non-fleet"
OWNER_UNKNOWN = "unknown"

_lock = threading.Lock()
_last_unreadable = False


def _stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_open(path: Optional[Path] = None) -> Dict[str, str]:
    """The open incidents as ``{"<host>/<metric>": first_seen}``; ``{}`` when
    nothing readable is on file."""
    target = Path(path or STATE_FILE)
    raw = read_json_or(target, {}, "machine-critical open incidents", expect=dict)
    return {str(k): v for k, v in raw.items() if isinstance(v, str)}


def _image_stem(name: str) -> str:
    stem = name.rsplit("\\", 1)[-1].rsplit("/", 1)[-1].lower()
    return stem[:-4] if stem.endswith(".exe") else stem


def _live_name(pid: int) -> Optional[str]:
    try:
        return psutil.Process(pid).name()
    except (psutil.Error, OSError):
        return None


def classify(
    row: Dict[str, Any],
    projects_dir: str,
    *,
    live_name: Callable[[int], Optional[str]] = _live_name,
    owner_folder: Optional[Callable[[int], Optional[str]]] = None,
) -> Dict[str, Any]:
    """One offender row as the contract spells it, with the launcher's own
    ``owner`` / ``repo`` classification.

    ``fleet`` only for an interpreter whose script or folder is a repo under
    ``projects_dir``; a live process that is something else is ``non-fleet``;
    a process that is gone, was replaced under the same pid, or cannot be read
    is ``unknown``, never guessed.
    """
    name, pid = row.get("name"), row.get("pid")
    out: Dict[str, Any] = {
        "name": name, "pid": int(pid) if isinstance(pid, (int, float)) else None,
        "owner": OWNER_UNKNOWN, "repo": None, "value": row.get("value"),
    }
    if out["pid"] is None or not name:
        return out
    live = live_name(out["pid"])
    if live is None or _image_stem(live) != _image_stem(str(name)):
        return out
    out["owner"] = OWNER_NON_FLEET
    if _image_stem(live) not in _APP_INTERPRETERS:
        return out
    folder = (owner_folder or system_load.process_owner)(out["pid"])
    repo = _WORKTREE_SUFFIX.sub("", folder) if folder else ""
    if repo and projects_dir and (Path(projects_dir) / repo).is_dir():
        out["owner"], out["repo"] = OWNER_FLEET, repo
    return out


def build_event(
    cfg: Any,
    host: str,
    finding: Dict[str, Any],
    machine: Dict[str, Any],
    first_seen: str,
    now: datetime,
    *,
    classify_row: Callable[..., Dict[str, Any]] = classify,
) -> Dict[str, Any]:
    key = _OFFENDER_KEYS.get(finding["metric"], "holders")
    rows = (machine.get(key) or [])[:_MAX_OFFENDERS] if key else []
    return {
        "v": EVENT_VERSION,
        "id": uuid.uuid4().hex,
        "kind": KIND,
        "event": EVENT_NAME,
        "host": host,
        "metric": finding["metric"],
        "value": finding.get("value"),
        # The hub's payload names no threshold, so none is invented: a reader
        # renders the missing field as "-".
        "threshold": None,
        "offenders": [classify_row(r, getattr(cfg, "projects_dir", "")) for r in rows],
        "verdict_url": hub_health.health_url(cfg.llm_hub_url),
        "first_seen": first_seen,
        "launcher_sid": None,
        "repo": None,
        "number": None,
        "agent": None,
        "dispatch_id": None,
        "ts": _stamp(now),
    }


def _note_readability(readable: bool) -> None:
    """One breadcrumb when readability flips, never one per tick."""
    global _last_unreadable
    if not readable and not _last_unreadable:
        logger.info("ℹ️ machine-critical watch: hub verdict not confirmed, incidents left as they were")
    _last_unreadable = not readable


def observe(
    cfg: Any,
    reading: Dict[str, Any],
    now: Optional[datetime] = None,
    *,
    path: Optional[Path] = None,
    classify_row: Callable[..., Dict[str, Any]] = classify,
) -> List[Path]:
    """Open, publish and close incidents from one hub reading; returns the paths
    published. Blocking (it inspects processes and writes files)."""
    canonical, reason = instance_role.canonical_instance()
    if not canonical:
        logger.info(f"ℹ️ machine-critical event skipped: non-canonical instance ({reason})")
        return []
    moment = now or datetime.now(timezone.utc)
    target = Path(path or STATE_FILE)
    machine = reading.get("machine") if isinstance(reading, dict) else None
    readable = isinstance(reading, dict) and reading.get("verdict") in hub_health.VERDICTS
    _note_readability(readable)
    if not readable:
        return []
    host = (machine or {}).get("host") or "unknown"
    current = {f["metric"]: f for f in (machine or {}).get("critical") or []}
    published: List[Path] = []
    with _lock:
        before = read_open(target)
        # A finding that is gone from a readable verdict closes its incident.
        # Another host's incident (a renamed host) is not this reading's to close.
        opened = {k: v for k, v in before.items()
                  if not k.startswith(f"{host}/") or k.split("/", 1)[1] in current}
        for metric, finding in current.items():
            key = f"{host}/{metric}"
            if key in opened:
                continue
            first_seen = _stamp(moment)
            try:
                event = build_event(cfg, host, finding, machine or {}, first_seen, moment, classify_row=classify_row)
                published.append(publish_event(Path(cfg.chief_inbox_dir), event))
            except Exception as exc:  # noqa: BLE001 — best effort, retried on the next tick
                logger.warning(f"⚠️ machine-critical event for {key} not published: {exc}")
                continue
            opened[key] = first_seen
            logger.info(f"✅ machine-critical event for {key} → {published[-1].name}")
        if opened != before:
            try:
                runtime_files.make_dirs(target.parent)
                atomic_write_json(target, opened)
            except (OSError, ValueError) as exc:
                logger.warning(f"⚠️ machine-critical open incidents not saved: {exc}")
    return published


def tick_once(cfg: Any, now: Optional[datetime] = None) -> List[Path]:
    """One poll of the hub's verdict through the shared cache, then :func:`observe`."""
    if cfg is None or not (getattr(cfg, "llm_hub_url", "") or "").strip():
        return []
    return observe(cfg, hub_health.read(cfg.llm_hub_url), now)


async def tick_forever(app: Any) -> None:
    """The lifespan loop. Reads ``app.state.webapp_config`` every tick so a
    Settings save applies live."""
    while True:
        try:
            await asyncio.to_thread(tick_once, getattr(app.state, "webapp_config", None))
        except Exception:  # noqa: BLE001 — never take the lifespan down
            logger.exception("❌ machine-critical tick failed")
        await asyncio.sleep(TICK_SECONDS)


def reset() -> None:
    """Drop in-memory state. Tests only."""
    global _last_unreadable
    _last_unreadable = False
