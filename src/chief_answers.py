"""Which of the chief's questions the Board already answered (#1487).

The answer sheet (#1295) sends one message to the chief, and the chief removes
the items it has taken by rewriting its plan. Until it does, the Board marks
the sent items "answered, waiting for the chief". Those marks used to live in
the answering browser's localStorage, so another device showed the same
questions as open and invited a second set of answers. They live here now,
once per machine, so every device renders the same state.

Store: ``webapp/chief-answered.json``, gitignored, atomically replaced — the
launcher's own runtime state, beside :mod:`src.chief_pointer`'s sidecar. The
app still never writes the chief's plan file; this is a separate record.

Shape: ``{"updated_at": <the plan's updated_at>, "ids": [<item key>, ...]}``.
An item's key is the chief's own ``id``, else its position in the list (the
sheet's ``itemKey``). The marks hold only for the plan version they were made
on: once the chief rewrites the plan (its ``updated_at`` moves) they read as
none, and the next record replaces them.

Degradation contract: missing, corrupt or for another plan version reads as
``[]`` — never an exception. A failed write raises :class:`OSError` to the
route, which says so to the device that answered.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, List, Optional

from src._json_io import atomic_write_json

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Module-level so tests redirect it at a tmp_path (tests/conftest.py).
CHIEF_ANSWERED_FILE = PROJECT_ROOT / "webapp" / "chief-answered.json"

# A plan holds a handful of questions; these bound what one request can store.
MAX_IDS = 50
MAX_ID_LEN = 200

# Two devices answering at once must union, not overwrite each other.
_lock = threading.Lock()


def clean_ids(value: Any) -> Optional[List[str]]:
    """``value`` as a list of item keys, or ``None`` when it isn't a list of
    strings. Blank and duplicate keys drop; the list is capped at
    :data:`MAX_IDS` keys of :data:`MAX_ID_LEN` characters."""
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    ids: List[str] = []
    for raw in value:
        key = raw.strip()[:MAX_ID_LEN]
        if key and key not in ids:
            ids.append(key)
    return ids[:MAX_IDS]


def _read(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("⚠️ chief answered marks unreadable: %s", exc.__class__.__name__)
        return {}
    if not isinstance(raw, dict) or not isinstance(raw.get("updated_at"), str):
        return {}
    return raw


def read_answered(updated_at: str, path: Optional[Path] = None) -> List[str]:
    """The keys answered on the plan version ``updated_at``; ``[]`` for any
    other version, or when there is nothing (readable) on file."""
    raw = _read(Path(path or CHIEF_ANSWERED_FILE))
    if raw.get("updated_at") != updated_at:
        return []
    return clean_ids(raw.get("ids")) or []


def record_answered(
    updated_at: str, ids: List[str], path: Optional[Path] = None
) -> List[str]:
    """Add ``ids`` to the marks for plan version ``updated_at`` and return
    them all. Marks for another version are replaced, not merged.

    The caller checks ``updated_at`` against the plan on disk first, so a
    page still showing an old plan can't overwrite the current marks.
    Raises :class:`OSError` when the file can't be written.
    """
    target = Path(path or CHIEF_ANSWERED_FILE)
    with _lock:
        kept = read_answered(updated_at, target)
        merged = (kept + [k for k in ids if k not in kept])[:MAX_IDS]
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(target, {"updated_at": updated_at, "ids": merged})
    logger.info(
        "ℹ️ chief answers recorded: %d new, %d on plan %s",
        len(merged) - len(kept), len(merged), updated_at or "(no updated_at)",
    )
    return merged
