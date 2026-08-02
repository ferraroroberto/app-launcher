"""State-file IO for the fleet context filter's control surface (issue #713).

The context filter itself (fleet-config#392/#541/#544) is a PreToolUse hook
that shrinks tool-call output before it reaches a coding agent's context
window. It is controlled machine-wide by two files under
``~/.fleet-context-filter`` (paths are configurable — see
``WebappConfig.context_filter_mode_file`` / ``context_filter_log_file``):

* **``mode.json``** — ``{"mode": "off"|"shadow"|"rewrite", "updated_at",
  "updated_by"}``. A plain file write, effective immediately for every
  session on the machine. No file at all means the filter resolves "off".
* **``shadow.jsonl``** — one JSON object per hook invocation, written in both
  shadow and rewrite modes: ``ts``, ``mode``, ``agent``, ``session_id``,
  ``cwd``, ``command``, ``tool``, ``raw_tokens``, ``compressed_tokens``,
  ``reduction_pct``, ``duration_ms``, ``exit_code``. Older rows predate some
  of these fields — every reader here is tolerant of a missing ``ts`` or
  ``agent`` rather than dropping the row outright.

Mirrors :mod:`src.board_state`'s degradation contract: a missing, unreadable,
corrupt, or wrong-shaped file must never raise — callers get an
``available: False`` shape (or, for the mode file specifically, an absent
file resolves to the true "off" default — see :func:`read_mode`) instead of
an exception, so a Settings-tab render never breaks on a cold or torn state
file. Deliberately self-contained (one module, no imports from
``board_state`` or any other feature) — this ships once and is ported to
app-launcher-lite verbatim later.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from src._json_io import atomic_write_json

logger = logging.getLogger(__name__)

VALID_MODES = ("off", "shadow", "rewrite")
DEFAULT_MODE = "off"
DEFAULT_UPDATED_BY = "app-launcher"

# A shadow.jsonl with no row newer than this is treated as gone-cold — the
# hook has stopped writing (filter off for a long stretch, or unwired).
# Mirrors board_state.py's STATE_STALE_AFTER (24h) for the same "a day with
# no update is worth flagging" horizon.
STATS_STALE_AFTER = timedelta(hours=24)

# Process-local cache keyed by (path, (st_mtime_ns, st_size)) — recomputed
# only when the log file itself changes, never on a wall-clock TTL (unlike
# src/jobs_stats.py's 30s TTL precedent: here the mtime/size key already
# guarantees "unchanged file never re-parses", so no TTL is needed on top).
_stats_cache: Dict[str, Tuple[Tuple[int, int], Dict[str, Any]]] = {}
_stats_lock = Lock()

# The harness applicability matrix — the Settings-tab panel's source of
# truth for which coding agents the filter actually reaches, and why.
# claude/codex are wired via fleet-config#544; the rest are tracked
# fleet-config follow-ups filed after this issue was scoped.
HARNESS_SUPPORT: List[Dict[str, str]] = [
    {
        "id": "claude",
        "label": "Claude Code",
        "status": "active",
        "note": "PreToolUse rewrite via ~/.claude wiring",
    },
    {
        "id": "codex",
        "label": "Codex CLI",
        "status": "active",
        "note": "same hook via codex-hooks.json; mode file reaches it",
    },
    {
        "id": "grok",
        "label": "Grok Build",
        "status": "unsupported",
        "note": "PreToolUse can only allow/deny — cannot rewrite output",
    },
    {
        "id": "pi",
        "label": "Pi",
        "status": "active",
        "note": "tool_result extension middleware (fleet-config#545) — compresses in place, no re-execution",
    },
    {
        "id": "copilot",
        "label": "GitHub Copilot CLI",
        "status": "planned",
        "note": "fleet-config#547 — upstream: interactive TUI sessions may fire no hooks",
    },
    {
        "id": "antigravity",
        "label": "Antigravity CLI",
        "status": "active",
        "note": "agy plugin, PreToolUse overwrite (fleet-config#546)",
    },
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_ts(raw: Any) -> Optional[datetime]:
    """Tolerant timestamp parse — ISO-8601 string or epoch seconds.

    Older shadow.jsonl rows may lack ``ts`` entirely (returns ``None``, and
    callers count the row in totals but not in a day-windowed slice).
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _command_family(raw: Any) -> Optional[str]:
    """First token of a shadow-logged command, ``git``/``gh`` plus subcommand.

    ``"git commit -m foo"`` -> ``"git commit"``; ``"ls -la"`` -> ``"ls"``.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    parts = raw.strip().split()
    if not parts:
        return None
    head = parts[0]
    if head in ("git", "gh") and len(parts) > 1:
        return f"{head} {parts[1]}"
    return head


def read_mode(path: Path) -> Dict[str, Any]:
    """Read the machine-wide mode switch.

    An **absent** file is not an error — it means the filter resolves to the
    true "off" default, so this reports ``available: True, mode: "off"``. An
    **unreadable / corrupt / wrong-shaped** file means the state genuinely
    can't be told, so it reports ``available: False`` — never conflated with
    the absent-file case, and never an exception either way.
    """
    unavailable = {
        "available": False,
        "mode": None,
        "updated_at": None,
        "updated_by": None,
    }
    if not path.exists():
        return {
            "available": True,
            "mode": DEFAULT_MODE,
            "updated_at": None,
            "updated_by": None,
        }
    try:
        # utf-8-sig: tolerate a BOM from a non-Python writer, same reasoning
        # as board_state.read_rate_limits.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return unavailable
    if not isinstance(data, dict):
        return unavailable
    mode = data.get("mode")
    if mode not in VALID_MODES:
        return unavailable
    updated_at = data.get("updated_at")
    updated_by = data.get("updated_by")
    return {
        "available": True,
        "mode": mode,
        "updated_at": updated_at if isinstance(updated_at, str) else None,
        "updated_by": updated_by if isinstance(updated_by, str) else None,
    }


def write_mode(path: Path, mode: str, *, updated_by: str = DEFAULT_UPDATED_BY) -> Dict[str, Any]:
    """Atomically write ``mode.json`` and return the fresh :func:`read_mode`.

    Raises ``ValueError`` for an invalid ``mode`` — callers (the API router)
    map that onto a 4xx, never a 500. The write is a temp-file-then-
    ``os.replace`` swap (:func:`src._json_io.atomic_write_json`); the
    sibling ``.tmp`` is removed in a ``finally`` so a write that fails
    mid-flight never leaves an orphaned temp file behind.
    """
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {VALID_MODES}; got {mode!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"mode": mode, "updated_at": _iso_z(_now()), "updated_by": updated_by}
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        atomic_write_json(path, payload)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return read_mode(path)


def _empty_bucket() -> Dict[str, int]:
    return {"rows": 0, "raw_tokens": 0, "compressed_tokens": 0, "tokens_saved": 0}


def _accumulate(bucket: Dict[str, int], raw_tokens: Any, compressed_tokens: Any) -> None:
    bucket["rows"] += 1
    if (
        isinstance(raw_tokens, (int, float))
        and not isinstance(raw_tokens, bool)
        and isinstance(compressed_tokens, (int, float))
        and not isinstance(compressed_tokens, bool)
    ):
        raw_i = int(raw_tokens)
        comp_i = int(compressed_tokens)
        bucket["raw_tokens"] += raw_i
        bucket["compressed_tokens"] += comp_i
        bucket["tokens_saved"] += max(0, raw_i - comp_i)


def _empty_stats() -> Dict[str, Any]:
    return {
        "available": False,
        "stale": False,
        "updated_at": None,
        "totals": _empty_bucket(),
        "last_7_days": _empty_bucket(),
        "today": _empty_bucket(),
        "per_agent": {},
        "top_commands": [],
    }


def _compute_stats(path: Path) -> Dict[str, Any]:
    try:
        raw_text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return _empty_stats()

    now = _now()
    seven_days_ago = now - timedelta(days=7)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    totals = _empty_bucket()
    last_7_days = _empty_bucket()
    today = _empty_bucket()
    per_agent: Dict[str, Dict[str, int]] = {}
    command_counter: "Counter[str]" = Counter()
    newest_ts: Optional[datetime] = None

    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue  # tolerate a torn/partial trailing line
        if not isinstance(row, dict):
            continue

        raw_tokens = row.get("raw_tokens")
        compressed_tokens = row.get("compressed_tokens")

        _accumulate(totals, raw_tokens, compressed_tokens)

        agent_raw = row.get("agent")
        agent = agent_raw.strip().lower() if isinstance(agent_raw, str) and agent_raw.strip() else "claude"
        _accumulate(per_agent.setdefault(agent, _empty_bucket()), raw_tokens, compressed_tokens)

        family = _command_family(row.get("command"))
        if family:
            command_counter[family] += 1

        ts = _parse_ts(row.get("ts"))
        if ts is not None:
            if newest_ts is None or ts > newest_ts:
                newest_ts = ts
            if ts >= seven_days_ago:
                _accumulate(last_7_days, raw_tokens, compressed_tokens)
            if ts >= today_start:
                _accumulate(today, raw_tokens, compressed_tokens)

    top_commands = [
        {"command": cmd, "count": count}
        for cmd, count in sorted(command_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    ]

    return {
        "available": True,
        "stale": bool(newest_ts is not None and now - newest_ts > STATS_STALE_AFTER),
        "updated_at": _iso_z(newest_ts) if newest_ts else None,
        "totals": totals,
        "last_7_days": last_7_days,
        "today": today,
        "per_agent": per_agent,
        "top_commands": top_commands,
    }


def read_stats(path: Path) -> Dict[str, Any]:
    """Aggregate ``shadow.jsonl`` — totals, last-7-days, today, per-agent,
    top command families.

    Cached module-locally keyed by ``(st_mtime_ns, st_size)``: unchanged
    file never re-parses, so this is safe to call on every
    ``GET /api/context-filter`` without re-reading a potentially large
    telemetry log each time. Absent/unreadable file -> ``available: False``;
    individual malformed lines are skipped rather than failing the whole
    read (fleet-config's writer is external and best-effort).
    """
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        return _empty_stats()
    stat_key = (st.st_mtime_ns, st.st_size)
    with _stats_lock:
        cached = _stats_cache.get(key)
        if cached is not None and cached[0] == stat_key:
            return cached[1]
    result = _compute_stats(path)
    with _stats_lock:
        _stats_cache[key] = (stat_key, result)
    return result
