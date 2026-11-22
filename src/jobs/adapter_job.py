"""Which Jobs run fleet-config's scheduled-run adapter.

The exit-code table in :mod:`src.jobs.outcome` is the adapter's own verdict
vocabulary (``claude_progress.py`` / ``scheduled_runner.py``, wrapping a
``claude -p`` run). It means something only for a Job whose child *is* that
adapter; a plain script that exits 2 on its own terms must not be told it was
"rejected by the adapter". A scheduled Claude Job reaches the adapter through
a ``run-*.bat`` wrapper, so the registry row alone does not say: the wrapper's
text does. A script that cannot be read is not the adapter: the run keeps the
conservative ``failed`` state and claims no reason.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Tuple

from src.jobs.config import Job, job_for

logger = logging.getLogger(__name__)

# The adapter's entry modules; either name in a Job's command or wrapper means
# its exit codes are the adapter's.
_ADAPTER_MARKERS = ("claude_progress.py", "scheduled_runner.py")

# A wrapper script is a few lines; a larger file is not one, and reading it on
# every Jobs-tab poll would cost more than the answer is worth.
_MAX_SCRIPT_BYTES = 256 * 1024

# (path, mtime_ns, size) -> whether that file names the adapter. Keyed on the
# stat so an edited wrapper is re-read and an untouched one is not.
_script_cache: Dict[Tuple[str, int, int], bool] = {}


def _file_names_adapter(path: Path) -> bool:
    try:
        stat = path.stat()
    except OSError:
        return False
    if stat.st_size > _MAX_SCRIPT_BYTES:
        return False
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _script_cache.get(key)
    if cached is not None:
        return cached
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.info(f"ℹ️  adapter check could not read {path.name}: {exc}")
        return False
    found = any(marker in text for marker in _ADAPTER_MARKERS)
    if len(_script_cache) > 256:
        _script_cache.clear()
    _script_cache[key] = found
    return found


def job_runs_adapter(job: Job) -> bool:
    """True when ``job``'s command or wrapper script names the adapter.

    Reads the fields with ``getattr`` because the alert path types its job as
    ``Any`` and hands in lighter stand-ins than a registry row.
    """
    script_path = getattr(job, "script_path", "") or ""
    kind_config = getattr(job, "kind_config", None) or {}
    texts = [script_path, getattr(job, "args", "") or ""]
    texts.extend(value for value in kind_config.values() if isinstance(value, str))
    if any(marker in text for text in texts for marker in _ADAPTER_MARKERS):
        return True
    if script_path:
        return _file_names_adapter(Path(script_path))
    return False


def adapter_run_for(job_id: str) -> bool:
    """:func:`job_runs_adapter` for the registry row ``job_id``.

    An unknown job or an unreadable registry is not the adapter, so its runs
    claim no adapter reason.
    """
    job = job_for(job_id)
    return job is not None and job_runs_adapter(job)
