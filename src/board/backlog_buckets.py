"""Partition the Board's open issues into buckets, one per issue.

The base unit is one open issue. Every open issue the GitHub fetch returned
lands in exactly one bucket, the bucket counts add up to the Backlog total, and
every other count on the Board (the Chief's plan sections, Your turn's owner
count) is read from this same assignment, never recomputed from the plan or
the labels. First match wins:

1. ``in_progress``: a live or unverified active-issue marker holds it, or a
   chief plan lane names it.
2. ``record``: a machine record (``audit-meta``), bookkeeping rather than
   work. No plan or label source changes that, so a record is placed even
   when the plan is unreadable.
3. ``waiting`` (Waiting on you): the ``needs-owner`` or ``needs-validation``
   label, and nothing else. A plan row cannot put an issue here without the
   label, because the owner checks only list labelled issues.
4. A plan queue row decides the rest: ``umbrella`` (parked, kind
   ``umbrella``), ``blocked`` (any other parked row: kind ``blocked-by``, an
   ``owner`` row that lacks the label, or a parked row with no kind) or
   ``queued`` (every other status).
5. ``dormant``: no row places it and its repo is dormant in the fleet scope.
6. ``not_planned``: in none of the above. The chief keeps this at zero.

``umbrella``, ``record`` and ``dormant`` draw folded shut, but they are counted
in the total like every other bucket. A plan question on an issue, or a
``waiting-roberto`` row, adds a ``question`` chip wherever the issue lands.

An issue is only placed when every source that could put it in a higher
bucket was read. With the plan or the marker file unreadable, an issue no
marker holds is ``unknown``, never "Not planned". An unreadable fleet scope
places issues as if every repo were active, and says ``scope: unknown``.

The plan's format is fleet-config's ``docs/chief-plan.md``; this module only
reads it, through :func:`src.chief.plan.read_plan_index`.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from src.board import owner_checks
from src.chief.queue_status import QueueStatus, queue_status

IN_PROGRESS = "in_progress"
WAITING = "waiting"
QUEUED = "queued"
BLOCKED = "blocked"
NOT_PLANNED = "not_planned"
UMBRELLA = "umbrella"
RECORD = "record"
DORMANT = "dormant"
UNKNOWN = "unknown"

# Every bucket, in the order the payload's ``counts`` lists them.
BUCKETS = (UNKNOWN, IN_PROGRESS, WAITING, QUEUED, BLOCKED, NOT_PLANNED, UMBRELLA, RECORD, DORMANT)

NEEDS_OWNER = "needs-owner"
NEEDS_VALIDATION = "needs-validation"

# Your turn's owner-check groups: every item sits in exactly one.
BEFORE_START_GROUP = "before_start"
VALIDATE_TO_CLOSE = "validate_to_close"
TRY_SHIPPED = "try_shipped"

# Owner-check items whose issue is closed: shipped work to validate, counted
# beside the open-issue total, never inside it.
CLOSED = "closed"
# An open owner-check item the backlog does not hold (the two lists were
# read at different moments).
ABSENT = "absent"

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


# Statuses the bucket heading or the question chip already says, so no status chip.
_HEADING_SAYS_IT = frozenset({QueueStatus.QUEUED, QueueStatus.PARKED, QueueStatus.WAITING_ROBERTO})


def _row_chips(row: Mapping[str, str]) -> List[str]:
    """A queued or blocked row's reasons; the bucket heading says the rest."""
    status = row.get("status") or ""
    state = queue_status(row)
    kind = row.get("kind") or ""
    chips: List[str] = []
    if status and state not in _HEADING_SAYS_IT:
        chips.append(status)
    if state is QueueStatus.PARKED:
        if kind == "blocked-by" and row.get("blocked_by"):
            chips.append("blocked by " + row["blocked_by"])
        elif kind == "owner":
            chips.extend(["owner step", "no label"])
        elif kind != "blocked-by":
            chips.append("parked")
    chips.extend(_after_chips(row.get("after") or ""))
    return chips


def _label_chips(labels: Set[str]) -> List[str]:
    """Why a labelled issue waits on the owner; empty when it doesn't."""
    chips = []
    if NEEDS_OWNER in labels:
        chips.append("before start")
    if NEEDS_VALIDATION in labels:
        chips.append("validate")
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
    dormant_repos: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Stamp ``bucket`` and ``bucket_chips`` on every card; return the section.

    ``issues`` are the backlog cards, already carrying ``in_progress`` from
    the active-issue markers; ``records`` are the machine-record issues.
    ``plan`` is :func:`read_plan_index`'s result. ``markers_read`` is
    ``False`` when the marker file exists but could not be read.
    ``dormant_repos`` is the lower-cased dormant repo names, or ``None`` when
    the fleet scope could not be read. The return value is the payload's
    ``backlog_buckets`` section: the sources' health, ``counts`` per bucket
    and their sum, ``total``.
    """
    plan_read = plan.get("state") == "ok"
    lanes, queue, asked = _index(plan) if plan_read else ({}, {}, set())
    dormant = dormant_repos or set()
    counts = {bucket: 0 for bucket in BUCKETS}
    for card, is_record in [(c, False) for c in issues] + [(c, True) for c in records]:
        key = _card_key(card)
        bucket, chips = _place(
            card, key, is_record, lanes, queue, asked, dormant, plan_read, markers_read,
        )
        card["bucket"], card["bucket_chips"] = bucket, chips
        counts[bucket] += 1
    unknown_reason = ""
    if not plan_read:
        unknown_reason = "plan unreadable"
    elif not markers_read:
        unknown_reason = "issue markers unreadable"
    return {
        "plan": "ok" if plan_read else "unreadable",
        "markers": "ok" if markers_read else "unreadable",
        "scope": "unknown" if dormant_repos is None else "ok",
        "unknown_reason": unknown_reason,
        "counts": counts,
        "total": sum(counts.values()),
    }


def _place(
    card: Mapping[str, Any],
    key: Optional[Key],
    is_record: bool,
    lanes: Mapping[Key, str],
    queue: Mapping[Key, Mapping[str, str]],
    asked: Set[Key],
    dormant: Set[str],
    plan_read: bool,
    markers_read: bool,
) -> Tuple[str, List[str]]:
    """One card's bucket and chips, by the module's precedence."""
    # The card's own claim chip already says "in progress".
    if card.get("in_progress") is True:
        return IN_PROGRESS, []
    if key is not None and key in lanes:
        return IN_PROGRESS, [lanes[key] or "lane"]
    if is_record:
        return RECORD, []
    if not (plan_read and markers_read):
        return UNKNOWN, []
    labels = {str(lab).lower() for lab in card.get("labels") or []}
    row = queue.get(key) if key is not None else None
    question = key in asked or queue_status(row) is QueueStatus.WAITING_ROBERTO
    tail = ["question"] if question else []
    chips = _label_chips(labels)
    if chips:
        return WAITING, chips + tail
    if row is not None:
        if queue_status(row) is QueueStatus.PARKED:
            bucket = UMBRELLA if row.get("kind") == "umbrella" else BLOCKED
            return bucket, ([] if bucket == UMBRELLA else _row_chips(row)) + tail
        return QUEUED, _row_chips(row) + tail
    if key is not None and key[0] in dormant:
        return DORMANT, tail
    return NOT_PLANNED, tail


def _unblocks(plan: Optional[Mapping[str, Any]]) -> Dict[Key, List[str]]:
    """The plan rows each issue holds up: ``{(repo, N): ["repo#M", ...]}``.

    Only a parked ``blocked-by`` row waits on the issues its ``blocked_by``
    names (the same rows :func:`_row_chips` reads); a bare ``#N`` there is the
    row's own repo. Empty when the plan was not read.
    """
    held: Dict[Key, List[str]] = {}
    if not plan or plan.get("state") != "ok":
        return held
    for row in plan.get("queue") or []:
        if queue_status(row) is not QueueStatus.PARKED or row.get("kind") != "blocked-by":
            continue
        repo = str(row.get("repo") or "")
        ref = str(row.get("ref") or "")
        label = repo + ref if ref.startswith("#") else ref
        if _key(ref, repo) is None:
            continue
        for token in re.split(r"[\s,]+", str(row.get("blocked_by") or "")):
            blocker = _key(token, repo)
            if blocker is not None and label not in held.setdefault(blocker, []):
                held[blocker].append(label)
    return held


def owner_group(item: Mapping[str, Any]) -> str:
    """The one Your turn group an owner-check item belongs to.

    An open ``before-start`` item blocks work from starting; an open
    ``validate-after`` item is validated to close; a closed issue's check
    only tries what shipped.
    """
    if str(item.get("state") or "").upper() == "CLOSED":
        return TRY_SHIPPED
    return BEFORE_START_GROUP if item.get("kind") == owner_checks.BEFORE_START else VALIDATE_TO_CLOSE


def owner_counts(
    cards: Iterable[Mapping[str, Any]],
    items: Iterable[Dict[str, Any]],
    plan: Optional[Mapping[str, Any]] = None,
) -> Dict[str, int]:
    """Stamp each owner-check item; return Your turn's counts.

    ``cards`` are the classified backlog cards, ``items`` the owner-check list
    and ``plan`` :func:`read_plan_index`'s result. An open item takes its
    issue's bucket (``absent`` when the backlog does not hold it); a closed
    one is ``closed``. Each item takes exactly one ``group``
    (:func:`owner_group`) and, in Validate to close, the plan rows it
    ``unblocks``. An item is ``counted`` when it waits on the owner: unmarked,
    and in Waiting on you or closed.

    So the badge's ``needs_you`` is the three group counts added, one issue in
    one group, and Waiting on you is the two open groups plus what the owner
    set aside (``waiting_set_aside``) and ``unlisted``, the Waiting on you
    issues the owner-check list does not hold yet.
    """
    buckets = {k: c.get("bucket") for c in cards if (k := _card_key(c)) is not None}
    waiting = {k for k, b in buckets.items() if b == WAITING}
    held = _unblocks(plan)
    listed: Set[Key] = set()
    per_group = {BEFORE_START_GROUP: 0, VALIDATE_TO_CLOSE: 0, TRY_SHIPPED: 0}
    set_aside = waiting_set_aside = 0
    for item in items:
        key = _card_key(item)
        item["group"] = owner_group(item)
        item["unblocks"] = held.get(key, []) if item["group"] == VALIDATE_TO_CLOSE and key else []
        if item["group"] == TRY_SHIPPED:
            item["bucket"] = CLOSED
        else:
            item["bucket"] = buckets.get(key, ABSENT) if key is not None else ABSENT
            if key is not None:
                listed.add(key)
        counted = item["bucket"] in (WAITING, CLOSED)
        if counted and item.get("mark"):
            set_aside += 1
            waiting_set_aside += item["bucket"] == WAITING
            counted = False
        item["counted"] = counted
        per_group[item["group"]] += counted
    return {
        "waiting": len(waiting),
        "before_start": per_group[BEFORE_START_GROUP],
        "validate_to_close": per_group[VALIDATE_TO_CLOSE],
        "try_shipped": per_group[TRY_SHIPPED],
        "set_aside": set_aside,
        "waiting_set_aside": waiting_set_aside,
        "needs_you": sum(per_group.values()),
        "unlisted": len(waiting - listed),
    }
