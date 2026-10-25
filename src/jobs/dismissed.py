"""Which failed job runs the owner already dealt with (#1669).

A failed or unconfirmed run raises a card in the Board's PRs and jobs lane for
the rest of the local day. Once the cause is fixed (or the work delivered by
hand) the card is noise, and it keeps the folded lane and the header's
"N failing" chip saying something is wrong. Dismissing a card records the
run's id here; :func:`src.board.jobs_attention` skips a dismissed run.

Keyed by ``run_id``, not by job: the next failed run of the same job has a
new id, so it raises a fresh card. The run record itself is never touched, so
the Jobs tab still shows the failure.

Store: ``webapp/jobs-dismissed.json``, gitignored, atomically replaced: the
launcher's own runtime state, beside ``webapp/chief-answered.json``. It sits
next to ``webapp/jobs/`` rather than inside it because that directory is one
folder per job id.

Shape: ``{"<job_id>": ["<run_id>", ...]}``. A run's card only ever exists for
the job's *latest* run, so a job keeps its newest :data:`MAX_PER_JOB` ids and
the file is bounded by the job registry.

Degradation contract: missing or corrupt reads as nothing dismissed, never an
exception, so a bad file can only bring a card back, never hide a failure
that was not dismissed. A failed write raises :class:`OSError` to the route.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional

from src._json_io import atomic_write_json

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Module-level so tests redirect it at a tmp_path (tests/conftest.py).
JOBS_DISMISSED_FILE = PROJECT_ROOT / "webapp" / "jobs-dismissed.json"

# Run history is pruned to 20 runs per job, so an older id can never come back.
MAX_PER_JOB = 20

# Two devices dismissing at once must union, not overwrite each other.
_lock = threading.Lock()


def _clean(raw: object) -> Dict[str, List[str]]:
    if not isinstance(raw, dict):
        return {}
    cleaned: Dict[str, List[str]] = {}
    for job_id, run_ids in raw.items():
        if not isinstance(job_id, str) or not isinstance(run_ids, list):
            continue
        ids = [r for r in run_ids if isinstance(r, str) and r]
        if ids:
            cleaned[job_id] = ids[-MAX_PER_JOB:]
    return cleaned


def read_dismissed(path: Optional[Path] = None) -> Dict[str, List[str]]:
    """The dismissed run ids per job id; ``{}`` when nothing (readable) is on file."""
    target = Path(path or JOBS_DISMISSED_FILE)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("⚠️ dismissed job runs unreadable: %s", exc.__class__.__name__)
        return {}
    return _clean(raw)


def is_dismissed(
    dismissed: Dict[str, List[str]], job_id: str, run_id: Optional[str]
) -> bool:
    return bool(run_id) and run_id in dismissed.get(job_id, ())


def dismiss(job_id: str, run_id: str, path: Optional[Path] = None) -> None:
    """Record ``run_id`` of ``job_id`` as dealt with. Idempotent.

    The caller checks that the run exists and is a dismissible failure first.
    Raises :class:`OSError` when the file can't be written.
    """
    target = Path(path or JOBS_DISMISSED_FILE)
    with _lock:
        current = read_dismissed(target)
        ids = current.get(job_id, [])
        if run_id in ids:
            return
        current[job_id] = (ids + [run_id])[-MAX_PER_JOB:]
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(target, current)
    logger.info("ℹ️ job alert dismissed: %s run %s", job_id, run_id)
