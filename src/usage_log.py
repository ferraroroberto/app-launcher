"""Local-only usage log for a fleet web app: views, taps, device class (#349).

Design reviews used to ask the owner "how often do you use X?" because no app
recorded which views or controls were actually used. This module is the
server half of the ``usage-log`` convention: a FastAPI router that accepts the
vendored client's batched events (``app/webapp/static/_vendored/usage-log/``)
into one SQLite file on the machine that serves the app, and a summary query
that ``scripts/usage_summary.py`` and ``GET /api/usage/summary`` both read.

**Foreground time** (#403): :func:`foreground_time` turns the same rows into
Screen-Time-style minutes per local day, per device class and per view, read
by ``GET /api/usage/time`` and ``usage_summary.py --time``. Arithmetic over
existing rows only — no new event kind, column or client signal. The rules:

1. Rows are grouped by ``session_id`` (one page visit) and ordered by ``id``,
   never by ``ts``: one client batch shares one server-stamped ``ts``.
2. An interval opens at ``session_start``, or at the visit's first event of
   any kind when its start was lost, and closes at the next ``session_end``.
3. A second ``session_start`` before an end (lost hide beacon) closes the open
   interval at the last event seen before it; a visit that never ends closes
   at its last event. Nothing is ever padded by a fixed amount, so a
   start-only visit reads as ~0.
4. ``phone``/``tablet`` intervals are trusted (auto-lock hides the page), cut
   only at :data:`MAX_INTERVAL` as a lost-end safety net. On ``desktop`` (a
   visible tab never reports hidden), a stretch longer than
   :data:`IDLE_GAP_DESKTOP` with no event in it counts as that gap only.
5. Time is split at local midnight in the caller's ``tz``; the day is the
   local date.
6. A day's total is the union of all intervals (two tabs, or phone and PC at
   once, are never counted twice); each device class is the union of its own
   intervals, so device rows may sum to more than the total; each slice goes
   to the visit's latest ``view`` at that moment, overlaps to the most
   recently started interval, so view rows sum to the total.
7. Accepted error: a start is stamped up to one client flush (10 s) late, so
   a visit shorter than that reads as 0 — a small undercount, not corrected.

**Privacy is the contract.** Never log message contents, document text,
transcripts, names, file names, search queries, form values, URLs with query
strings, IP addresses, or the raw user-agent. Only: event kind, view id,
control id, a random per-visit session id, device class, and timestamps.
Control ids are developer-chosen constants in ``data-track``, never derived
from element text or user data.

The privacy contract is enforced here, not trusted to the client: an event is
stored only if its ``kind`` is in :data:`EVENT_KINDS` and its id matches
:data:`ID_PATTERN` (bounded, no spaces, no ``/`` — a URL or a sentence cannot
pass), the device and OS classes must be one of a few constants, and the
table has no column that could hold anything else. Extra keys a client sends
are never read.

**Instrumentation never breaks the app.** A locked or full database drops the
batch with one info-level log line and a 503 the client ignores; nothing here
raises into a request the app cares about. Writes are one transaction per
client batch (the client batches; there is no per-tap ``fsync``), and every
batch prunes rows past the age and row caps so a runaway client cannot fill
the disk.

Wiring (in the app, not here — the path is the app's choice):

    from src.runtime_data import runtime_db_path
    from src.usage_log import usage_router

    app.include_router(usage_router(
        runtime_db_path("my-app", "usage.sqlite3", create=True),
        dependencies=[Depends(require_auth)],   # the app's own auth rules
    ))

Vendor-verbatim: adopters copy this file byte-identical into their own
``src/`` (manifest key ``usage_log``). It deliberately does not import
``src.runtime_data`` — the database path is a call-site argument, so the copy
resolves in any tree. Stdlib-only at import; FastAPI is imported only inside
:func:`usage_router`, so the summary CLI runs under a venv without it.
"""

# No ``from __future__ import annotations`` here, deliberately: FastAPI reads
# the handlers' annotations at runtime, and ``Request`` is imported inside
# ``usage_router`` (FastAPI stays optional at import), so a stringified
# ``request: Request`` would not resolve and the route would answer 422.
import json
import logging
import re
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from itertools import groupby
from operator import itemgetter
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from fastapi import APIRouter

logger = logging.getLogger(__name__)

#: The only event kinds stored. Anything else is dropped.
EVENT_KINDS = frozenset({"session_start", "view", "tap", "session_end"})
#: Coarse device classes the client derives; the raw user-agent never leaves it.
DEVICE_CLASSES = frozenset({"phone", "tablet", "desktop"})
OS_CLASSES = frozenset({"ios", "android", "windows", "other"})

#: View and control ids: developer-chosen constants. Bounded, starts with an
#: alphanumeric, no whitespace and no ``/`` — so no URL, path or sentence fits.
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
#: A random per-visit id (the client sends a UUID).
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9-]{8,64}")

#: Retention: rows older than this are pruned on every insert.
DEFAULT_MAX_AGE_DAYS = 365
#: Hard cap on the table; the oldest rows beyond it are pruned on insert.
DEFAULT_MAX_ROWS = 200_000
#: Per-request limits — a batch is a handful of events, never thousands.
MAX_EVENTS_PER_BATCH = 50
MAX_BODY_BYTES = 16_384

#: Foreground time: a desktop stretch with no event longer than this counts
#: as this much only (rule 4).
IDLE_GAP_DESKTOP = timedelta(minutes=15)
#: Foreground time: a phone/tablet interval is cut at this length (rule 4).
MAX_INTERVAL = timedelta(hours=4)
#: ``GET /api/usage/time`` window bound, in local days.
MAX_TIME_DAYS = 366
#: The ``by_view`` key for time before a visit's first ``view`` event; it
#: cannot collide with a real view id (:data:`ID_PATTERN` starts alphanumeric).
NO_VIEW = "(none)"

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    kind         TEXT NOT NULL,
    view_id      TEXT,
    control_id   TEXT,
    device_class TEXT NOT NULL,
    os_class     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
"""


class PayloadError(ValueError):
    """The batch envelope itself is unusable (not JSON, bad session/device)."""


@dataclass(frozen=True)
class Event:
    """One validated row, ready to insert."""

    kind: str
    view_id: str | None
    control_id: str | None


@dataclass
class Batch:
    """A validated client batch: the envelope plus the events that passed."""

    session_id: str
    device_class: str
    os_class: str
    events: list[Event] = field(default_factory=list)
    rejected: int = 0


def format_ts(moment: datetime) -> str:
    """UTC timestamp in the one stored format (lexically sortable)."""
    return moment.astimezone(UTC).strftime(_TS_FORMAT)


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None


def validate_event(raw: object) -> Event | None:
    """One client event → a storable :class:`Event`, or ``None`` to drop it.

    Dropped: a ``kind`` outside :data:`EVENT_KINDS`; a ``view`` without a
    valid ``id``; a ``tap`` without a valid ``id``; any id (including the
    optional ``view`` a tap carries) that does not match :data:`ID_PATTERN`.
    Keys other than ``kind``/``id``/``view`` are ignored, never stored.
    """
    if not isinstance(raw, Mapping):
        return None
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in EVENT_KINDS:
        return None
    ident = raw.get("id")
    context_view = raw.get("view")
    if context_view is not None and not _valid_id(context_view):
        return None
    if kind == "view":
        return Event(kind, ident, None) if _valid_id(ident) else None
    if kind == "tap":
        if not _valid_id(ident):
            return None
        return Event(kind, context_view if isinstance(context_view, str) else None, ident)
    # session_start / session_end carry no id; a stray one is not stored.
    return Event(kind, context_view if isinstance(context_view, str) else None, None)


def parse_batch(body: bytes) -> Batch:
    """Decode and validate a ``POST /api/usage/events`` body.

    Raises :class:`PayloadError` for an unusable envelope. Individual bad
    events are counted in :attr:`Batch.rejected`, not raised.
    """
    if len(body) > MAX_BODY_BYTES:
        raise PayloadError("batch too large")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise PayloadError("body is not JSON") from exc
    if not isinstance(data, Mapping):
        raise PayloadError("body is not a JSON object")
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or SESSION_ID_PATTERN.fullmatch(session_id) is None:
        raise PayloadError("bad session_id")
    device_class = data.get("device_class")
    if not isinstance(device_class, str) or device_class not in DEVICE_CLASSES:
        raise PayloadError("bad device_class")
    os_class = data.get("os_class")
    if not isinstance(os_class, str) or os_class not in OS_CLASSES:
        raise PayloadError("bad os_class")
    raw_events = data.get("events")
    if not isinstance(raw_events, list) or len(raw_events) > MAX_EVENTS_PER_BATCH:
        raise PayloadError("bad events list")
    batch = Batch(session_id, device_class, os_class)
    for raw in raw_events:
        event = validate_event(raw)
        if event is None:
            batch.rejected += 1
        else:
            batch.events.append(event)
    return batch


class UsageLog:
    """The ``events`` table behind one app's ``usage.sqlite3``.

    Each call opens and closes its own short-lived connection (WAL,
    ``synchronous=NORMAL``, a 1 s busy timeout so a locked file drops the batch
    fast instead of stalling a request).
    """

    def __init__(
        self,
        db_path: Path,
        *,
        max_age_days: int = DEFAULT_MAX_AGE_DAYS,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> None:
        self.db_path = Path(db_path)
        self.max_age_days = max_age_days
        self.max_rows = max_rows

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=1.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        return conn

    def record(self, batch: Batch, *, now: datetime | None = None) -> int:
        """Insert a validated batch, then prune; returns rows inserted.

        Raises ``sqlite3.Error`` (locked, full, unwritable) — the router turns
        that into a dropped batch.
        """
        return self.insert_rows(
            (
                (
                    format_ts(now or datetime.now(UTC)),
                    batch.session_id,
                    e.kind,
                    e.view_id,
                    e.control_id,
                    batch.device_class,
                    batch.os_class,
                )
                for e in batch.events
            ),
            now=now,
        )

    def insert_rows(
        self,
        rows: Iterable[tuple[str, str, str, str | None, str | None, str, str]],
        *,
        now: datetime | None = None,
    ) -> int:
        """Insert raw ``(ts, session_id, kind, view_id, control_id, device,
        os)`` rows and apply retention. Callers outside :meth:`record` are
        tests seeding history; the app path always goes through ``record``."""
        conn = self._connect()
        try:
            with conn:
                cur = conn.executemany(
                    "INSERT INTO events (ts, session_id, kind, view_id, control_id,"
                    " device_class, os_class) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    list(rows),
                )
                inserted = cur.rowcount
                self._prune(conn, now or datetime.now(UTC))
            return inserted
        finally:
            conn.close()

    def _prune(self, conn: sqlite3.Connection, now: datetime) -> None:
        cutoff = format_ts(now - timedelta(days=self.max_age_days))
        conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        conn.execute(
            "DELETE FROM events WHERE id <= ("
            " SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?)",
            (self.max_rows,),
        )

    def summary(self, days: int, *, now: datetime | None = None) -> dict[str, Any]:
        """Aggregate the last ``days`` days (see :func:`summarize`)."""
        conn = self._connect()
        try:
            return summarize(conn, days, now=now)
        finally:
            conn.close()


def open_read_only(db_path: Path) -> sqlite3.Connection:
    """Open an existing ``usage.sqlite3`` without the right to write it.

    Raises ``FileNotFoundError`` when the file is absent — "no data yet" must
    stay distinguishable from "zero usage" (an empty, existing table).
    """
    if not db_path.is_file():
        raise FileNotFoundError(str(db_path))
    return sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)


def _counts(conn: sqlite3.Connection, sql: str, params: Sequence[object]) -> dict[str, int]:
    return {str(key): int(n) for key, n in conn.execute(sql, params)}


def summarize(
    conn: sqlite3.Connection, days: int, *, now: datetime | None = None
) -> dict[str, Any]:
    """Per-view visits, per-control taps, device/OS split by session, totals.

    Session ids are counted, never returned.
    """
    end = now or datetime.now(UTC)
    since = format_ts(end - timedelta(days=days))
    where = "FROM events WHERE ts >= ?"
    params = (since,)
    totals = conn.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT session_id), MIN(ts), MAX(ts) {where}", params
    ).fetchone()
    return {
        "days": days,
        "since": since,
        "events": int(totals[0]),
        "sessions": int(totals[1]),
        "first_ts": totals[2],
        "last_ts": totals[3],
        "views": _counts(
            conn,
            f"SELECT view_id, COUNT(*) {where} AND kind = 'view'"
            " GROUP BY view_id ORDER BY COUNT(*) DESC, view_id",
            params,
        ),
        "controls": _counts(
            conn,
            f"SELECT control_id, COUNT(*) {where} AND kind = 'tap'"
            " GROUP BY control_id ORDER BY COUNT(*) DESC, control_id",
            params,
        ),
        "devices": _counts(
            conn,
            f"SELECT device_class, COUNT(DISTINCT session_id) {where}"
            " GROUP BY device_class ORDER BY device_class",
            params,
        ),
        "os": _counts(
            conn,
            f"SELECT os_class, COUNT(DISTINCT session_id) {where}"
            " GROUP BY os_class ORDER BY os_class",
            params,
        ),
    }


# ------------------------------------------------------------ foreground time
# The rules are numbered in the module docstring; comments below cite them.

#: Device classes whose intervals are trusted as-is (auto-lock hides the page).
_TRUSTED_DEVICES = frozenset({"phone", "tablet"})


@dataclass
class _Interval:
    """One foreground stretch of a visit: open row to closing row."""

    start: datetime
    order: int                          # id of the opening row (overlap tie-break)
    device_class: str
    points: list[datetime]              # every event time inside; the last is the end
    views: list[tuple[datetime, str]]   # (from, view) changes; the first is at start


@dataclass(frozen=True)
class _Slice:
    """A clipped piece of an interval with one device class and one view."""

    start: datetime
    end: datetime
    device_class: str
    view: str
    priority: tuple[datetime, int]      # most recently started interval wins


def _parse_ts(value: str) -> datetime:
    return datetime.strptime(value, _TS_FORMAT).replace(tzinfo=UTC)


def _local_date(moment: datetime, tz: tzinfo | None) -> date:
    return moment.astimezone(tz).date()


def _local_midnight(day: date, tz: tzinfo | None) -> datetime:
    # tz=None builds a naive local time, which astimezone() reads with the
    # host's own DST rules for that date.
    return datetime.combine(day, time(), tzinfo=tz).astimezone(UTC)


def local_window(
    days: int, tz: tzinfo | None = None, *, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """``(since, until)`` for the last ``days`` local days, today included.

    ``since`` is the local midnight ``days - 1`` days before today in ``tz``
    (``None`` = the host's local time); ``until`` is ``now``.
    """
    if days < 1:
        raise ValueError("days must be at least 1")
    end = (now or datetime.now(UTC)).astimezone(UTC)
    first = _local_date(end, tz) - timedelta(days=days - 1)
    return _local_midnight(first, tz), end


def _visit_intervals(rows: Iterable[tuple[int, str, str, str | None, str]]) -> list[_Interval]:
    """One visit's ``(id, ts, kind, view_id, device_class)`` rows, in ``id``
    order, → its intervals (rules 1–3). An interval ends at its last point, so
    "close at the last event seen" is simply not appending to it any more."""
    intervals: list[_Interval] = []
    current: _Interval | None = None
    view = NO_VIEW  # the visit's latest view, carried across its intervals
    for row_id, ts, kind, view_id, device_class in rows:
        moment = _parse_ts(ts)
        if kind == "view" and view_id:
            view = view_id
        if kind == "session_start" or current is None:
            current = _Interval(moment, row_id, device_class, [moment], [(moment, view)])
            intervals.append(current)
        else:
            current.points.append(moment)
            if kind == "view":
                current.views.append((moment, view))
        if kind == "session_end":
            current = None
    return intervals


def _active_spans(
    interval: _Interval, idle_gap_desktop: timedelta, max_interval: timedelta
) -> list[tuple[datetime, datetime]]:
    """The counted part of an interval (rule 4)."""
    if interval.device_class in _TRUSTED_DEVICES:
        end = min(interval.points[-1], interval.start + max_interval)
        return [(interval.start, end)]
    pts = interval.points
    return [(a, min(b, a + idle_gap_desktop)) for a, b in zip(pts, pts[1:], strict=False)]


def _view_slices(
    interval: _Interval, spans: Iterable[tuple[datetime, datetime]],
    since: datetime, until: datetime,
) -> Iterator[_Slice]:
    """Clip ``spans`` to the window and cut them at the interval's view changes."""
    views = interval.views
    for a, b in spans:
        a, b = max(a, since), min(b, until)
        for i, (view_from, view) in enumerate(views):
            view_to = views[i + 1][0] if i + 1 < len(views) else b
            lo, hi = max(a, view_from), min(b, view_to)
            if hi > lo:
                yield _Slice(lo, hi, interval.device_class, view,
                             (interval.start, interval.order))


def _sweep(
    slices: Sequence[_Slice], cuts: Iterable[datetime]
) -> Iterator[tuple[datetime, datetime, list[_Slice]]]:
    """Elementary pieces between every slice edge and cut, each with the
    slices covering all of it; pieces nothing covers are skipped."""
    points = sorted({s.start for s in slices} | {s.end for s in slices} | set(cuts))
    ordered = sorted(slices, key=lambda s: s.start)
    active: list[_Slice] = []
    nxt = 0
    for lo, hi in zip(points, points[1:], strict=False):
        while nxt < len(ordered) and ordered[nxt].start <= lo:
            active.append(ordered[nxt])
            nxt += 1
        active = [s for s in active if s.end > lo]
        if active:
            yield lo, hi, active


def _minutes(seconds: float) -> float:
    return round(seconds / 60, 1)


def _minute_map(seconds_by_key: Mapping[str, float]) -> dict[str, float]:
    ranked = sorted(seconds_by_key.items(), key=lambda kv: (-kv[1], kv[0]))
    return {key: _minutes(seconds) for key, seconds in ranked}


def foreground_time(
    conn: sqlite3.Connection,
    since: datetime,
    until: datetime,
    tz: tzinfo | None,
    *,
    idle_gap_desktop: timedelta = IDLE_GAP_DESKTOP,
    max_interval: timedelta = MAX_INTERVAL,
) -> dict[str, Any]:
    """Foreground minutes in ``[since, until)``: total, per device class and
    per view, for the window and for every local day in it (zero days
    included). ``tz`` is the day boundary's zone; ``None`` = host-local.
    The rules are in the module docstring. Session ids are never returned.
    """
    since, until = since.astimezone(UTC), until.astimezone(UTC)
    if until <= since:
        raise ValueError("until must be after since")
    # A visit counts only if it has an event this close to the window: neither
    # rule-4 bound lets an interval reach further past its last event.
    lookback = max(idle_gap_desktop, max_interval)
    rows = conn.execute(
        "SELECT session_id, id, ts, kind, view_id, device_class FROM events"
        " WHERE session_id IN (SELECT session_id FROM events WHERE ts >= ? AND ts <= ?)"
        " ORDER BY session_id, id",
        (format_ts(since - lookback), format_ts(until)),
    ).fetchall()

    slices: list[_Slice] = []
    for _, visit in groupby(rows, key=itemgetter(0)):
        for interval in _visit_intervals(row[1:] for row in visit):
            spans = _active_spans(interval, idle_gap_desktop, max_interval)
            slices.extend(_view_slices(interval, spans, since, until))

    first = _local_date(since, tz)
    last = _local_date(until - timedelta(microseconds=1), tz)
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    cuts = [since, until, *(_local_midnight(day, tz) for day in days[1:])]   # rule 5

    total: dict[date, float] = defaultdict(float)
    day_device: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    day_view: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    window_device: dict[str, float] = defaultdict(float)
    window_view: dict[str, float] = defaultdict(float)
    for lo, hi, active in _sweep(slices, cuts):                             # rule 6
        seconds = (hi - lo).total_seconds()
        day = _local_date(lo, tz)
        total[day] += seconds
        for device_class in {s.device_class for s in active}:
            day_device[day][device_class] += seconds
            window_device[device_class] += seconds
        view = max(active, key=lambda s: s.priority).view
        day_view[day][view] += seconds
        window_view[view] += seconds

    return {
        "days": len(days),
        "since": format_ts(since),
        "until": format_ts(until),
        "tz": str(tz) if tz is not None else (until.astimezone().tzname() or "local"),
        "rules": {
            "idle_gap_desktop_minutes": idle_gap_desktop.total_seconds() / 60,
            "max_interval_minutes": max_interval.total_seconds() / 60,
        },
        "total_minutes": _minutes(sum(total.values())),
        "by_device": _minute_map(window_device),
        "by_view": _minute_map(window_view),
        "days_series": [
            {
                "day": day.isoformat(),
                "minutes": _minutes(total[day]),
                "by_device": _minute_map(day_device[day]),
                "by_view": _minute_map(day_view[day]),
            }
            for day in days
        ],
    }


def usage_router(
    db_path: Path,
    *,
    dependencies: Sequence[Any] = (),
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> "APIRouter":
    """``POST /api/usage/events``, ``GET /api/usage/summary`` and
    ``GET /api/usage/time`` over ``db_path``.

    Pass the app's own auth as ``dependencies`` (or include the router where
    the app's auth middleware already applies) — every endpoint follows the
    app's existing rules, loopback bypass included.

    ``GET /api/usage/time?days=7&by=day|device|view`` is :func:`foreground_time`
    over the last ``days`` (1..366) host-local days; ``by`` picks the
    breakdown kept (``day`` keeps totals only). Both ``/summary`` and
    ``/time`` read the file read-only and never create it: an absent file
    answers the zero report with ``"log_exists": false``, so "no data yet"
    stays distinct from zero usage, and a locked file answers 503 rather
    than 500.
    """
    from fastapi import APIRouter, Query, Request
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool

    log = UsageLog(db_path, max_age_days=max_age_days, max_rows=max_rows)
    router = APIRouter(prefix="/api/usage", dependencies=list(dependencies))

    @router.post("/events")
    async def post_events(request: Request) -> JSONResponse:
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return JSONResponse({"detail": "batch too large"}, status_code=413)
        body = await request.body()
        try:
            batch = parse_batch(body)
        except PayloadError as exc:
            status = 413 if len(body) > MAX_BODY_BYTES else 400
            return JSONResponse({"detail": str(exc)}, status_code=status)
        if not batch.events:
            return JSONResponse({"accepted": 0, "rejected": batch.rejected})
        try:
            accepted = await run_in_threadpool(log.record, batch)
        except sqlite3.Error as exc:
            logger.info(
                "ℹ️ usage log: dropped %d event(s), database unavailable (%s)",
                len(batch.events), exc,
            )
            return JSONResponse({"detail": "usage log unavailable"}, status_code=503)
        return JSONResponse({"accepted": accepted, "rejected": batch.rejected})

    @router.get("/summary")
    def get_summary(days: int = Query(30, ge=1, le=3650)) -> JSONResponse:
        exists = log.db_path.is_file()
        try:
            if exists:
                conn = open_read_only(log.db_path)
            else:
                conn = sqlite3.connect(":memory:")
                conn.executescript(_SCHEMA)
            try:
                report = summarize(conn, days)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.info("ℹ️ usage log: summary unavailable (%s)", exc)
            return JSONResponse({"detail": "usage log unavailable"}, status_code=503)
        return JSONResponse({**report, "log_exists": exists})

    @router.get("/time")
    def get_time(
        days: int = Query(7, ge=1, le=MAX_TIME_DAYS),
        by: Literal["day", "device", "view"] = "day",
    ) -> JSONResponse:
        since, until = local_window(days)
        exists = log.db_path.is_file()
        try:
            if exists:
                conn = open_read_only(log.db_path)
            else:
                conn = sqlite3.connect(":memory:")
                conn.executescript(_SCHEMA)
            try:
                report = foreground_time(conn, since, until, None)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.info("ℹ️ usage log: foreground time unavailable (%s)", exc)
            return JSONResponse({"detail": "usage log unavailable"}, status_code=503)
        dropped = {"day": ("by_device", "by_view"), "device": ("by_view",),
                   "view": ("by_device",)}[by]
        for key in dropped:
            del report[key]
            for row in report["days_series"]:
                del row[key]
        return JSONResponse({**report, "by": by, "log_exists": exists})

    return router
