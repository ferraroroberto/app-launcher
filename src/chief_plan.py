"""Read the fleet chief's plan file for the Board's "Chief's plan" card (#1279).

The chief records its lanes, its queue and what waits on Roberto in
``~/.claude/hooks/state/chief-plan.json`` (format v1, written only through
fleet-config's ``chief_ops.py plan`` helper). This module is the app's reader,
and it is tolerant by contract: unknown fields are dropped, a missing file or
an empty queue is ``empty``, and anything it cannot trust is ``unreadable`` —
never an exception, never an error toast. Status values pass through
unvalidated; the card shows an unknown one as a neutral chip.

A ``waiting_on_roberto`` item is one of the chief's questions for the Board's
answer sheet (#1295). Beyond v1's ``text`` and ``ref`` it may carry the
additive fields ``id``, ``repo``, ``question``, ``detail``, ``recommendation``,
``options`` and ``multi``. A field of the wrong type is dropped on its own,
never failing the item, and an old text-only item reads as a free-text
question. A ``ref`` shaped ``repo#N`` (or ``#N`` beside a ``repo``) gains a
``ref_url`` on the configured GitHub owner; any other ref stays plain text.
Queue rows gain a ``ref_url`` the same way, for the card's title-first rows
(#1297), where the repo and number are the secondary, linked text.

Lane and queue rows may carry an optional ``model`` (``opus`` | ``sonnet``,
#1352). It passes through unvalidated for the card's quiet chip; anything but
a string reads as blank.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

logger = logging.getLogger(__name__)

SUPPORTED_VERSION = 1

_LANE_FIELDS = ("repo", "session", "item", "status", "model")
_QUEUE_FIELDS = ("repo", "ref", "title", "status", "note", "model")
_WAITING_FIELDS = ("text", "ref", "repo", "detail", "recommendation")
# Read only when the chief wrote a string: a number here is a wrong type, not
# a label to show (#1352).
_STRING_ONLY_FIELDS = frozenset({"model"})
MAX_OPTIONS = 4
_REF = re.compile(r"^([A-Za-z0-9._-]*)#(\d+)$")


def _text(value: Any) -> str:
    """A field as one line of text; anything that isn't a scalar is blank."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return " ".join(str(value).split())


def _field(entry: Mapping[str, Any], name: str) -> str:
    """One row field as text; a string-only field of any other type is blank."""
    value = entry.get(name)
    if name in _STRING_ONLY_FIELDS and not isinstance(value, str):
        return ""
    return _text(value)


def _rows(value: Any, fields: Sequence[str]) -> List[Dict[str, str]]:
    """The dict entries of a list, each cut down to ``fields``."""
    if not isinstance(value, list):
        return []
    return [
        {name: _field(entry, name) for name in fields}
        for entry in value
        if isinstance(entry, Mapping)
    ]


def _options(value: Any) -> List[Dict[str, Any]]:
    """An item's choices: the first :data:`MAX_OPTIONS` entries with a label."""
    if not isinstance(value, list):
        return []
    options = [
        {
            "label": _text(entry.get("label")),
            "description": _text(entry.get("description")),
            "recommended": entry.get("recommended") is True,
        }
        for entry in value
        if isinstance(entry, Mapping) and _text(entry.get("label"))
    ]
    return options[:MAX_OPTIONS]


def _ref_url(ref: str, repo: str, owner: str) -> str:
    """The GitHub link for a ``repo#N`` ref, or ``""`` when it isn't one."""
    match = _REF.match(ref)
    repo = (match.group(1) or repo) if match else ""
    if not (match and repo and owner):
        return ""
    return f"https://github.com/{owner}/{repo}/issues/{match.group(2)}"


def _waiting(value: Any, owner: str) -> List[Dict[str, Any]]:
    """The questions waiting on Roberto, each read field by field.

    ``question`` falls back to ``text``, so an old item still has a line to
    ask. A missing ``id`` stays blank: the sheet keys that item on its
    position, and the message it sends quotes only ids the chief wrote.
    """
    if not isinstance(value, list):
        return []
    items: List[Dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            continue
        item: Dict[str, Any] = {name: _text(entry.get(name)) for name in _WAITING_FIELDS}
        item["id"] = _text(entry.get("id"))
        item["question"] = _text(entry.get("question")) or item["text"]
        item["options"] = _options(entry.get("options"))
        item["multi"] = entry.get("multi") is True
        item["ref_url"] = _ref_url(item["ref"], item["repo"], owner)
        items.append(item)
    return items


def read_chief_plan(path: Path, github_owner: str = "") -> Dict[str, Any]:
    """The plan as ``{"state": "ok", ...}``, or ``{"state": "empty"}`` /
    ``{"state": "unreadable"}``. ``github_owner`` resolves question and
    queue-row refs to links; without it they stay plain text.

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
    for row in queue:
        row["ref_url"] = _ref_url(row["ref"], row["repo"], github_owner)
    return {
        "state": "ok",
        "updated_at": _text(data.get("updated_at")),
        "lanes": _rows(data.get("lanes"), _LANE_FIELDS),
        "queue": queue,
        "waiting_on_roberto": _waiting(data.get("waiting_on_roberto"), github_owner),
    }
