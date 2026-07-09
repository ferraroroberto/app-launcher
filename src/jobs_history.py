"""Run-history file storage for the Jobs tab.

Every run (manual or scheduled) creates ``webapp/jobs/<job_id>/<run_id>/``
with ``run.json`` (metadata) and ``output.log`` (combined stdout+stderr).
Mirrors the audit pattern in :mod:`src.audit`. Pruned to
:data:`MAX_RUNS_PER_JOB` per job.

Split out of :mod:`src.jobs` (issue #315) — the schtasks sync layer lives in
:mod:`src.jobs_schtasks`, the mutex queue in :mod:`src.jobs_queue`, and
percentiles/health in :mod:`src.jobs_stats`; all three read run history
through this module.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

try:  # Windows-only interprocess file lock (this repo runs Windows-only).
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows fallback
    msvcrt = None  # type: ignore[assignment]

from src._json_io import atomic_write_json

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

JOBS_RUNS_DIR = PROJECT_ROOT / "webapp" / "jobs"
MAX_RUNS_PER_JOB = 20


def runs_dir(job_id: str) -> Path:
    """Where this job's run history lives."""
    return JOBS_RUNS_DIR / job_id


def new_run_id() -> str:
    """A sortable, filesystem-safe run id."""
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def new_run_dir(job_id: str, run_id: Optional[str] = None) -> Path:
    """Create and return ``webapp/jobs/<job_id>/<run_id>/``.

    Collisions are resolved by appending ``-2``, ``-3``, … to ``run_id``
    so two manual triggers within the same second never overwrite each
    other.
    """
    base = runs_dir(job_id)
    base.mkdir(parents=True, exist_ok=True)
    rid = run_id or new_run_id()
    target = base / rid
    n = 2
    while target.exists():
        target = base / f"{rid}-{n}"
        n += 1
    target.mkdir()
    return target


# Terminal-outcome fields a Kill claims once it has fired. The webapp's
# Kill endpoint and the executor's natural finalise are separate processes
# writing the same ``run.json``; once a run is marked ``killed``, a later
# finalise must not silently downgrade these back to a "success" the user
# explicitly stopped. Resource stats (peak_rss_bytes, cpu_seconds) are not
# in this set — recording them post-kill is harmless.
_KILL_OWNED_FIELDS = ("status", "exit_code", "finished_at", "duration_seconds")


@contextmanager
def _run_json_lock(run_dir: Path) -> Iterator[None]:
    """Hold an exclusive interprocess lock for a ``run.json`` read-merge-swap.

    The two writers (the executor's finalise and the webapp's Kill endpoint)
    live in different processes, so a thread lock is not enough — this uses a
    Windows ``msvcrt.locking`` byte-range lock on a dedicated ``run.json.lock``
    sidecar (never on ``run.json`` itself, which ``os.replace`` swaps out from
    under any held handle). ``LK_LOCK`` is the fully-blocking flavour: it retries
    for ~10 s then raises, so a couple of attempts covers any real contention
    (the critical section is sub-millisecond). If the lock genuinely can't be
    taken, we log and proceed best-effort — losing serialization is far better
    than wedging a job's finalisation forever.
    """
    if msvcrt is None:  # pragma: no cover - non-Windows fallback
        yield
        return
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / "run.json.lock"
    handle = open(lock_path, "a+b")
    locked = False
    try:
        for attempt in range(3):
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                locked = True
                break
            except OSError:
                if attempt == 2:
                    logger.warning(
                        "⚠️  run.json lock contended for %s — writing unlocked",
                        run_dir,
                    )
        yield
    finally:
        if locked:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        handle.close()


def write_run_json(run_dir: Path, **fields: Any) -> None:
    """Interprocess-locked atomic merge into ``run_dir / run.json``.

    The read-merge-swap is serialized across processes with an exclusive file
    lock so the executor's finalise and the webapp's Kill endpoint can't race
    on the same record. Kill precedence: once a Kill has marked the run
    ``killed=True``, a later natural finalise is not allowed to overwrite the
    terminal outcome (:data:`_KILL_OWNED_FIELDS`) back to success.
    """
    target = run_dir / "run.json"
    incoming = {k: v for k, v in fields.items() if v is not None}
    with _run_json_lock(run_dir):
        existing: Dict[str, Any] = {}
        if target.exists():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
        # A write that is not itself (re)asserting the kill must not clobber a
        # kill already on record.
        if existing.get("killed") and not incoming.get("killed"):
            for key in _KILL_OWNED_FIELDS:
                incoming.pop(key, None)
        existing.update(incoming)
        atomic_write_json(target, existing)


# Header names safe to persist verbatim alongside a webhook run — never the
# secret or the raw signature value itself (only whether a signature header
# was present at all).
_WEBHOOK_SAFE_HEADERS = (
    "content-type",
    "x-github-event",
    "x-github-delivery",
)


def write_webhook_payload(
    run_dir: Path,
    *,
    provider: str,
    event: Optional[str],
    headers: Dict[str, str],
    payload: Any,
) -> None:
    """Persist the triggering webhook's payload to ``run_dir / _webhook.json``
    (issue #73) — so the run record is reproducible, mirroring ``run.json`` /
    ``output.log``. Written before the child spawns (the executor never
    touches this file), so it's present even for a run that lands in the
    mutex queue.
    """
    safe_headers = {
        name: headers[name] for name in _WEBHOOK_SAFE_HEADERS if name in headers
    }
    atomic_write_json(
        run_dir / "_webhook.json",
        {
            "provider": provider,
            "event": event,
            "headers": safe_headers,
            "payload": payload,
        },
    )


def read_webhook_payload(run_dir: Path) -> Optional[Dict[str, Any]]:
    """Read ``_webhook.json`` from ``run_dir``. Missing/malformed → ``None``."""
    target = run_dir / "_webhook.json"
    if not target.is_file():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def read_run(run_dir: Path) -> Dict[str, Any]:
    """Read ``run.json`` from ``run_dir``. Missing file → empty dict."""
    target = run_dir / "run.json"
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def list_runs(job_id: str) -> List[Dict[str, Any]]:
    """Newest-first list of decorated run records for ``job_id``."""
    base = runs_dir(job_id)
    if not base.is_dir():
        return []
    runs: List[Dict[str, Any]] = []
    for child in sorted(base.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        run = read_run(child)
        run.setdefault("run_id", child.name)
        runs.append(run)
    return runs


def latest_run(job_id: str) -> Optional[Dict[str, Any]]:
    runs = list_runs(job_id)
    return runs[0] if runs else None


def is_running(job_id: str) -> bool:
    """Cheap check — is the most recent run still in ``running`` state?"""
    latest = latest_run(job_id)
    return bool(latest and latest.get("status") == "running")


def prune_runs(job_id: str, keep: int = MAX_RUNS_PER_JOB) -> int:
    """Delete the oldest run dirs beyond ``keep``. Returns count removed."""
    base = runs_dir(job_id)
    if not base.is_dir():
        return 0
    children = [c for c in base.iterdir() if c.is_dir()]
    # Newest first by name — run ids are sortable timestamps.
    children.sort(key=lambda p: p.name, reverse=True)
    removed = 0
    for child in children[keep:]:
        try:
            for f in child.iterdir():
                try:
                    f.unlink()
                except OSError:
                    pass
            child.rmdir()
            removed += 1
        except OSError as exc:
            logger.debug(f"prune_runs: could not remove {child}: {exc}")
    return removed


def read_output_tail(run_dir: Path, max_bytes: int = 64 * 1024) -> str:
    """Read up to the last ``max_bytes`` of ``output.log``. Missing → ``""``."""
    target = run_dir / "output.log"
    if not target.is_file():
        return ""
    try:
        size = target.stat().st_size
        with target.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                # Drop a (possibly partial) first line after the seek.
                fh.readline()
            data = fh.read()
        return data.decode("utf-8", errors="replace")
    except OSError as exc:
        logger.debug(f"read_output_tail({target}) failed: {exc}")
        return ""
