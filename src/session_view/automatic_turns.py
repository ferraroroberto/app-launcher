"""Which user turns in a transcript the launcher or the harness typed, not a person.

The chief's doorbell types one fixed line into its terminal each time the
mailbox has events, and the transcript records it as a user message with no
origin field. The Chat pane folds those turns (and the replies to them) behind a
divider, so the people-started conversation stays readable. This module is the
server half: it marks a ``user`` entry with ``auto`` — the origin name — only
when the origin is established, and leaves every other entry unmarked, which the
client reads as visible.

Three sources, all deterministic:

* the **injection ledger**: each time the launcher types input itself it appends
  a row (time, SHA-256 of the text, origin) to ``<session id>.injected.log``
  beside the session's audit logs. An entry matches a row with the same text
  that was written no later than the transcript line and not long before it,
  and each row is spent by one entry;
* an **exact match on** :data:`DOORBELL_LINE`, for rings from before the ledger
  existed. The line is launcher-owned and fixed by the mailbox contract;
* Claude's own ``isCompactSummary`` flag, set by the claude line reader.

A ledger that cannot be read is *unknown*, never empty: :func:`read_ledger`
returns ``None`` and no entry is marked from it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src import audit, runtime_files

logger = logging.getLogger(__name__)

#: The mailbox contract's fixed doorbell line (fleet-config ``docs/chief-mailbox.md``).
DOORBELL_LINE = "⟳ chief-inbox: pending — run chief_ops.py inbox"

ORIGIN_DOORBELL = "doorbell"
ORIGIN_COMPACT_SUMMARY = "compact-summary"

#: ``.log`` so the daily session-retention sweep ages the ledger out with the
#: session's other files.
LEDGER_SUFFIX = ".injected.log"

#: How long after the launcher typed a line the harness may write it into the
#: transcript. A busy agent takes a typed line only when its turn ends, so the
#: gap is bounded by a turn, not by a keystroke.
LEDGER_LAG_SECONDS = 15 * 60.0

_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]+$")

Row = Tuple[float, str, str]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ledger_path(session_id: str, directory: Optional[Path] = None) -> Optional[Path]:
    """Where ``session_id``'s ledger lives, or None for an id that is not a
    plain token (it would otherwise name a path outside the directory)."""
    if not _SAFE_SESSION_ID.match(session_id or ""):
        return None
    return (directory or audit.sessions_dir()) / f"{session_id}{LEDGER_SUFFIX}"


def record_injected(
    session_id: str,
    text: str,
    origin: str,
    *,
    directory: Optional[Path] = None,
    now: Optional[float] = None,
) -> bool:
    """Append one row for a line the launcher typed into ``session_id``.

    Returns whether the row was written; a failure is logged and never raised,
    because the line it describes was already delivered and a missing row only
    leaves that turn visible."""
    path = ledger_path(session_id, directory)
    if path is None:
        logger.warning("⚠️ injected-input ledger: unusable session id %r", session_id[:12])
        return False
    row = {"ts": time.time() if now is None else now, "sha256": _digest(text), "origin": origin}
    try:
        runtime_files.make_dirs(path.parent)
        with runtime_files.open_for_write(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError as exc:
        logger.warning("⚠️ injected-input ledger write failed for %s: %s", session_id[:8], exc)
        return False
    return True


def read_ledger(session_id: str, directory: Optional[Path] = None) -> Optional[List[Row]]:
    """The session's ledger rows, oldest first; ``[]`` when none was ever
    written; ``None`` when the file exists but cannot be read."""
    path = ledger_path(session_id, directory)
    if path is None:
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("⚠️ injected-input ledger unreadable for %s: %s", session_id[:8], exc)
        return None
    rows: List[Row] = []
    for line in raw.splitlines():
        try:
            obj = json.loads(line)
            rows.append((float(obj["ts"]), str(obj["sha256"]), str(obj["origin"])))
        except (ValueError, KeyError, TypeError):
            continue  # a torn or foreign line spends nothing and marks nothing
    rows.sort(key=lambda r: r[0])
    return rows


def _epoch(timestamp: Any) -> Optional[float]:
    if not isinstance(timestamp, str):
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def mark_automatic(
    session_id: str, *batches: Iterable[Dict[str, Any]], directory: Optional[Path] = None
) -> int:
    """Set ``auto`` on the ``user`` entries of ``batches`` whose origin is
    established, in place. Returns how many it marked.

    ``batches`` are the lists of one response (a page's entries, or a tail's
    settled and pending halves), taken together so one ledger row is spent once
    across them. An entry already carrying ``auto`` (a compact summary) is left
    alone, and a sidechain or capped entry is never matched against text.
    """
    marked = 0
    open_entries: List[Dict[str, Any]] = []
    for batch in batches:
        for entry in batch:
            if entry.get("kind") != "user" or entry.get("auto") or entry.get("sidechain"):
                continue
            text = entry.get("text")
            if not isinstance(text, str) or entry.get("truncated"):
                continue
            if text == DOORBELL_LINE:
                entry["auto"] = ORIGIN_DOORBELL
                marked += 1
            else:
                open_entries.append(entry)
    if not open_entries:
        return marked
    rows = read_ledger(session_id, directory)
    if not rows:
        return marked
    unspent: Dict[str, List[Row]] = {}
    for row in rows:
        unspent.setdefault(row[1], []).append(row)
    for entry in open_entries:
        when = _epoch(entry.get("timestamp"))
        waiting = unspent.get(_digest(entry["text"]))
        if when is None or not waiting:
            continue
        for i, (ts, _digest_hex, origin) in enumerate(waiting):
            if ts <= when <= ts + LEDGER_LAG_SECONDS:
                entry["auto"] = origin
                del waiting[i]
                marked += 1
                break
    return marked
