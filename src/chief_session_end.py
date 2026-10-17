"""Publish a chief ``session-end`` event for a session that died without a hook (#1644).

A worker the fleet chief dispatched reports its end through its own ``Stop`` /
``SessionEnd`` hook, which writes an event into the chief's mailbox. A worker
killed too hard to run any hook leaves nothing. The session-host's reaper
already notices every dead session, so it calls :func:`publish_session_ends`
with the sessions it just dropped; each one the chief registry names gets one
``session-end`` event.

The mailbox contract is fleet-config's ``docs/chief-mailbox.md``: one file per
event, written to a temp file in the same directory (``.event.<random>.tmp``)
and ``os.replace``d to ``<published_ms>-<id>.json``, so a reader never sees a
partial ``.json``. Nothing is imported from fleet-config; the file format is the
whole interface.

"Could not tell" is not "not managed": an unreadable or malformed registry
logs an *undetermined* line and writes nothing. A missing registry file is a
determination (nothing was ever dispatched), and stays quiet.

A worker whose own ``SessionEnd`` hook already fired yields a second event for
the same sid and kind. That is harmless; the chief dedups by both.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

EVENT_VERSION = 2
KIND_SESSION_END = "session-end"
SOURCE_REAPER = "reaper"
#: v1-compatible event name, so ``wait-event`` still names the event.
LEGACY_EVENT_NAME = "SessionEnd"


#: ``src.webapp_config.WEBAPP_CONFIG_PATH_ENV`` and ``DEFAULT_CONFIG_PATH``. Not
#: imported: ``webapp_config`` pulls in ``model_catalog``, so every edit to
#: either would mark the long-lived session-host stale. A test pins them equal.
CONFIG_PATH_ENV = "LAUNCHER_WEBAPP_CONFIG"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "webapp_config.json"

#: Mirror of ``WebappConfig.chief_managed_file`` / ``chief_inbox_dir`` defaults.
DEFAULT_STATE_DIR = Path.home() / ".claude" / "hooks" / "state"
REGISTRY_KEY = "chief_managed_file"
INBOX_KEY = "chief_inbox_dir"


def resolve_paths() -> Tuple[Path, Path]:
    """``(registry file, inbox dir)`` from ``webapp_config.json``, else the
    user-scope defaults. A missing, unreadable or non-object config file, or a
    key that is not a non-empty string, falls back to the default for that
    path: the defaults are the contract's own location."""
    config_path = Path(os.environ.get(CONFIG_PATH_ENV, "").strip() or DEFAULT_CONFIG_PATH)
    raw: Any = {}
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        logger.warning(f"⚠️ chief session-end: {config_path} unreadable ({exc}); using default paths")
    if not isinstance(raw, dict):
        raw = {}

    def pick(key: str, default: Path) -> Path:
        value = raw.get(key)
        return Path(value) if isinstance(value, str) and value.strip() else default

    return (
        pick(REGISTRY_KEY, DEFAULT_STATE_DIR / "chief-managed.json"),
        pick(INBOX_KEY, DEFAULT_STATE_DIR / "chief-inbox"),
    )


def _read_registry(registry: Path) -> Optional[Dict[str, Any]]:
    """The chief-managed rows keyed by launcher sid.

    ``{}`` when the file is absent (a determination: nothing dispatched).
    ``None`` when it cannot be established: unreadable, not JSON, or not an
    object.
    """
    try:
        raw = registry.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.warning(f"⚠️ chief session-end undetermined: cannot read {registry} ({exc})")
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        logger.warning(f"⚠️ chief session-end undetermined: {registry} is not JSON ({exc})")
        return None
    if not isinstance(data, dict):
        logger.warning(f"⚠️ chief session-end undetermined: {registry} is not a JSON object")
        return None
    return data


def _number(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _event(sid: str, row: Dict[str, Any], agent: str, now: datetime) -> Dict[str, Any]:
    dispatch_id = row.get("dispatch_id")
    repo = row.get("repo")
    return {
        "v": EVENT_VERSION,
        "id": uuid.uuid4().hex,
        "kind": KIND_SESSION_END,
        "event": LEGACY_EVENT_NAME,
        "launcher_sid": sid,
        "repo": repo if isinstance(repo, str) else None,
        "number": _number(row.get("number")),
        "agent": agent,
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dispatch_id": dispatch_id if isinstance(dispatch_id, str) and dispatch_id else None,
        "excerpt": None,
        "excerpt_source": None,
        "excerpt_unavailable": "session died without running a hook",
        "source": SOURCE_REAPER,
    }


def _publish(inbox: Path, event: Dict[str, Any], now_ms: int) -> Path:
    """Write ``event`` into ``inbox`` atomically; returns the published path."""
    inbox.mkdir(parents=True, exist_ok=True)
    tmp = inbox / f".event.{secrets.token_hex(8)}.tmp"
    final = inbox / f"{now_ms}-{event['id']}.json"
    try:
        tmp.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, final)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return final


def publish_session_ends(
    sessions: Iterable[Any],
    *,
    registry: Path,
    inbox: Path,
    now: Optional[datetime] = None,
) -> List[Path]:
    """Publish one ``session-end`` event per reaped session the registry names.

    Per-event write failures are logged and skipped; the reaper keeps running.
    Returns the published paths.
    """
    reaped = [s for s in sessions if getattr(s, "session_id", "")]
    if not reaped:
        return []
    rows = _read_registry(registry)
    if rows is None:
        sids = ", ".join(s.session_id[:8] for s in reaped)
        logger.warning(
            f"⚠️ chief session-end undetermined for {sids}: "
            f"registry unreadable, nothing published"
        )
        return []
    moment = now or datetime.now(timezone.utc)
    now_ms = int(moment.timestamp() * 1000)
    published: List[Path] = []
    for session in reaped:
        sid = session.session_id
        row = rows.get(sid)
        if row is None:
            continue
        event = _event(
            sid, row if isinstance(row, dict) else {}, getattr(session, "agent", ""), moment
        )
        try:
            published.append(_publish(inbox, event, now_ms))
        except OSError as exc:
            logger.warning(f"❌ chief session-end for {sid[:8]} not published: {exc}")
            continue
        logger.info(
            f"✅ chief session-end published for {sid[:8]} ({event['repo']}#{event['number']})"
        )
    return published
