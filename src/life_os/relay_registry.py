"""Conversations the Telegram relay reopened for a bot, and the ones it closed.

The relay's ``stop`` (and the idle close built on it) may act only on a
session listed here, so this file is the line between "the relay reopened
this for a bot" and every other session on the PC. ``resume`` adds a session
only when it actually launched one (state ``resumed``). A conversation found
already open (``opened``), for instance one the owner has open in the app, is
never added, so the relay never closes what it did not open. Life OS sessions
started from the app never pass through here at all.

Store: ``relay-registry.json`` in the launcher's runtime-data directory,
atomically replaced, every read-modify-write under one lock (the webapp is the
only writer, in one process). Shape::

    {"entries": [{"profile", "chat", "session_id", "conversation", "title",
                  "opened_at", "last_activity"}, ...],
     "stops":   [{"profile", "chat", "session_id", "conversation", "title",
                  "reason", "stopped_at"}, ...]}

Times are epoch seconds. ``chat`` is the bot's opaque per-chat key, ``""``
when the bot passed none. An entry whose session is no longer live is pruned
whenever a caller hands in the live session ids. ``stops`` holds the closes
not yet reported to the bot: :func:`take_stops` returns and drops them.

Degradation: a missing or unreadable file reads as empty, which only ever
makes ``stop`` refuse (``not_relay_owned``), never act. A failed write raises
:class:`OSError` to the route.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src._json_io import atomic_write_json
from src.runtime_data import runtime_data_dir

logger = logging.getLogger(__name__)

REGISTRY_NAME = "relay-registry.json"
# Unreported stops are a short backlog between two status calls; the cap
# keeps a bot that never asks from growing the file without bound.
MAX_STOPS = 50

_lock = threading.Lock()


def registry_path() -> Path:
    # Resolved per call so a test's APP_LAUNCHER_DATA_DIR override applies.
    return runtime_data_dir("app-launcher", create=True) / REGISTRY_NAME


def _read(path: Path) -> Dict[str, List[Dict[str, Any]]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError) as exc:
        logger.warning(f"⚠️ relay registry unreadable ({exc.__class__.__name__}); read as empty")
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {key: [row for row in raw.get(key) or [] if isinstance(row, dict)]
            for key in ("entries", "stops")}


def _prune(data: Dict[str, List[Dict[str, Any]]], live: Iterable[str]) -> bool:
    alive = set(live)
    kept = [e for e in data["entries"] if str(e.get("session_id")) in alive]
    pruned = len(kept) != len(data["entries"])
    if pruned:
        logger.info(f"ℹ️ relay registry: pruned {len(data['entries']) - len(kept)} "
                    "entry(ies) whose session ended")
        data["entries"] = kept
    return pruned


def entries(profile: str, live: Iterable[str], *, chat: Optional[str] = None
            ) -> List[Dict[str, Any]]:
    """``profile``'s entries whose session is in ``live``, optionally one
    chat's. Entries of any profile whose session ended are pruned."""
    with _lock:
        path = registry_path()
        data = _read(path)
        if _prune(data, live):
            _write_best_effort(path, data, "prune")
    return [dict(e) for e in data["entries"] if e.get("profile") == profile
            and (chat is None or e.get("chat") == chat)]


def find(profile: str, session_id: str) -> Optional[Dict[str, Any]]:
    """``profile``'s entry for ``session_id``, live or not (no pruning)."""
    with _lock:
        data = _read(registry_path())
    return next((dict(e) for e in data["entries"] if e.get("profile") == profile
                 and str(e.get("session_id")) == session_id), None)


def add(profile: str, chat: str, session_id: str, conversation: str, title: str,
        now: float) -> None:
    """Record a conversation the relay just launched for ``profile``."""
    with _lock:
        path = registry_path()
        data = _read(path)
        data["entries"] = [e for e in data["entries"]
                           if str(e.get("session_id")) != session_id]
        data["entries"].append({
            "profile": profile, "chat": chat, "session_id": session_id,
            "conversation": conversation, "title": title,
            "opened_at": now, "last_activity": now,
        })
        atomic_write_json(path, data)


def _write_best_effort(path: Path, data: Dict[str, List[Dict[str, Any]]], what: str) -> None:
    # A prune or activity stamp that fails to land is redone by the next call;
    # it must not fail the relay call it rides on.
    try:
        atomic_write_json(path, data)
    except OSError as exc:
        logger.warning(f"⚠️ relay registry {what} not saved ({exc.__class__.__name__})")


def touch(profile: str, session_id: str, now: float) -> bool:
    """Move an entry's ``last_activity`` to ``now``; ``False`` when it has none."""
    with _lock:
        path = registry_path()
        data = _read(path)
        hit = False
        for entry in data["entries"]:
            if entry.get("profile") == profile and str(entry.get("session_id")) == session_id:
                entry["last_activity"] = now
                hit = True
        if hit:
            _write_best_effort(path, data, "activity stamp")
    return hit


def forget(profile: str, session_id: str) -> None:
    """Drop an entry whose session ended without a stop."""
    with _lock:
        path = registry_path()
        data = _read(path)
        kept = [e for e in data["entries"] if not (
            e.get("profile") == profile and str(e.get("session_id")) == session_id)]
        if len(kept) != len(data["entries"]):
            data["entries"] = kept
            _write_best_effort(path, data, "prune")


def record_stop(profile: str, session_id: str, reason: str, now: float) -> None:
    """Move an entry to the unreported stops, with ``reason`` (``done``, ``idle``)."""
    with _lock:
        path = registry_path()
        data = _read(path)
        entry = next((e for e in data["entries"] if e.get("profile") == profile
                      and str(e.get("session_id")) == session_id), None)
        if entry is None:
            return
        data["entries"].remove(entry)
        data["stops"].append({
            **{key: entry.get(key, "") for key in
               ("profile", "chat", "session_id", "conversation", "title")},
            "reason": reason, "stopped_at": now,
        })
        data["stops"] = data["stops"][-MAX_STOPS:]
        atomic_write_json(path, data)


def take_stops(profile: str, *, chat: Optional[str] = None) -> List[Dict[str, Any]]:
    """``profile``'s unreported stops (one chat's, when given), oldest first,
    removed from the file so the next call reports only newer ones."""
    with _lock:
        path = registry_path()
        data = _read(path)
        mine = [s for s in data["stops"] if s.get("profile") == profile
                and (chat is None or s.get("chat") == chat)]
        if mine:
            data["stops"] = [s for s in data["stops"] if s not in mine]
            # Unsaved, the same stops come back once more on the next call.
            _write_best_effort(path, data, "report")
    return mine
