"""The fleet chief's journal, read for the Board's "since you last looked" card.

The chief appends one JSON line per event to ``chief-journal.jsonl`` beside its
plan file (fleet-config's ``docs/chief-journal.md`` owns the format; the writer
is ``chief_ops.py``). This module is the app's reader and, like
:mod:`src.chief.plan`, tolerant by contract: a missing or empty file is
``empty``, a file that cannot be read or holds no line it can trust is
``unreadable``, a line that does not parse is skipped, an unknown field is
dropped and an unknown ``kind`` passes through for the card's neutral chip.
Never an exception.

"Last look" is the moment Roberto last sent the chief a message from the Board
or submitted the answer sheet. It is kept here, once per machine, so every
device counts the same entries as new: ``webapp/chief-journal-look.json``,
gitignored, atomically replaced. Unlike the journal, the app writes it.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src import runtime_files
from src._json_io import atomic_write_json
from src.chief.plan import ref_url

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Module-level so tests redirect it at a tmp_path (tests/conftest.py).
CHIEF_JOURNAL_LOOK_FILE = PROJECT_ROOT / "webapp" / "chief-journal-look.json"

JOURNAL_FILE_NAME = "chief-journal.jsonl"
# The writer keeps 500 entries; this bounds what one response carries.
MAX_ENTRIES = 200
MAX_TEXT = 200
_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_KIND = re.compile(r"[a-z][a-z-]{0,23}")

_lock = threading.Lock()


def journal_path(plan_file: Path) -> Path:
    """The journal sits beside the plan file, as the writer places it."""
    return Path(plan_file).with_name(JOURNAL_FILE_NAME)


def _entry(raw: Any, owner: str) -> Optional[Dict[str, Any]]:
    """One journal line as the card's entry, or ``None`` when it can't be trusted."""
    if not isinstance(raw, dict):
        return None
    ts, kind = raw.get("ts"), raw.get("kind")
    if not (isinstance(ts, str) and _TS.fullmatch(ts)):
        return None
    if not (isinstance(kind, str) and _KIND.fullmatch(kind)):
        return None
    text, ref = raw.get("text"), raw.get("ref")
    text = " ".join(text.split())[:MAX_TEXT] if isinstance(text, str) else ""
    ref = ref.strip() if isinstance(ref, str) else ""
    return {"ts": ts, "kind": kind, "ref": ref, "text": text, "ref_url": ref_url(ref, "", owner)}


def read_last_look(path: Optional[Path] = None) -> str:
    """When Roberto last looked (``YYYY-MM-DDTHH:MM:SSZ``), or ``""`` for never
    or an unreadable record."""
    try:
        raw = json.loads(Path(path or CHIEF_JOURNAL_LOOK_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    except (OSError, ValueError) as exc:
        logger.warning("⚠️ chief journal look unreadable: %s", exc.__class__.__name__)
        return ""
    at = raw.get("at") if isinstance(raw, dict) else None
    return at if isinstance(at, str) and _TS.fullmatch(at) else ""


def record_look(now: Optional[datetime] = None, path: Optional[Path] = None) -> str:
    """Stamp "Roberto looked just now" and return the stamp.

    Raises :class:`OSError` when the file can't be written.
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    at = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    target = Path(path or CHIEF_JOURNAL_LOOK_FILE)
    with _lock:
        runtime_files.make_dirs(target.parent)
        atomic_write_json(target, {"at": at})
    logger.info("ℹ️ chief journal last look recorded: %s", at)
    return at


def read_journal(
    path: Path, github_owner: str = "", look_path: Optional[Path] = None
) -> Dict[str, Any]:
    """The journal as ``{"state": "ok", "entries": [...], "new_count": n,
    "last_look": ...}``, newest entry first, each flagged ``new`` when it is
    later than the last look; or ``{"state": "empty"}`` / ``{"state":
    "unreadable"}``.

    ``empty`` is no file or a file with no lines. ``unreadable`` is a file
    that can't be read, or one whose lines are all untrustworthy.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"state": "empty"}
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("⚠️ chief journal unreadable: %s", exc.__class__.__name__)
        return {"state": "unreadable"}
    lines = [line for line in raw.splitlines() if line.strip()]
    if not lines:
        return {"state": "empty"}
    entries: List[Dict[str, Any]] = []
    for line in lines:
        try:
            entry = _entry(json.loads(line), github_owner)
        except ValueError:
            continue
        if entry:
            entries.append(entry)
    if not entries:
        logger.warning("⚠️ chief journal holds no readable line")
        return {"state": "unreadable"}
    # The file is oldest first and sorted stable, so a tie keeps the later write first.
    entries.reverse()
    entries.sort(key=lambda e: e["ts"], reverse=True)
    entries = entries[:MAX_ENTRIES]
    last_look = read_last_look(look_path)
    for entry in entries:
        entry["new"] = entry["ts"] > last_look
    return {
        "state": "ok",
        "entries": entries,
        "new_count": sum(1 for e in entries if e["new"]),
        "last_look": last_look,
    }
