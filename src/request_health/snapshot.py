"""A read-only health snapshot: is this box slow, and is it the launcher or something else?

``scripts/launcher.py health`` reads what a slow moment leaves behind and writes it to
one timestamped text file under ``webapp/health/``. It changes nothing: it
signals no process, starts no scan, and the one request it makes to the webapp
is a ``GET`` of ``/api/request-stats``, which reads counters and a cache and
never builds anything.

Every section degrades to ``unknown (<why>)`` rather than a blank or a zero: a
snapshot taken while the webapp is down must say so, not report an idle box.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from src import runtime_files
from src._json_io import read_json_or
from src.agents import AGENTS
from src.diagnostics import listener_pids

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - psutil is a hard requirement
    psutil = None  # type: ignore

logger = logging.getLogger(__name__)

LOG_DIR = Path(__file__).resolve().parent.parent.parent / "webapp"
HEALTH_DIR = LOG_DIR / "health"
SLOW_LOG = LOG_DIR / "slow-requests.log"

#: How long the per-process CPU reading samples for, and how many rows it keeps.
CPU_SAMPLE_S = 1.0
TOP_PROCESSES = 8
#: Lines of the slow-request log's end the report carries, read from this many bytes.
SLOW_TAIL_LINES = 20
SLOW_TAIL_BYTES = 64 * 1024
#: Executables that are a coding agent; only these are counted as agent processes.
AGENT_PROCESS_NAMES = frozenset(
    name for agent in AGENTS.values() for name in (agent.command, f"{agent.command}.exe")
)
#: How long the one webapp request may take before the section reads unknown.
STATS_TIMEOUT_S = 3.0

ProcessRow = Tuple[str, int, float, float]  # name, pid, cpu percent, rss MB


@dataclass(frozen=True)
class Sources:
    """Where the snapshot looks: the two ports and the two lane-state files."""

    webapp_port: int
    session_host_port: int
    managed_file: Path
    sessions_file: Path


def sources_from_config(cfg: Any) -> Sources:
    return Sources(
        webapp_port=int(cfg.port),
        session_host_port=int(cfg.session_host_port),
        managed_file=Path(cfg.chief_managed_file),
        sessions_file=Path(cfg.sessions_state_file),
    )


def literals_from_config(cfg: Any) -> Tuple[str, ...]:
    """The configured secrets, so the report can blank them if a log line carried one."""
    values = [cfg.auth_token, *(getattr(cfg, "secrets", None) or {}).values()]
    return tuple(v for v in values if isinstance(v, str) and len(v) >= 6)


def redact(text: str, literals: Tuple[str, ...]) -> str:
    for literal in literals:
        text = text.replace(literal, "[redacted]")
    return text


def sample_processes(sample_s: float = CPU_SAMPLE_S) -> Optional[List[ProcessRow]]:
    """Every process's CPU over ``sample_s`` seconds, or ``None`` when psutil is absent."""
    if psutil is None:
        return None
    procs = []
    for proc in psutil.process_iter(["name"]):
        try:
            proc.cpu_percent(None)
            procs.append(proc)
        except (psutil.Error, OSError):
            continue
    time.sleep(sample_s)
    rows: List[ProcessRow] = []
    for proc in procs:
        try:
            rss = proc.memory_info().rss / (1024 * 1024)
            rows.append((proc.info.get("name") or "?", proc.pid, proc.cpu_percent(None), rss))
        except (psutil.Error, OSError):
            continue
    return rows


def fetch_request_stats(port: int) -> Tuple[Optional[Dict[str, Any]], str]:
    """The webapp's ``/api/request-stats`` and ``""``, or ``None`` and why it could not be read."""
    reason = "not tried"
    for scheme in ("https", "http"):
        try:
            resp = httpx.get(
                f"{scheme}://127.0.0.1:{port}/api/request-stats",
                params={"window_s": 300},
                verify=False,
                timeout=STATS_TIMEOUT_S,
            )
            resp.raise_for_status()
            return resp.json(), ""
        except (httpx.HTTPError, ValueError) as exc:
            reason = f"{type(exc).__name__}"
    return None, f"webapp on :{port} unreachable ({reason})"


def _tail(path: Path, lines: int) -> List[str]:
    if not path.exists():
        return [f"{path.name}: absent"]
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - SLOW_TAIL_BYTES))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError as exc:
        return [f"{path.name}: unknown ({type(exc).__name__})"]
    return text.splitlines()[-lines:] or [f"{path.name}: empty"]


def _row_count(path: Path) -> str:
    if not path.exists():
        return "absent"
    data = read_json_or(path, None, path.name, expect=dict)
    return "unknown (unreadable)" if data is None else str(len(data))


def _listener_line(label: str, port: int) -> str:
    pids = listener_pids(port)
    if pids is None:
        return f"{label} :{port}: unknown (socket table unreadable)"
    return f"{label} :{port}: " + (", ".join(f"pid {p}" for p in pids) if pids else "not listening")


def _process_sections(rows: Optional[List[ProcessRow]]) -> List[str]:
    if rows is None:
        return ["top CPU: unknown (psutil missing)", "agent processes: unknown (psutil missing)"]
    # pid 0 is Windows' idle accounting (every spare core), not a process using CPU.
    top = sorted((r for r in rows if r[1] != 0), key=lambda r: r[2], reverse=True)[:TOP_PROCESSES]
    out = [f"top CPU over {CPU_SAMPLE_S:g}s (percent of one core):"]
    out += [f"  {name} pid {pid}: {cpu:.0f}%, {rss:.0f} MB" for name, pid, cpu, rss in top]
    agents = [r for r in rows if r[0].lower() in AGENT_PROCESS_NAMES]
    out.append(f"agent processes: {len(agents)}")
    out += [f"  {name} pid {pid}: {cpu:.0f}%, {rss:.0f} MB" for name, pid, cpu, rss in agents]
    return out


def _memory_line() -> str:
    if psutil is None:
        return "memory: unknown (psutil missing)"
    mem = psutil.virtual_memory()
    return (
        f"memory: {mem.available / 2**30:.1f} GB available of {mem.total / 2**30:.1f} GB "
        f"({mem.percent:.0f}% used)"
    )


def _request_lines(stats: Optional[Dict[str, Any]], why: str) -> List[str]:
    if stats is None:
        return [
            f"requests: unknown ({why})",
            "git-status cache age: unknown (webapp unreachable)",
        ]
    age = stats.get("git_status_age_s")
    out = [
        f"requests: {stats.get('inflight')} in flight now, peak {stats.get('max_inflight')} "
        f"in the last {stats.get('window_s'):g}s, webapp up {stats.get('uptime_s')}s",
        "git-status cache age: " + ("no scan yet" if age is None else f"{age}s"),
    ]
    for name, counts in sorted((stats.get("verdicts") or {}).items()):
        out.append(
            f"  {name}: {counts.get('ok', 0)} ok, {counts.get('over', 0)} over soft, "
            f"{counts.get('breach', 0)} breach"
        )
    return out


@dataclass(frozen=True)
class Snapshot:
    path: Path
    text: str


def build_text(
    sources: Sources,
    *,
    now: datetime,
    rows: Optional[List[ProcessRow]],
    stats: Optional[Dict[str, Any]],
    stats_why: str,
    slow_tail: List[str],
) -> str:
    """The report body; every input is passed in, so a test needs no live box."""
    lines = [f"Health snapshot {now:%Y-%m-%d %H:%M:%S}", "", "-- listeners"]
    lines += [
        _listener_line("webapp", sources.webapp_port),
        _listener_line("session-host", sources.session_host_port),
    ]
    lines += ["", "-- load", _memory_line(), *_process_sections(rows)]
    lines += ["", "-- lanes", f"lane marks (chief-managed): {_row_count(sources.managed_file)}",
              f"session rows: {_row_count(sources.sessions_file)}"]
    lines += ["", "-- requests"] + _request_lines(stats, stats_why)
    lines += ["", f"-- slow-request log, last {SLOW_TAIL_LINES} lines"] + [
        f"  {line}" for line in slow_tail
    ]
    return "\n".join(lines) + "\n"


def save_snapshot(
    sources: Sources,
    *,
    literals: Tuple[str, ...] = (),
    out_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
    sampler: Callable[[], Optional[List[ProcessRow]]] = sample_processes,
    stats_reader: Callable[[int], Tuple[Optional[Dict[str, Any]], str]] = fetch_request_stats,
) -> Snapshot:
    """Gather, redact and write one snapshot file; return where it went."""
    moment = now or datetime.now()
    stats, why = stats_reader(sources.webapp_port)
    text = build_text(
        sources,
        now=moment,
        rows=sampler(),
        stats=stats,
        stats_why=why,
        slow_tail=_tail(SLOW_LOG, SLOW_TAIL_LINES),
    )
    text = redact(text, literals)
    out_dir = out_dir or HEALTH_DIR
    runtime_files.make_dirs(out_dir)
    target = out_dir / f"health-{moment:%Y%m%d-%H%M%S}.txt"
    runtime_files.save_text(target, text, encoding="utf-8")
    logger.info(f"✅ health snapshot saved: {target.name}")
    return Snapshot(target, text)
