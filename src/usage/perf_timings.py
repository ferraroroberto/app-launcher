"""Real-phone load and slow-API timings: store, validation and summary.

The phone is sometimes slow and nothing said whether the time went to the
connection, the server or the page. ``app/webapp/static/perf-timing.js``
records two kinds of row from the real device and posts them here:

* ``load`` — one per page load: the document's Navigation Timing legs, when
  boot finished, and the boot request that landed last with its own split.
* ``api`` — one per ``/api/`` request whose total time reached
  :data:`SLOW_API_MS`, with the Resource Timing split. The ``server`` leg is
  the response's ``Server-Timing: app;dur=`` value
  (:class:`app.webapp.observability.SlowRequestLogMiddleware`), so
  ``wait - server`` is the network round trip.

**Privacy is enforced here, not trusted to the client.** A row is stored only
when its kind, classes and every number pass the fixed checks below; the path
is replaced by the server's own route template (``/api/sessions/{sid}``), so
an id, a file name or a query string can never reach the table, and a path no
route matches is dropped. No page text, IP, user agent or host is stored.

Each insert prunes rows past :data:`MAX_AGE_DAYS` and :data:`MAX_ROWS`. A
locked or full database drops the batch (the router answers 503, the client
ignores it): timings never break the app.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import statistics
from collections import defaultdict
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

#: Mirrors perf-timing.js: an API row below this total is never sent.
SLOW_API_MS = 800
#: No leg is longer than two minutes; anything else is a broken clock.
MAX_LEG_MS = 120_000
MAX_AGE_DAYS = 90
MAX_ROWS = 50_000
MAX_ROWS_PER_BATCH = 50
MAX_BODY_BYTES = 32_768
#: ``GET /api/perf/summary`` window bound, in days.
MAX_SUMMARY_DAYS = 90
#: How many of the slowest API paths the summary lists.
TOP_PATHS = 10

DEVICE_CLASSES = frozenset({"phone", "tablet", "desktop"})
OS_CLASSES = frozenset({"ios", "android", "windows", "other"})
DISPLAYS = frozenset({"pwa", "tab"})
NAV_TYPES = frozenset({"navigate", "reload", "back_forward", "prerender"})
CONN_TYPES = frozenset({"slow-2g", "2g", "3g", "4g"})
#: The client's asset hash: the ``?v=`` stamp on its own module URL.
ASSET_PATTERN = re.compile(r"[0-9a-f]{6,16}")
#: A stored path is a route template, so this only bounds its shape.
TEMPLATE_PATTERN = re.compile(r"/api(/[A-Za-z0-9._{}-]{1,64}){1,10}")

#: The legs each kind records, in display order. ``network`` is derived.
LEGS: dict[str, tuple[str, ...]] = {
    "load": ("dns", "connect", "tls", "ttfb", "download", "dom_ready", "boot",
             "last_wait", "last_server", "last_download"),
    "api": ("queue", "connect", "wait", "server", "download", "total"),
}
_LEG_COLUMNS = tuple(dict.fromkeys(leg for legs in LEGS.values() for leg in legs))

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS timings ("
    " id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL,"
    " build TEXT, asset TEXT, device_class TEXT NOT NULL, os_class TEXT NOT NULL,"
    " display TEXT, nav_type TEXT, cached INTEGER, conn_type TEXT,"
    " status INTEGER, path TEXT, "
    + ", ".join(f"{leg} REAL" for leg in _LEG_COLUMNS)
    + ");\nCREATE INDEX IF NOT EXISTS timings_ts ON timings (ts);\n"
)
_COLUMNS = ("ts", "kind", "build", "asset", "device_class", "os_class", "display",
            "nav_type", "cached", "conn_type", "status", "path") + _LEG_COLUMNS

#: ``raw_path -> route template or None``; the router supplies it.
Templater = Callable[[str], Optional[str]]


class PayloadError(ValueError):
    """The batch envelope itself is unusable."""


def format_ts(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(_TS_FORMAT)


def _choice(raw: Mapping[str, Any], key: str, allowed: frozenset[str]) -> Optional[str]:
    value = raw.get(key)
    return value if isinstance(value, str) and value in allowed else None


def _ms(value: object) -> Optional[float]:
    """A leg in ms, or ``None`` when absent; raises on an out-of-range number."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("leg is not a number")
    if not 0 <= value <= MAX_LEG_MS:
        raise ValueError("leg out of range")
    return round(float(value), 1)


def _template(templater: Templater, raw: object) -> Optional[str]:
    if not isinstance(raw, str) or not raw.startswith("/api/") or len(raw) > 512:
        return None
    template = templater(raw.split("?", 1)[0])
    if template is None or TEMPLATE_PATTERN.fullmatch(template) is None:
        return None
    return template


def validate_row(raw: object, templater: Templater) -> Optional[dict[str, Any]]:
    """One client row → a storable dict, or ``None`` to drop it.

    Keys outside the kind's legs and fields are never read.
    """
    if not isinstance(raw, Mapping):
        return None
    kind = raw.get("kind")
    if kind not in LEGS:
        return None
    try:
        legs = {leg: _ms(raw.get(leg)) for leg in LEGS[kind]}
    except ValueError:
        return None
    row: dict[str, Any] = {"kind": kind, **legs}
    if kind == "load":
        cached = raw.get("cached")
        row["nav_type"] = _choice(raw, "nav_type", NAV_TYPES)
        row["cached"] = int(cached) if isinstance(cached, bool) else None
        last = raw.get("last_path")
        row["path"] = None if last is None else _template(templater, last)
        if last is not None and row["path"] is None:
            for leg in ("last_wait", "last_server", "last_download"):
                row[leg] = None
        return row
    # Resource Timing exposes no method, and a status only on some engines
    # (0 = unknown).
    status = raw.get("status")
    if legs["total"] is None or legs["total"] < SLOW_API_MS:
        return None
    if isinstance(status, bool) or not isinstance(status, int) or not 0 <= status <= 599:
        return None
    path = _template(templater, raw.get("path"))
    if path is None or path == "/api/perf/timings":
        return None
    row.update(status=status, path=path)
    return row


def parse_batch(body: bytes, templater: Templater) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """Decode a ``POST /api/perf/timings`` body.

    Returns ``(envelope, rows, rejected)``; raises :class:`PayloadError` for an
    unusable envelope.
    """
    if len(body) > MAX_BODY_BYTES:
        raise PayloadError("batch too large")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise PayloadError("body is not JSON") from exc
    if not isinstance(data, Mapping):
        raise PayloadError("body is not a JSON object")
    device = _choice(data, "device_class", DEVICE_CLASSES)
    os_class = _choice(data, "os_class", OS_CLASSES)
    if device is None or os_class is None:
        raise PayloadError("bad device or OS class")
    raw_rows = data.get("rows")
    if not isinstance(raw_rows, list) or len(raw_rows) > MAX_ROWS_PER_BATCH:
        raise PayloadError("bad rows list")
    asset = data.get("asset")
    envelope = {
        "device_class": device,
        "os_class": os_class,
        "display": _choice(data, "display", DISPLAYS),
        "conn_type": _choice(data, "conn_type", CONN_TYPES),
        "asset": asset if isinstance(asset, str) and ASSET_PATTERN.fullmatch(asset) else None,
    }
    rows = [r for r in (validate_row(raw, templater) for raw in raw_rows) if r is not None]
    return envelope, rows, len(raw_rows) - len(rows)


class PerfTimings:
    """The ``timings`` table behind ``perf.sqlite3``; one short connection per call."""

    def __init__(self, db_path: Path, *, max_age_days: int = MAX_AGE_DAYS, max_rows: int = MAX_ROWS) -> None:
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

    def record(
        self,
        envelope: Mapping[str, Any],
        rows: list[dict[str, Any]],
        *,
        build: Optional[str],
        now: Optional[datetime] = None,
    ) -> int:
        """Insert validated rows stamped with ``build``, then prune.

        Raises ``sqlite3.Error``; the router turns that into a dropped batch.
        """
        moment = now or datetime.now(UTC)
        stamp = {"ts": format_ts(moment), "build": build, **envelope}
        values = [tuple({**stamp, **row}.get(col) for col in _COLUMNS) for row in rows]
        conn = self._connect()
        try:
            with conn:
                conn.executemany(
                    f"INSERT INTO timings ({', '.join(_COLUMNS)})"
                    f" VALUES ({', '.join('?' for _ in _COLUMNS)})",
                    values,
                )
                conn.execute(
                    "DELETE FROM timings WHERE ts < ?",
                    (format_ts(moment - timedelta(days=self.max_age_days)),),
                )
                conn.execute(
                    "DELETE FROM timings WHERE id <= ("
                    " SELECT id FROM timings ORDER BY id DESC LIMIT 1 OFFSET ?)",
                    (self.max_rows,),
                )
            return len(values)
        finally:
            conn.close()

    def summary(self, days: int, *, now: Optional[datetime] = None) -> dict[str, Any]:
        conn = self._connect()
        try:
            return summarize(conn, days, now=now)
        finally:
            conn.close()


def open_read_only(db_path: Path) -> sqlite3.Connection:
    """Open an existing ``perf.sqlite3`` read-only; ``FileNotFoundError`` when
    absent, so "no data yet" stays distinct from "no slow requests"."""
    if not db_path.is_file():
        raise FileNotFoundError(str(db_path))
    return sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile of a non-empty list."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def leg_stats(values: list[Optional[float]]) -> dict[str, Any]:
    """``{"n", "median", "p95"}`` over the known values; ``n == 0`` means the
    leg is unknown (never reported as zero)."""
    known = [v for v in values if v is not None]
    if not known:
        return {"n": 0, "median": None, "p95": None}
    return {"n": len(known), "median": round(statistics.median(known), 1),
            "p95": round(percentile(known, 0.95), 1)}


def _network(wait: Optional[float], server: Optional[float]) -> Optional[float]:
    return None if wait is None or server is None else max(0.0, wait - server)


def _with_network(kind: str, row: Mapping[str, Any]) -> dict[str, Optional[float]]:
    legs = {leg: row[leg] for leg in LEGS[kind]}
    if kind == "api":
        legs["network"] = _network(row["wait"], row["server"])
    else:
        legs["last_network"] = _network(row["last_wait"], row["last_server"])
    return legs


def _by_device(kind: str, rows: list[sqlite3.Row]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Optional[float]]]] = defaultdict(list)
    for row in rows:
        grouped[row["device_class"]].append(_with_network(kind, row))
    out = {}
    for device in sorted(grouped):
        legs = grouped[device]
        out[device] = {
            "rows": len(legs),
            "legs": {leg: leg_stats([r[leg] for r in legs]) for leg in legs[0]},
        }
    return out


def summarize(conn: sqlite3.Connection, days: int, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """Median and p95 per leg, per kind and device class, plus the slowest
    API paths and which request lands last on boot."""
    since = format_ts((now or datetime.now(UTC)) - timedelta(days=days))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM timings WHERE ts >= ? ORDER BY id", (since,)).fetchall()
    loads = [r for r in rows if r["kind"] == "load"]
    apis = [r for r in rows if r["kind"] == "api"]

    by_path: dict[str, list[dict[str, Optional[float]]]] = defaultdict(list)
    for row in apis:
        by_path[row["path"]].append(_with_network("api", row))
    paths = [
        {"path": path, "rows": len(legs),
         "total": leg_stats([r["total"] for r in legs]),
         "server": leg_stats([r["server"] for r in legs]),
         "network": leg_stats([r["network"] for r in legs])}
        for path, legs in by_path.items()
    ]
    paths.sort(key=lambda p: (-(p["total"]["p95"] or 0), p["path"]))

    last_paths: dict[str, int] = defaultdict(int)
    for row in loads:
        last_paths[row["path"] or "(none)"] += 1
    return {
        "days": days,
        "since": since,
        "rows": len(rows),
        "builds": sorted({r["build"] for r in rows if r["build"]}),
        "loads": _by_device("load", loads),
        "api": _by_device("api", apis),
        "slowest_paths": paths[:TOP_PATHS],
        "boot_last_paths": dict(sorted(last_paths.items(), key=lambda kv: (-kv[1], kv[0]))),
    }
