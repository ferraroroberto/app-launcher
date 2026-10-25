"""Provider-aware quota views backed by fleet-config's shared contract.

The schema, validation, freshness rules, and native collectors remain owned by
``fleet-config/skills/_lib``.  This module only selects an exact
harness/provider source for the launcher and removes account identifiers from
the browser payload.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread
from types import ModuleType
from typing import Any, Callable, Dict, Optional

from src._fleet_contract import load_fleet_contract
from src.env_path import effective_path
from src.launching.model_catalog import PI_MODEL_SPECS
from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)

REFRESH_COOLDOWN_SECONDS = 600.0


@dataclass(frozen=True)
class QuotaRoute:
    """The measured provider account a selected harness route would use."""

    harness: str
    provider: str
    label: str


def resolve_quota_route(selection: str, *, pi_model: Optional[str] = None) -> QuotaRoute:
    """Resolve a provider-qualified UI choice without claiming shared identity."""
    harness, _, model = str(selection or "").partition(":")
    if harness == "claude":
        return QuotaRoute("claude", "anthropic", "Claude")
    if harness == "codex":
        return QuotaRoute("codex", "openai", "Codex")
    if harness == "pi":
        spec = PI_MODEL_SPECS.get(model or str(pi_model or ""), {})
        provider = {
            "claude-agent-sdk": "anthropic",
            "openai-codex": "openai",
        }.get(str(spec.get("provider") or ""), "unknown")
        return QuotaRoute("pi", provider, "Pi")
    if harness == "grok":
        return QuotaRoute("grok", "xai", "Grok")
    return QuotaRoute(harness or "unknown", "unknown", "Quota")


def _load_contract(path_text: str) -> ModuleType:
    return load_fleet_contract(path_text, "launcher_quota_snapshot", "quota")


def _read_snapshot(fleet_config_dir: Path, state_dir: Path) -> Dict[str, Any]:
    contract = fleet_config_dir / "skills" / "_lib" / "quota_snapshot.py"
    module = _load_contract(str(contract.resolve()))
    return module.read_snapshot(state_dir)


def _empty_view(route: QuotaRoute, state: str, reason: str) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "harness": route.harness,
        "provider": route.provider,
        "label": route.label,
        "state": state,
        "reason": reason,
        "checked_at": None,
        "observations": [],
        # Legacy keys remain during the Claude-only client migration.
        "available": False,
        "stale": state == "stale",
        "updated_at": None,
        "five_hour": None,
        "seven_day": None,
    }


def _legacy_window(window: Dict[str, Any]) -> Dict[str, Any]:
    reset = window.get("resets_at")
    if isinstance(reset, str):
        try:
            reset = int(datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp())
        except ValueError:
            reset = None
    return {"used_percentage": window.get("used_percentage"), "resets_at": reset}


def _source_view(
    snapshot: Dict[str, Any], route: QuotaRoute, source: Dict[str, Any]
) -> Dict[str, Any]:
    pools = {
        item.get("pool_id"): item
        for item in snapshot.get("pools", [])
        if isinstance(item, dict) and item.get("pool_id")
    }
    observations = []
    for item in source.get("observations", []):
        if not isinstance(item, dict):
            continue
        pool = pools.get(item.get("pool_id"))
        harnesses = sorted(
            str(value) for value in (pool or {}).get("harnesses", [])
            if isinstance(value, str)
        )
        observations.append({
            "bucket": item.get("bucket"),
            "state": item.get("state"),
            "observed_at": item.get("observed_at"),
            "expires_at": item.get("expires_at"),
            "account_state": (item.get("account") or {}).get("state", "unknown"),
            "shared_account": bool(item.get("pool_id") and len(harnesses) > 1),
            "harnesses": harnesses,
            "windows": [
                {
                    "id": window.get("id"),
                    "duration_minutes": window.get("duration_minutes"),
                    "used_percentage": window.get("used_percentage"),
                    "resets_at": window.get("resets_at"),
                    "state": window.get("state"),
                }
                for window in item.get("windows", [])
                if isinstance(window, dict)
            ],
        })

    state = str(source.get("state") or "unknown")
    view = _empty_view(route, state, str(source.get("reason") or "source_unknown"))
    view.update(
        checked_at=source.get("checked_at"),
        observations=observations,
        available=state == "available",
        stale=state == "stale",
    )
    observed = [item.get("observed_at") for item in observations if item.get("observed_at")]
    view["updated_at"] = max(observed) if observed else source.get("checked_at")

    if route.harness == "claude":
        windows = {
            window.get("id"): window
            for item in observations
            for window in item.get("windows", [])
        }
        for name in ("five_hour", "seven_day"):
            if isinstance(windows.get(name), dict):
                view[name] = _legacy_window(windows[name])
    return view


def _legacy_view(
    route: QuotaRoute, legacy: Dict[str, Any]
) -> Dict[str, Any]:
    """Adapt only an absent canonical Claude shard, preserving old API keys."""
    view = _empty_view(route, "unknown", "legacy_observation_time_missing")
    view.update(
        available=bool(legacy.get("available")),
        stale=bool(legacy.get("stale")),
        updated_at=legacy.get("updated_at"),
        five_hour=legacy.get("five_hour"),
        seven_day=legacy.get("seven_day"),
    )
    if not legacy.get("available"):
        view["reason"] = "source_absent"
        return view
    # The old reader deliberately accepts an empty object as available. Keep
    # that compatibility bit, but never promote an unknown timestamp to fresh.
    if not legacy.get("updated_at"):
        return view
    state = "stale" if legacy.get("stale") else "available"
    observations = []
    windows = []
    for name, minutes in (("five_hour", 300), ("seven_day", 10080)):
        item = legacy.get(name)
        if not isinstance(item, dict):
            continue
        windows.append({
            "id": name,
            "duration_minutes": minutes,
            "used_percentage": item.get("used_percentage"),
            "resets_at": item.get("resets_at"),
            "state": state if item.get("used_percentage") is not None else "unknown",
        })
    if windows:
        observations.append({
            "bucket": "claude-code",
            "state": state,
            "observed_at": legacy.get("updated_at"),
            "expires_at": None,
            "account_state": "unknown",
            "shared_account": False,
            "harnesses": [],
            "windows": windows,
        })
    view.update(state=state, reason="legacy_native_observation", observations=observations)
    return view


def _view_from_snapshot(
    snapshot: Dict[str, Any],
    route: QuotaRoute,
    legacy_reader: Optional[Callable[[], Dict[str, Any]]],
) -> Dict[str, Any]:
    candidates = [
        source for source in snapshot.get("sources", [])
        if isinstance(source, dict)
        and source.get("harness") == route.harness
        and source.get("provider") == route.provider
    ]
    source = max(candidates, key=lambda item: str(item.get("checked_at") or ""), default=None)
    if source is None:
        return _empty_view(route, "unknown", "source_absent")
    if (
        route.harness == "claude"
        and source.get("state") == "unknown"
        and source.get("reason") == "source_absent"
        and legacy_reader is not None
    ):
        return _legacy_view(route, legacy_reader())
    return _source_view(snapshot, route, source)


# --------------------------------------------------------- compact lines
#
# The selection-scoped view above answers "what does the harness I picked
# have left". What the launcher shows is the opposite: both heavy agents at
# once, so the number you need before choosing one is never the hidden one.
# Built on the same per-harness view, but off a single
# snapshot read — this runs on the tabs' 5s poll, so reading the shard
# directory once per harness would double that cost for nothing.

QUOTA_LINE_HARNESSES = (("claude", "Claude Code"), ("codex", "Codex"))

# Duration classes shown on a compact line. Native window *ids* differ per
# harness (Claude: five_hour/seven_day, Codex: primary/secondary), so match
# on the duration the provider itself reports, never on the id.
_LINE_WINDOWS = (("five_hour", 300), ("weekly", 10080))


def _worst_window(observations: list, minutes: int) -> Optional[Dict[str, Any]]:
    """Highest used percentage across every window of one duration class.

    Codex reports several buckets at the same duration (``codex``,
    ``codex_bengalfox``, ``base_model_inference``); the one that constrains
    you is the fullest, and collapsing to it keeps internal bucket ids out
    of the UI. Ties and unmeasured windows degrade to ``None`` rather than
    inventing a zero.
    """
    best: Optional[Dict[str, Any]] = None
    for observation in observations:
        for window in observation.get("windows", []):
            if window.get("duration_minutes") != minutes:
                continue
            pct = window.get("used_percentage")
            if not isinstance(pct, (int, float)) or isinstance(pct, bool):
                continue
            if best is None or pct > best["used_percentage"]:
                best = {
                    "used_percentage": pct,
                    "resets_at": window.get("resets_at"),
                    # The window length, so the UI can place "now" inside it
                    # without hardcoding 7 days.
                    "duration_minutes": minutes,
                }
    return best


def _quota_line(view: Dict[str, Any], label: str) -> Dict[str, Any]:
    observations = [
        item for item in view.get("observations", []) if isinstance(item, dict)
    ]
    line = {
        "harness": view.get("harness"),
        "provider": view.get("provider"),
        "label": label,
        "state": view.get("state"),
        "reason": view.get("reason"),
        "stale": bool(view.get("stale")),
        "updated_at": view.get("updated_at"),
    }
    for name, minutes in _LINE_WINDOWS:
        line[name] = _worst_window(observations, minutes)
    return line


def read_quota_lines(
    fleet_config_dir: Path,
    state_dir: Path,
    *,
    legacy_reader: Optional[Callable[[], Dict[str, Any]]] = None,
) -> list:
    """One compact row per heavy agent, in fixed order, always both.

    A harness whose source is absent or unreadable still yields its row with
    a non-available ``state`` — the caller renders it degraded rather than
    dropping it, so the two lines never collapse into one. A contract read
    that fails degrades *both* rows the same way, never silently one.
    """
    routes = [
        (resolve_quota_route(harness), label)
        for harness, label in QUOTA_LINE_HARNESSES
    ]
    try:
        snapshot = _read_snapshot(fleet_config_dir, state_dir)
    except (ImportError, OSError, AttributeError, TypeError, ValueError):
        logger.info("Quota contract read unavailable")
        return [
            _quota_line(_empty_view(route, "error", "consumer_contract_unavailable"), label)
            for route, label in routes
        ]
    return [
        _quota_line(
            _view_from_snapshot(
                snapshot, route,
                legacy_reader if route.harness == "claude" else None,
            ),
            label,
        )
        for route, label in routes
    ]


# --------------------------------------------------------- Grok's line
#
# Grok exposes no session or weekly allowance to scripts (its status-line
# guide says the payload carries no rate-limit summary; the allowance lives
# only in the interactive /usage modal), so its row never draws a bar. What
# Grok does persist is a per-session cost ledger, read through its own
# documented `grok usage <session-id>` command, so the row shows today's
# spend from that and nothing else.

# `costUsdTicks` is 10^10 ticks per USD (Grok's sessions guide).
GROK_TICKS_PER_USD = 10_000_000_000
# One sweep spawns `grok usage` once per session touched today; the 5 s poll
# reuses the result for this long instead of respawning them all.
GROK_COST_TTL_SECONDS = 120.0
GROK_USAGE_TIMEOUT_SECONDS = 10

_grok_cache_lock = Lock()
_grok_cache: Dict[str, Any] = {}


def grok_home() -> Path:
    """Grok's state folder: ``GROK_HOME`` when set, else ``~/.grok``."""
    override = os.environ.get("GROK_HOME", "").strip()
    return Path(override) if override else Path.home() / ".grok"


def _grok_session_ids(home: Path, since: float) -> Optional[list]:
    """Ids of the sessions touched since ``since`` (epoch seconds), from the
    folder names alone; ``None`` when there is no sessions folder to list.

    Sessions sit at ``sessions/<encoded cwd>/<id>/``. A folder's own mtime
    can lag a rewrite of a file inside it, so its ``summary.json`` is
    stat'ed too (never read: the ledger comes from ``grok usage``)."""
    root = home / "sessions"
    if not root.is_dir():
        return None
    ids = []
    for cwd_dir in root.iterdir():
        if not cwd_dir.is_dir():
            continue
        for session_dir in cwd_dir.iterdir():
            try:
                stamps = [session_dir.stat().st_mtime]
                summary = session_dir / "summary.json"
                if summary.exists():
                    stamps.append(summary.stat().st_mtime)
            except OSError:
                continue
            if session_dir.is_dir() and max(stamps) >= since:
                ids.append(session_dir.name)
    return ids


def _run_grok_usage(exe: str, session_id: str) -> Optional[Dict[str, Any]]:
    """One session's ledger as ``grok usage`` prints it, or ``None``."""
    try:
        result = subprocess.run(
            [exe, "usage", session_id],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GROK_USAGE_TIMEOUT_SECONDS,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        logger.info("ℹ️ grok usage: process failed for one session")
        return None
    if result.returncode != 0:
        logger.info(f"ℹ️ grok usage: exit {result.returncode} for one session")
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        logger.info("ℹ️ grok usage: output was not JSON")
        return None
    return data if isinstance(data, dict) else None


def _parse_instant(text: Any) -> Optional[datetime]:
    if not isinstance(text, str):
        return None
    # Grok writes nanoseconds; fromisoformat takes at most microseconds.
    head, dot, tail = text.partition(".")
    if dot:
        digits = len(tail) - len(tail.lstrip("0123456789"))
        text = f"{head}.{tail[:min(digits, 6)]}{tail[digits:]}"
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else None


def grok_cost_today(
    home: Path,
    exe: Optional[str],
    *,
    now: Optional[datetime] = None,
    runner: Callable[[str, str], Optional[Dict[str, Any]]] = _run_grok_usage,
) -> Dict[str, Any]:
    """Today's Grok spend (local midnight onward) and the state it is in.

    ``available`` with ``usd`` summed over every turn that ended today;
    ``unsupported`` when the CLI or its sessions folder is missing;
    ``unknown`` when any session's ledger could not be read, because a
    partial sum would understate the day. A resumed or forked session
    carries its parent's turns, so a turn is counted once per
    (end time, cost) pair.
    """
    local_now = (now or datetime.now(timezone.utc)).astimezone()
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    if not exe:
        return {"state": "unsupported", "reason": "grok_not_installed", "usd": None}
    ids = _grok_session_ids(home, midnight.timestamp())
    if ids is None:
        return {"state": "unsupported", "reason": "grok_sessions_absent", "usd": None}
    seen = set()
    ticks = 0
    for session_id in ids:
        data = runner(exe, session_id)
        if data is None:
            return {"state": "unknown", "reason": "grok_usage_unreadable", "usd": None}
        for turn in data.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            ended = _parse_instant(turn.get("endedAt"))
            cost = turn.get("costUsdTicks")
            if ended is None or ended < midnight:
                continue
            if not isinstance(cost, int) or isinstance(cost, bool):
                continue
            key = (turn.get("endedAt"), cost)
            if key in seen:
                continue
            seen.add(key)
            ticks += cost
    return {"state": "available", "reason": "grok_usage_ledger", "usd": ticks / GROK_TICKS_PER_USD}


def _grok_line(cost: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "harness": "grok",
        "provider": "xai",
        "label": "Grok",
        "state": "unsupported",
        "reason": "limits_not_exposed",
        "stale": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "five_hour": None,
        "weekly": None,
        "cost_today": cost,
    }


def _sweep_grok_cost() -> None:
    try:
        exe = shutil.which("grok", path=effective_path())
        cost = grok_cost_today(grok_home(), exe)
    except Exception:  # noqa: BLE001 - a failed sweep must still clear in_flight
        logger.exception("❌ Grok cost sweep failed")
        cost = {"state": "unknown", "reason": "grok_usage_unreadable", "usd": None}
    with _grok_cache_lock:
        _grok_cache.update(line=_grok_line(cost), at=time.monotonic(), in_flight=False)


def grok_quota_line(
    *,
    now: Optional[float] = None,
    start: Optional[Callable[[Callable[[], None]], None]] = None,
) -> Dict[str, Any]:
    """Grok's usage row: no windows ever, today's cost when it can be read.

    The row's own ``state`` is ``unsupported`` (limits not exposed), which
    the meter keeps as text; the cost has a state of its own. A sweep spawns
    the CLI once per session touched today (seconds, on a busy day), so it
    runs on a background thread at most every :data:`GROK_COST_TTL_SECONDS`
    and the poll returns the last result, ``unknown`` until the first lands.
    """
    tick = time.monotonic() if now is None else now
    with _grok_cache_lock:
        cached = _grok_cache.get("line")
        fresh = cached is not None and tick - _grok_cache["at"] < GROK_COST_TTL_SECONDS
        launch = not fresh and not _grok_cache.get("in_flight")
        if launch:
            _grok_cache["in_flight"] = True
    if launch:
        (start or (lambda fn: Thread(target=fn, daemon=True).start()))(_sweep_grok_cost)
    if cached is None:
        return _grok_line({"state": "unknown", "reason": "grok_usage_pending", "usd": None})
    return dict(cached)


class RefreshGate:
    """Serialize and rate-limit on-demand native refresh scheduling."""

    def __init__(self, cooldown_seconds: float = REFRESH_COOLDOWN_SECONDS) -> None:
        self._cooldown_seconds = cooldown_seconds
        self._lock = Lock()
        self._in_flight = False
        self._last_started: Optional[float] = None

    def begin(self, *, now: Optional[float] = None) -> bool:
        tick = time.monotonic() if now is None else now
        with self._lock:
            if self._in_flight:
                return False
            if self._last_started is not None and tick - self._last_started < self._cooldown_seconds:
                return False
            self._in_flight = True
            self._last_started = tick
            return True

    def finish(self) -> None:
        with self._lock:
            self._in_flight = False


def refresh_codex(fleet_config_dir: Path, state_dir: Path) -> str:
    """Invoke fleet-config's bounded canonical Codex collector exactly once."""
    script = fleet_config_dir / "skills" / "_lib" / "quota_sources.py"
    python = fleet_config_dir / ".venv" / "Scripts" / "python.exe"
    if not script.is_file() or not python.is_file():
        return "collector_unavailable"
    try:
        result = subprocess.run(
            [str(python), str(script), "codex", "--state-dir", str(state_dir)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=35,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        logger.info("Codex quota refresh: native_process_failed")
        return "native_process_failed"
    if result.returncode != 0:
        logger.info("Codex quota refresh: collector_error")
        return "collector_error"
    return "complete"
