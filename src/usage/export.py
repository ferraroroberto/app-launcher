"""Send the launcher's Screen time to task-os's Time tracker (#1678, step 2/2).

The Screen time sheet (step 1) draws foreground minutes from the vendored
usage log (:func:`src.usage_log.foreground_time`). This module sends the same
numbers to task-os as one duration-only Time entry per completed local day:

- **One entry per day, all devices merged.** The day's total is the union
  across devices (phone and PC at once count once); the device split goes in
  the entry's note. One entry per device would double-count in task-os.
- **Idempotent by key.** Each day is ``PUT /api/time/entries/by-source/
  usage:app-launcher:<YYYY-MM-DD>`` (task-os#469): a resend replaces the
  entry, never duplicates it. A day that rounds to 0 minutes is a ``DELETE``
  of the same key; a 404 there means it was never sent, which is fine.
- **The last 7 completed days plus today by default.** Today goes as a running
  total under its own day key, replaced on every send and finalised by the
  next run; resending a week absorbs late events and rule changes. ``since``
  widens the window for a backfill.
- **Unchanged days are skipped.** The status file keeps what each day last
  sent (whole minutes and the device split), so a day that has not changed
  makes no request, and every send can say what it changed.
- **Config** lives in ``webapp_config.json``: ``task_os_url``,
  ``task_os_person`` (a task-os person id) and the target category as
  ``task_os_area`` / ``task_os_category`` slugs, resolved to an id through
  ``GET /api/time/meta`` on every send. A missing category is its own error
  and is never created here: the owner makes it in task-os's Time tab.

Failures are kept apart (:class:`ExportError` subclasses): the export is not
set up, the category does not exist, task-os is unreachable, task-os refused
the data, and the usage log does not exist yet. Each carries an HTTP status
for the webapp route and an exit code for the CLI and the nightly Job.

A send (not a dry run) records its outcome in ``screen-time-export.json`` in
the app's runtime-data folder, which the sheet's "Last sent" line reads. Only
dates, counts, the per-day sent minutes and the error message are stored,
never usage rows.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import requests

from src.clients import _loopback_http
from src._json_io import atomic_write_json
from src.runtime_data import runtime_data_dir, runtime_db_path
from src.usage_log import foreground_time, open_read_only
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)

APP = "app-launcher"
SOURCE_PREFIX = f"usage:{APP}:"
DEFAULT_DAYS = 7
STATUS_FILE = "screen-time-export.json"

# task-os serves a self-signed loopback cert, like the other sibling apps.
_VERIFY = False
_TIMEOUT = 10.0
# task-os's own bounds on an entry (src/time_repo.py there).
_MAX_MINUTES = 1440

# Device classes in the note's order, with the names the sheet shows.
_DEVICE_LABELS = (("desktop", "PC"), ("phone", "Phone"), ("tablet", "Tablet"))


class ExportError(RuntimeError):
    """A send that could not finish. ``kind`` names the failure for the
    status file and the sheet; ``status`` is the webapp route's HTTP status
    and ``exit_code`` the CLI's."""

    kind = "error"
    status = 500
    exit_code = 1


class ExportNotConfigured(ExportError):
    """A required ``task_os_*`` key is empty, or names no task-os person."""

    kind = "not_configured"
    status = 409
    exit_code = 2


class CategoryNotFound(ExportError):
    """The configured area/category slugs match no live task-os category."""

    kind = "category_missing"
    status = 409
    exit_code = 3


class TaskOsUnreachable(ExportError):
    """No answer from task-os at all: down, wrong port, or timed out."""

    kind = "unreachable"
    status = 503
    exit_code = 4


class TaskOsRejected(ExportError):
    """task-os answered and refused a request (any 4xx/5xx)."""

    kind = "rejected"
    status = 502
    exit_code = 5


class UsageLogMissing(ExportError):
    """No usage log file yet, so there is nothing to count. Never read as
    "every day is zero", which would delete what was sent before."""

    kind = "no_usage_log"
    status = 409
    exit_code = 6


@dataclass(frozen=True)
class DayEntry:
    """What one completed day sends: ``minutes`` 0 means delete the key."""

    day: date
    minutes: int
    by_device: Dict[str, int]
    note: str

    @property
    def source_id(self) -> str:
        return SOURCE_PREFIX + self.day.isoformat()

    @property
    def action(self) -> str:
        return "put" if self.minutes > 0 else "delete"


@dataclass(frozen=True)
class DayOutcome:
    """What a send did to one day: ``added``, ``updated``, ``removed`` or
    ``unchanged`` (no request, or a delete of a key task-os never had)."""

    day: date
    minutes: int
    outcome: str


@dataclass
class ExportResult:
    """One run's outcome; ``to_dict`` is the route's answer and the status file's record."""

    first: date
    last: date
    entries: List[DayEntry]
    dry_run: bool
    put: int = 0
    deleted: int = 0
    outcomes: List[DayOutcome] = field(default_factory=list)

    @property
    def total_minutes(self) -> int:
        return sum(e.minutes for e in self.entries)

    @property
    def unchanged(self) -> int:
        return sum(1 for o in self.outcomes if o.outcome == "unchanged")

    def summary(self) -> str:
        """The toast after a send: what changed, or that nothing did. ``last``
        is today, so days read as "today", "yesterday" or a date."""
        return _summary(self.outcomes, self.last)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "first": self.first.isoformat(),
            "last": self.last.isoformat(),
            "days": len(self.entries),
            "total_minutes": self.total_minutes,
            "dry_run": self.dry_run,
            "put": self.put,
            "deleted": self.deleted,
            "unchanged": self.unchanged,
            "summary": self.summary(),
        }


# ------------------------------------------------------------ the numbers


def send_days(today: date, since: Optional[date] = None) -> List[date]:
    """The days a send covers: ``since`` (default a week back) through today."""
    first = since if since is not None else today - timedelta(days=DEFAULT_DAYS)
    if first > today:
        raise ValueError(f"--since must not be after today ({today.isoformat()})")
    return [first + timedelta(days=i) for i in range((today - first).days + 1)]


def _whole_minutes(minutes: float) -> int:
    """Round half up to whole minutes (task-os stores integers), at most a day."""
    return min(_MAX_MINUTES, int(math.floor(float(minutes) + 0.5)))


def _format_minutes(minutes: int) -> str:
    """The sheet's wording: "45 m", "2 h", "2 h 5 m"."""
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{rest} m"
    return f"{hours} h {rest} m" if rest else f"{hours} h"


def entry_note(by_device: Mapping[str, int]) -> str:
    """The device split for the entry's note, e.g. "Launcher screen time:
    PC 1 h 5 m, Phone 20 m". Short enough for task-os's 140-character cap."""
    parts = [
        f"{label} {_format_minutes(by_device[device])}"
        for device, label in _DEVICE_LABELS
        if by_device.get(device, 0) > 0
    ]
    return "Launcher screen time: " + ", ".join(parts) if parts else "Launcher screen time"


def _day_label(day: date, today: date) -> str:
    if day == today:
        return "today"
    if day == today - timedelta(days=1):
        return "yesterday"
    return f"{day:%a} {day.day} {day:%b}"


# Older changed days listed by name before the toast folds them into a count.
_NAMED_OLDER_CHANGES = 3


def _summary(outcomes: List[DayOutcome], today: date) -> str:
    """One line for the toast after a send. Today and yesterday are always
    named; older days are named only when they changed."""
    if not outcomes:
        return "Nothing to send."
    if all(o.outcome == "unchanged" for o in outcomes):
        earlier = len(outcomes) - 1 if outcomes[-1].day == today else len(outcomes)
        if earlier <= 0:
            return "Nothing new to send. Today is already in task-os."
        what = "the last day" if earlier == 1 else f"the last {earlier} days"
        return f"Nothing new to send. Today and {what} are up to date in task-os."

    parts: List[str] = []
    older_changed: List[DayOutcome] = []
    older_unchanged = 0
    for o in reversed(outcomes):
        label = _day_label(o.day, today)
        recent = o.day >= today - timedelta(days=1)
        if o.outcome == "unchanged":
            if recent:
                parts.append(f"{label} unchanged")
            else:
                older_unchanged += 1
        elif recent:
            parts.append(_changed_phrase(o, label))
        else:
            older_changed.append(o)
    if len(older_changed) > _NAMED_OLDER_CHANGES:
        parts.append(f"{len(older_changed)} earlier days changed")
    else:
        parts.extend(_changed_phrase(o, _day_label(o.day, today)) for o in older_changed)
    if older_unchanged:
        parts.append(f"{older_unchanged} earlier day{'s' if older_unchanged != 1 else ''} unchanged")
    return "Sent: " + ", ".join(parts) + "."


def _changed_phrase(outcome: DayOutcome, label: str) -> str:
    if outcome.outcome == "removed":
        return f"{label} removed"
    return f"{label} {_format_minutes(outcome.minutes)} ({outcome.outcome})"


def _local_midnight(day: date) -> datetime:
    # A naive local time: astimezone() applies the host's DST rules for that
    # date, the same day boundary the sheet's GET /api/usage/time uses.
    return datetime.combine(day, time()).astimezone(timezone.utc)


def day_entries(conn: sqlite3.Connection, days: List[date]) -> List[DayEntry]:
    """Every day in ``days`` (consecutive) with its whole minutes and note."""
    since = _local_midnight(days[0])
    until = _local_midnight(days[-1] + timedelta(days=1))
    report = foreground_time(conn, since, until, None)
    entries = []
    for row in report["days_series"]:
        by_device = {k: _whole_minutes(v) for k, v in row["by_device"].items()}
        by_device = {k: v for k, v in by_device.items() if v > 0}
        minutes = _whole_minutes(row["minutes"])
        entries.append(DayEntry(
            day=date.fromisoformat(row["day"]),
            minutes=minutes,
            by_device=by_device,
            note=entry_note(by_device) if minutes else "",
        ))
    return entries


# ------------------------------------------------------------ task-os calls


def _task_os_message(resp: requests.Response) -> str:
    """task-os's error envelope is ``{"error": {"code", "message"}}``."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if body.get("detail"):
            return str(body["detail"])
    return f"HTTP {resp.status_code}"


def _call(method: str, url: str, **kwargs: Any) -> requests.Response:
    """One task-os call: no answer at all is :class:`TaskOsUnreachable`."""
    try:
        return _loopback_http.pooled_request(
            method, url, timeout=_TIMEOUT, verify=_VERIFY, **kwargs
        )
    except requests.RequestException as exc:
        logger.info("ℹ️ screen time export: %s %s failed (%s)", method, url, exc)
        raise TaskOsUnreachable("task-os is not reachable") from exc


def _rejected(what: str, resp: requests.Response) -> TaskOsRejected:
    message = _task_os_message(resp)
    logger.info("ℹ️ screen time export: task-os refused %s: HTTP %d %s",
                what, resp.status_code, message)
    return TaskOsRejected(f"task-os refused {what}: {message}")


def resolve_category(base: str, person: str, area_slug: str, category_slug: str) -> int:
    """The id of ``area_slug``/``category_slug`` from ``GET /api/time/meta``,
    after checking ``person`` is one of task-os's people."""
    resp = _call("GET", f"{base}/api/time/meta")
    if resp.status_code >= 400:
        raise _rejected("the category lookup", resp)
    try:
        meta = resp.json()
    except ValueError as exc:
        raise TaskOsRejected("task-os answered the category lookup with no JSON") from exc
    people = {p.get("id") for p in meta.get("people") or [] if isinstance(p, dict)}
    if person not in people:
        raise ExportNotConfigured(
            f"task_os_person {person!r} is not one of task-os's people"
        )
    for area in meta.get("areas") or []:
        if area.get("slug") != area_slug:
            continue
        for category in area.get("categories") or []:
            if category.get("slug") == category_slug and not category.get("archived"):
                return int(category["id"])
    raise CategoryNotFound(
        f"task-os has no category {area_slug}/{category_slug}: create it in its Time tab"
    )


def _sent_record(entry: DayEntry) -> Dict[str, Any]:
    """What the ledger keeps of a day: whole minutes and the device split."""
    return {"minutes": entry.minutes, "by_device": dict(entry.by_device)}


def send_entries(
    base: str,
    person: str,
    category_id: int,
    entries: List[DayEntry],
    sent: Dict[str, Dict[str, Any]],
) -> List[DayOutcome]:
    """Send each day that differs from ``sent`` (the ledger of what went
    before): PUT a day with minutes, DELETE a zero day, skip an unchanged one.
    ``sent`` is updated after every accepted day, so a refusal midway, which
    stops the run, still leaves the ledger true to what task-os holds."""
    outcomes = []
    for entry in entries:
        iso = entry.day.isoformat()
        url = f"{base}/api/time/entries/by-source/{entry.source_id}"
        record = _sent_record(entry)
        before = sent.get(iso)
        if before == record:
            outcome = "unchanged"
        elif entry.action == "put":
            resp = _call("PUT", url, json={
                "person": person,
                "category_id": category_id,
                "day": iso,
                "minutes": entry.minutes,
                "note": entry.note,
            })
            if resp.status_code >= 400:
                raise _rejected(iso, resp)
            outcome = "updated" if before and before.get("minutes") else "added"
        else:
            resp = _call("DELETE", url)
            if resp.status_code >= 400 and resp.status_code != 404:
                raise _rejected(f"the delete of {iso}", resp)
            # A 404 means task-os never had the key: nothing was removed.
            outcome = "unchanged" if resp.status_code == 404 else "removed"
        sent[iso] = record
        outcomes.append(DayOutcome(entry.day, entry.minutes, outcome))
    return outcomes


# ------------------------------------------------------------ orchestration


def _target(cfg: WebappConfig) -> tuple[str, str, str, str]:
    values = {
        "task_os_url": (cfg.task_os_url or "").strip().rstrip("/"),
        "task_os_person": (cfg.task_os_person or "").strip(),
        "task_os_area": (cfg.task_os_area or "").strip(),
        "task_os_category": (cfg.task_os_category or "").strip(),
    }
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise ExportNotConfigured(
            "set " + ", ".join(missing) + " in webapp_config.json"
        )
    return (values["task_os_url"], values["task_os_person"],
            values["task_os_area"], values["task_os_category"])


def is_configured(cfg: WebappConfig) -> bool:
    try:
        _target(cfg)
    except ExportNotConfigured:
        return False
    return True


def default_db_path() -> Path:
    return runtime_db_path(APP, "usage.sqlite3")


def default_status_path() -> Path:
    return runtime_data_dir(APP) / STATUS_FILE


def run_export(
    cfg: WebappConfig,
    *,
    since: Optional[date] = None,
    dry_run: bool = False,
    today: Optional[date] = None,
    db_path: Optional[Path] = None,
    status_path: Optional[Path] = None,
) -> ExportResult:
    """Count the days through today and send those that changed; a dry run
    only counts.

    A dry run touches neither task-os nor the status file, so it needs no
    ``task_os_*`` config. A real send records its outcome, success or
    :class:`ExportError`, in the status file before returning or raising.
    """
    days = send_days(today or date.today(), since)
    db_path = db_path or default_db_path()
    status_path = status_path or default_status_path()
    ledger: Optional[Dict[str, Any]] = None
    try:
        if not db_path.is_file():
            raise UsageLogMissing("no usage log yet: nothing to send")
        target = None if dry_run else _target(cfg)
        conn = open_read_only(db_path)
        try:
            entries = day_entries(conn, days)
        finally:
            conn.close()
        result = ExportResult(days[0], days[-1], entries, dry_run)
        if target is not None:
            base, person, area, category = target
            category_id = resolve_category(base, person, area, category)
            ledger = _read_ledger(status_path, _target_key(base, person, category_id))
            result.outcomes = send_entries(base, person, category_id, entries, ledger["days"])
            result.put = sum(1 for o in result.outcomes if o.outcome in ("added", "updated"))
            result.deleted = sum(1 for o in result.outcomes if o.outcome == "removed")
    except ExportError as exc:
        if not dry_run:
            _record(status_path, error=exc, ledger=ledger)
        raise
    if not dry_run:
        _record(status_path, result=result, ledger=ledger)
        logger.info(
            "✅ screen time export: %s to %s, %d sent, %d deleted, %d unchanged",
            result.first.isoformat(), result.last.isoformat(),
            result.put, result.deleted, result.unchanged,
        )
    return result


# ------------------------------------------------------------ status file


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_raw(path: Path) -> Dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError) as exc:
        logger.info("ℹ️ screen time export: unreadable status file %s (%s)", path, exc)
        raw = {}
    return raw if isinstance(raw, dict) else {}


def read_status(status_path: Optional[Path] = None) -> Dict[str, Any]:
    """``{"last_ok": {...} | None, "last_error": {...} | None}``."""
    raw = _read_raw(status_path or default_status_path())
    return {"last_ok": raw.get("last_ok"), "last_error": raw.get("last_error")}


def _target_key(base: str, person: str, category_id: int) -> str:
    """Names where the ledger's days went: a new task-os, person or category
    starts a fresh ledger, so nothing is skipped as "already sent" there."""
    return hashlib.sha256(f"{base}|{person}|{category_id}".encode("utf-8")).hexdigest()[:16]


def _read_ledger(status_path: Path, target: str) -> Dict[str, Any]:
    """What each day last sent to ``target``: ``{"target", "days": {iso: {minutes, by_device}}}``."""
    stored = _read_raw(status_path).get("sent")
    if isinstance(stored, dict) and stored.get("target") == target and isinstance(stored.get("days"), dict):
        return {"target": target, "days": dict(stored["days"])}
    return {"target": target, "days": {}}


def _record(
    status_path: Path,
    *,
    result: Optional[ExportResult] = None,
    error: Optional[ExportError] = None,
    ledger: Optional[Dict[str, Any]] = None,
) -> None:
    """Keep the last success, the last failure and the per-day ledger. A
    success clears nothing: the sheet shows a failure only when it is newer
    than the last success."""
    status = _read_raw(status_path)
    if ledger is not None:
        status["sent"] = ledger
    if result is not None:
        status["last_ok"] = {"at": _now_iso(), **result.to_dict()}
    if error is not None:
        status["last_error"] = {"at": _now_iso(), "kind": error.kind, "message": str(error)}
    try:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(status_path, status)
    except OSError as exc:
        logger.warning("⚠️ screen time export: could not write %s (%s)", status_path, exc)
