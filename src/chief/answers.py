"""Which of the chief's questions the Board already answered (#1487).

The answer sheet (#1295) sends one message to the chief, and the chief removes
the items it has taken by rewriting its plan. Until it does, the Board marks
the sent items "answered, waiting for the chief". Those marks used to live in
the answering browser's localStorage, so another device showed the same
questions as open and invited a second set of answers. They live here now,
once per machine, so every device renders the same state.

Store: ``webapp/chief-answered.json``, gitignored, atomically replaced — the
launcher's own runtime state, beside :mod:`src.chief.pointer`'s sidecar. The
app still never writes the chief's plan file; this is a separate record.

Shape: ``{"updated_at": <the plan's updated_at>, "ids": [<item key>, ...]}``.
An item's key is the chief's own ``id``, else its position in the list (the
sheet's ``itemKey``). The marks hold only for the plan version they were made
on: once the chief rewrites the plan (its ``updated_at`` moves) they read as
none, and the next record replaces them.

Degradation contract: missing, corrupt or for another plan version reads as
``[]`` — never an exception. A failed write raises :class:`OSError` to the
route, which says so to the device that answered.

How many questions still wait (#1502) is counted here too, for the Board tab's
badge on every tab: :func:`unanswered_count` applies :func:`item_key`, the
Python twin of ``chief-keys.js``'s ``itemKey``. Both sides are pinned to one
shared fixture (``tests/fixtures/chief_question_keys.json``).
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from src.chief import plan as chief_plan
from src._json_io import atomic_write_json

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

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


def item_key(item: Mapping[str, Any], index: int) -> str:
    """A question's key: the chief's own ``id``, else its position in the
    list. The same rule as ``chief-keys.js``'s ``itemKey``."""
    return str(item.get("id") or "") or str(index)


def read_plan_with_answers(plan_file: Path, github_owner: str = "") -> Dict[str, Any]:
    """The chief's plan as :func:`src.chief.plan.read_chief_plan` reads it; an
    ``ok`` plan also carries ``answered``, the keys any device already
    answered on this version of it."""
    plan = chief_plan.read_chief_plan(plan_file, github_owner)
    if plan["state"] == "ok":
        plan["answered"] = read_answered(plan["updated_at"])
    return plan


def unanswered_count(plan: Mapping[str, Any]) -> Optional[int]:
    """How many of the plan's questions no device has answered yet, fleet-wide.

    ``None`` when the plan can't be read: unknown, never zero. A plan with
    nothing queued (``empty``) has no questions.
    """
    state = plan.get("state")
    if state == "empty":
        return 0
    if state != "ok":
        return None
    answered = set(plan.get("answered") or [])
    return sum(
        1 for index, item in enumerate(plan.get("waiting_on_roberto") or [])
        if item_key(item, index) not in answered
    )
