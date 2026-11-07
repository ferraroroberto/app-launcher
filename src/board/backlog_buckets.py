"""Group the Board's backlog into where each open issue stands.

Every open issue lands in exactly one bucket, first match wins:

1. ``in_progress``: a live or unverified active-issue marker holds it, or a
   chief plan lane names it.
2. ``waiting``: it waits on the owner. That is the ``needs-owner`` or
   ``needs-validation`` label, an open plan question whose ``ref`` names it,
   or a queue row in ``waiting-roberto`` or parked with kind ``owner``.
3. ``queued``: any other plan queue row, except an umbrella.
4. ``not_planned``: in none of the above. The chief keeps this at zero.

Umbrella rows (parked, kind ``umbrella``) and machine records (``audit-meta``
issues) are ``folded``: listed in one collapsed row, outside every count.

An issue is only placed when every source that could put it in a higher
bucket was read. With the plan or the marker file unreadable, an issue no
marker holds is ``unknown``, never "Not planned": the lane or claim that
would have placed it is exactly what could not be read.

The plan's format is fleet-config's ``docs/chief-plan.md``; this module only
reads it, through :func:`src.chief.plan.read_plan_index`.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

IN_PROGRESS = "in_progress"
WAITING = "waiting"
QUEUED = "queued"
NOT_PLANNED = "not_planned"
FOLDED = "folded"
UNKNOWN = "unknown"

NEEDS_OWNER = "needs-owner"
NEEDS_VALIDATION = "needs-validation"

_REF = re.compile(r"^([A-Za-z0-9._-]*)#(\d+)$")
_AFTER_ISSUE = re.compile(r"^[A-Za-z0-9._-]+#\d+$")

Key = Tuple[str, int]


def _key(ref: Any, repo: Any = "") -> Optional[Key]:
    """``(repo, N)`` from a ``repo#N`` ref or a ``#N`` beside ``repo``."""
    match = _REF.match(str(ref or "").strip())
    if not match:
        return None
    name = (match.group(1) or str(repo or "")).strip().lower()
    return (name, int(match.group(2))) if name else None


def _card_key(card: Mapping[str, Any]) -> Optional[Key]:
    repo = str(card.get("repo") or "").strip().lower()
    number = card.get("number")
    if not repo or not isinstance(number, int) or isinstance(number, bool):
        return None
    return (repo, number)


def _after_chips(after: str) -> List[str]:
    """One chip per ``after`` condition, in words."""
    chips = []
    for token in after.split():
        if token.startswith("no-lane:"):
            chips.append("after no lane in " + token[len("no-lane:"):])
        elif _AFTER_ISSUE.match(token):
            chips.append("after " + token)
    return chips


def _queued_chips(row: Mapping[str, str]) -> List[str]:
    # "queued" repeats the bucket's heading; any other status is news.
    status = row.get("status") or ""
    chips = [status] if status and status != "queued" else []
    if row.get("kind") == "blocked-by" and row.get("blocked_by"):
        chips.append("blocked by " + row["blocked_by"])
    chips.extend(_after_chips(row.get("after") or ""))
    return chips


def _waiting_chips(
    labels: Set[str], asked: bool, row: Optional[Mapping[str, str]],
) -> List[str]:
    """Why the issue waits on the owner; empty when it doesn't."""
    chips = []
    if NEEDS_OWNER in labels:
        chips.append("before start")
    if NEEDS_VALIDATION in labels:
        chips.append("validate")
    if asked or (row is not None and row.get("status") == "waiting-roberto"):
        chips.append("question")
    if row is not None and row.get("status") == "parked" and row.get("kind") == "owner":
        chips.append("owner step")
    return chips


def _index(plan: Mapping[str, Any]) -> Tuple[Dict[Key, str], Dict[Key, Mapping[str, str]], Set[Key]]:
    """Lane status, queue row and question refs, each keyed by ``(repo, N)``."""
    lanes: Dict[Key, str] = {}
    for lane in plan.get("lanes") or []:
        key = _key(lane.get("item"), lane.get("repo"))
        if key is not None:
            lanes[key] = lane.get("status") or ""
    queue: Dict[Key, Mapping[str, str]] = {}
    for row in plan.get("queue") or []:
        key = _key(row.get("ref"), row.get("repo"))
        if key is not None and key not in queue:
            queue[key] = row
    asked = {
        key for key in (_key(w.get("ref"), w.get("repo")) for w in plan.get("waiting") or [])
        if key is not None
    }
    return lanes, queue, asked


def classify(
    issues: Iterable[Dict[str, Any]],
    records: Iterable[Dict[str, Any]],
    plan: Mapping[str, Any],
    *,
    markers_read: bool,
) -> Dict[str, Any]:
    """Stamp ``bucket`` and ``bucket_chips`` on every card; return the health.

    ``issues`` are the backlog cards, already carrying ``in_progress`` from
    the active-issue markers; ``records`` are the machine-record issues,
    always folded. ``plan`` is :func:`read_plan_index`'s result.
    ``markers_read`` is ``False`` when the marker file exists but could not
    be read. The return value is the payload's ``backlog_buckets`` section.
    """
    plan_read = plan.get("state") == "ok"
    lanes, queue, asked = _index(plan) if plan_read else ({}, {}, set())
    for card in issues:
        key = _card_key(card)
        card["bucket"], card["bucket_chips"] = _place(
            card, key, lanes, queue, asked, plan_read, markers_read,
        )
    for card in records:
        card["bucket"], card["bucket_chips"] = FOLDED, ["record"]
    unknown_reason = ""
    if not plan_read:
        unknown_reason = "plan unreadable"
    elif not markers_read:
        unknown_reason = "issue markers unreadable"
    return {
        "plan": "ok" if plan_read else "unreadable",
        "markers": "ok" if markers_read else "unreadable",
        "unknown_reason": unknown_reason,
    }


def _place(
    card: Mapping[str, Any],
    key: Optional[Key],
    lanes: Mapping[Key, str],
    queue: Mapping[Key, Mapping[str, str]],
    asked: Set[Key],
    plan_read: bool,
    markers_read: bool,
) -> Tuple[str, List[str]]:
    """One card's bucket and chips, by the module's precedence."""
    # The card's own claim chip already says "in progress".
    if card.get("in_progress") is True:
        return IN_PROGRESS, []
    if key is not None and key in lanes:
        status = lanes[key]
        return IN_PROGRESS, [status or "lane"]
    if not (plan_read and markers_read):
        return UNKNOWN, []
    labels = {str(lab).lower() for lab in card.get("labels") or []}
    row = queue.get(key) if key is not None else None
    chips = _waiting_chips(labels, key in asked, row)
    if chips:
        return WAITING, chips
    if row is not None:
        if row.get("status") == "parked" and row.get("kind") == "umbrella":
            return FOLDED, ["umbrella"]
        return QUEUED, _queued_chips(row)
    return NOT_PLANNED, []
