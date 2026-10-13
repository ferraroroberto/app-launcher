"""Local-only usage log for a fleet web app: views, taps, device class (#349).

Design reviews used to ask the owner "how often do you use X?" because no app
recorded which views or controls were actually used. This module is the
server half of the ``usage-log`` convention: a FastAPI router that accepts the
vendored client's batched events (``app/webapp/static/_vendored/usage-log/``)
into one SQLite file on the machine that serves the app, and a summary query
that ``scripts/usage_summary.py`` and ``GET /api/usage/summary`` both read.

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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

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


def usage_router(
    db_path: Path,
    *,
    dependencies: Sequence[Any] = (),
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> "APIRouter":
    """``POST /api/usage/events`` + ``GET /api/usage/summary`` over ``db_path``.

    Pass the app's own auth as ``dependencies`` (or include the router where
    the app's auth middleware already applies) — both endpoints follow the
    app's existing rules, loopback bypass included.
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
    def get_summary(days: int = Query(30, ge=1, le=3650)) -> dict[str, Any]:
        return log.summary(days)

    return router
