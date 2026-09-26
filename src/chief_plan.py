"""Read the fleet chief's plan file for the Board's "Chief's plan" card (#1279).

The chief records its lanes, its queue and what waits on Roberto in
``~/.claude/hooks/state/chief-plan.json`` (format v1, written only through
fleet-config's ``chief_ops.py plan`` helper). This module is the app's reader,
and it is tolerant by contract: unknown fields are dropped, a missing file or
an empty queue is ``empty``, and anything it cannot trust is ``unreadable`` —
never an exception, never an error toast. Status values pass through
unvalidated; the card shows an unknown one as a neutral chip.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

logger = logging.getLogger(__name__)

SUPPORTED_VERSION = 1

_LANE_FIELDS = ("repo", "session", "item", "status")
_QUEUE_FIELDS = ("repo", "ref", "title", "status", "note")
_WAITING_FIELDS = ("text", "ref")


def _text(value: Any) -> str:
    """A field as one line of text; anything that isn't a scalar is blank."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return " ".join(str(value).split())


def _rows(value: Any, fields: Sequence[str]) -> List[Dict[str, str]]:
    """The dict entries of a list, each cut down to ``fields``."""
    if not isinstance(value, list):
        return []
    return [
        {name: _text(entry.get(name)) for name in fields}
        for entry in value
        if isinstance(entry, Mapping)
    ]


def read_chief_plan(path: Path) -> Dict[str, Any]:
    """The plan as ``{"state": "ok", ...}``, or ``{"state": "empty"}`` /
    ``{"state": "unreadable"}``.

    ``empty``: no file, or a queue with no rows. ``unreadable``: the file
    exists but can't be read, isn't JSON, isn't an object, or names a
    version other than :data:`SUPPORTED_VERSION`.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"state": "empty"}
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("⚠️ chief plan unreadable: %s", exc.__class__.__name__)
        return {"state": "unreadable"}
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("⚠️ chief plan is not valid JSON")
        return {"state": "unreadable"}
    if not isinstance(data, dict) or data.get("version") != SUPPORTED_VERSION:
        logger.warning("⚠️ chief plan has an unsupported shape or version")
        return {"state": "unreadable"}
    queue = _rows(data.get("queue"), _QUEUE_FIELDS)
    if not queue:
        return {"state": "empty"}
    return {
        "state": "ok",
        "updated_at": _text(data.get("updated_at")),
        "lanes": _rows(data.get("lanes"), _LANE_FIELDS),
        "queue": queue,
        "waiting_on_roberto": _rows(data.get("waiting_on_roberto"), _WAITING_FIELDS),
    }
