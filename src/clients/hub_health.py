"""The PC health verdict from local-llm-hub, for the Code header's chip.

The hub owns the measurement, thresholds and alerts (its health watch samples
leaks and pressure on this machine). The launcher only reads
``GET /admin/api/machines/health`` over loopback and shows the verdict; it
never judges the machine itself.

**Unknown is its own state.** A hub that is not configured, does not answer,
answers with something that is not a verdict, or itself reports ``unknown``
reads ``unknown`` with a ``why``; none of them ever reads ``ok``.

The hub serves the answer from memory and re-samples every ten minutes, so a
short cache here keeps the page's poll from reaching it on every tick. A
failed read is cached for less time than a good one, so a hub that comes back
is noticed quickly without a down hub costing a connect timeout per poll.

Transitions into and out of ``unknown`` leave one breadcrumb each on the
``launcher.slowreq`` logger: the live webapp's own stdout is discarded by the
tray, and that logger persists to ``webapp/slow-requests.log``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from src.clients import _loopback_http

breadcrumbs = logging.getLogger("launcher.slowreq")

VERDICTS = ("ok", "watch", "act")
VERDICT_UNKNOWN = "unknown"

OK_CACHE_SECONDS = 30.0
FAILED_CACHE_SECONDS = 10.0
_TIMEOUT = 3.0
# A reason or fix is one line for a chip and a sheet row; the hub's own text
# is short, this only keeps a surprising payload from filling the sheet.
_MAX_TEXT = 240
# The act findings that make a machine critical: ephemeral ports (the slow
# sample's backstop, the port watch's percents and the Tcpip exhaustion events
# `tcpip_<id>`), commit charge and a physical disk saturated for minutes (a
# starved disk froze a machine with no other warning). Handles, pools and
# TIME_WAIT alone are not.
PORT_METRICS = frozenset({"tcp_percent", "udp_percent", "ephemeral_ports_percent"})
COMMIT_METRICS = frozenset({"commit_percent"})
DISK_METRICS = frozenset({"disk_load"})
_EVENT_METRIC_PREFIX = "tcpip_"
_MAX_HOLDERS = 5
_MAX_MEMORY_OFFENDERS = 3
_MAX_DISKS = 8
_MAX_DRIVES = 4
# The hub's per-disk states; anything else reads unknown, never ok.
_DISK_STATES = frozenset({"ok", "watch", "alert", "unknown"})


class HubHealthError(_loopback_http.LoopbackError):
    """Raised when the hub is unreachable or answers with an error."""


_lock = threading.Lock()
_cached: Optional[Dict[str, Any]] = None
_cached_until = 0.0
_was_unknown = False


def _unknown(why: str, reason: Optional[str] = None) -> Dict[str, Any]:
    return {"verdict": VERDICT_UNKNOWN, "why": why, "reason": reason, "fix": None}


def _text(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    return " ".join(value.split())[:_MAX_TEXT]


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _rows(rows: Any, limit: int, keys: Iterable[str]) -> List[Dict[str, Any]]:
    """The first ``limit`` process rows with only ``keys``, text trimmed."""
    out: List[Dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or len(out) >= limit:
            continue
        out.append({k: _text(row.get(k)) if k == "name" else _num(row.get(k)) for k in keys})
    return out


def is_critical_metric(metric: Any) -> bool:
    """Whether an act finding on ``metric`` is one the chief is woken for."""
    return isinstance(metric, str) and (
        metric in PORT_METRICS or metric in COMMIT_METRICS or metric in DISK_METRICS
        or metric.startswith(_EVENT_METRIC_PREFIX)
    )


def _disks(body: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """One row per physical disk the hub measured, or ``None`` when the payload
    has no usable ``disks`` block (an older hub, or a malformed one): the sheet
    then shows no disk rows, never a healthy-looking empty list. A row whose
    state is missing or unrecognised reads ``unknown``; a reading that is not a
    number is ``None``, never 0."""
    rows = body.get("disks")
    if not isinstance(rows, list):
        return None
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or len(out) >= _MAX_DISKS:
            continue
        drives = [d for d in (_text(x) for x in (row.get("drives") if isinstance(row.get("drives"), list) else []))
                  if d][:_MAX_DRIVES]
        disk = _text(row.get("disk"))
        if not drives and not disk:
            continue
        state = row.get("state")
        out.append({
            "disk": disk,
            "drives": drives,
            "state": state if state in _DISK_STATES else "unknown",
            "busy_percent": _num(row.get("busy_percent")),
            "queue": _num(row.get("queue")),
            "read_ms": _num(row.get("read_ms")),
            "write_ms": _num(row.get("write_ms")),
        })
    return out or None


def _machine(body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """What the machine row and sheet draw beyond the verdict: the worst ports
    percent, commit percent, the port holders, the last exhaustion event, the
    per-disk load and the act findings on a critical metric. ``None`` when the
    payload carries none of them. A stale or missing port block leaves ``ports_percent`` unset, never 0."""
    system = body.get("system") if isinstance(body.get("system"), dict) else {}
    ports = body.get("ports") if isinstance(body.get("ports"), dict) else {}
    percents = [
        _num(ports[proto].get("percent")) for proto in ("tcp", "udp")
        if ports.get("stale") is not True and isinstance(ports.get(proto), dict)
    ]
    percents = [p for p in percents if p is not None]
    offenders = body.get("offenders") if isinstance(body.get("offenders"), dict) else {}
    holders = _rows(ports.get("top_holders") or offenders.get("ports"), _MAX_HOLDERS,
                    ("name", "pid", "value", "tcp", "udp"))
    event = ports.get("last_event") if isinstance(ports.get("last_event"), dict) else None
    findings = body.get("findings") if isinstance(body.get("findings"), list) else []
    disks = _disks(body)
    critical = [
        {"metric": f["metric"], "value": f.get("value") if isinstance(f.get("value"), (int, float)) else None}
        for f in findings
        if isinstance(f, dict) and f.get("level") == "act" and is_critical_metric(f.get("metric"))
    ]
    machine = {
        "host": _text(body.get("host_id")),
        "ports_percent": max(percents) if percents else None,
        "commit_percent": _num(system.get("commit_percent")),
        "holders": holders,
        "memory_holders": _rows(offenders.get("memory_mb"), _MAX_MEMORY_OFFENDERS, ("name", "pid", "value")),
        "last_event": None if event is None else {
            "ts": _num(event.get("ts")), "event_id": _num(event.get("event_id")),
            "protocol": _text(event.get("protocol")), "reason": _text(event.get("reason")),
        },
        "disks": disks,
        "critical": critical,
    }
    measured = (machine["ports_percent"], machine["commit_percent"], machine["last_event"], disks)
    if all(v is None for v in measured) and not holders and not critical:
        return None
    return machine


def parse(body: Any) -> Dict[str, Any]:
    """The hub's payload as the launcher shows it. Anything that is not a
    recognised verdict is ``unknown``, never ``ok``."""
    if not isinstance(body, dict):
        return _unknown("unreadable")
    verdict = body.get("verdict")
    if verdict in VERDICTS:
        out: Dict[str, Any] = {
            "verdict": verdict,
            "why": None,
            "reason": _text(body.get("reason")),
            "fix": _text(body.get("fix")),
        }
        machine = _machine(body)
        if machine is not None:
            out["machine"] = machine
        return out
    if verdict == VERDICT_UNKNOWN:
        return _unknown("hub", _text(body.get("reason")))
    return _unknown("unreadable")


def health_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/admin/api/machines/health"


def _fetch(base_url: str) -> Dict[str, Any]:
    try:
        body = _loopback_http.request(
            "GET", health_url(base_url), error=HubHealthError, service="local-llm-hub",
            timeout=_TIMEOUT, allow_empty=False,
        )
    except HubHealthError as exc:
        return _unknown("unreachable", str(exc))
    return parse(body)


def _note_transition(reading: Dict[str, Any]) -> None:
    global _was_unknown
    now_unknown = reading["verdict"] == VERDICT_UNKNOWN
    if now_unknown and not _was_unknown:
        breadcrumbs.info("⚠️ hub health unknown (%s): %s", reading.get("why"), reading.get("reason") or "no detail")
    elif _was_unknown and not now_unknown:
        breadcrumbs.info("✅ hub health readable again: %s", reading["verdict"])
    _was_unknown = now_unknown


def read(base_url: str, now: Optional[float] = None) -> Dict[str, Any]:
    """The cached verdict, fetched when the cache is cold. Blocking."""
    global _cached, _cached_until
    base = (base_url or "").strip()
    if not base:
        return _unknown("disabled")
    now = time.monotonic() if now is None else now
    with _lock:
        if _cached is not None and now < _cached_until:
            return dict(_cached)
    reading = _fetch(base)
    ttl = FAILED_CACHE_SECONDS if reading["verdict"] == VERDICT_UNKNOWN else OK_CACHE_SECONDS
    with _lock:
        _note_transition(reading)
        _cached = reading
        _cached_until = now + ttl
    return dict(reading)


def invalidate() -> None:
    """Drop the cached verdict so the next read asks the hub again."""
    global _cached, _cached_until
    with _lock:
        _cached = None
        _cached_until = 0.0


def reset() -> None:
    """Drop all state. Tests only."""
    global _cached, _cached_until, _was_unknown
    with _lock:
        _cached = None
        _cached_until = 0.0
        _was_unknown = False
