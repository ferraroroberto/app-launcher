"""Which red CI runs the owner already dealt with.

A red workflow on a fleet repo's default branch raises a card in the Board's
PRs and jobs lane until a green run lands. Once the owner knows (a fix is on
its way, or the failure is a known flake) the card is noise, and it keeps the
folded lane and the header's "N failing" chip saying something is wrong.
Dismissing a card records the run's id here; :func:`src.board.ci_runs.board_cards`
skips a dismissed run.

Keyed by repo, workflow and ``run_id``: the alert is about that run, so the
workflow's next red run has a new id and raises a fresh card. The run itself
is never touched, and the chief's ci-failed feed is a separate store.

Store: ``webapp/ci-dismissed.json``, gitignored, atomically replaced: the
launcher's own runtime state, beside ``webapp/ci-failed-seen.json``.

Shape: ``{"<repo>": {"<workflow>": [<run_id>, ...]}}``. Only a workflow's
latest run ever has a card, so each keeps its newest :data:`MAX_PER_WORKFLOW`
ids and the file is bounded by the fleet's workflows.

A missing or corrupt file reads as nothing dismissed and never raises, so
damage can only bring a card back, not hide a red run the owner never
dismissed. A failed write raises :class:`OSError` to the route.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional

from src import runtime_files
from src._json_io import atomic_write_json

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Module-level so tests redirect it at a tmp_path (tests/conftest.py).
CI_DISMISSED_FILE = PROJECT_ROOT / "webapp" / "ci-dismissed.json"

# A workflow's older runs are superseded by its newest, so an older id never comes back.
MAX_PER_WORKFLOW = 20

# Two devices dismissing at once must union, not overwrite each other.
_lock = threading.Lock()

Dismissed = Dict[str, Dict[str, List[int]]]


def _run_ids(raw: object) -> List[int]:
    if not isinstance(raw, list):
        return []
    # ``bool`` is an ``int`` subclass: a stray ``true`` is not a run id.
    return [r for r in raw if isinstance(r, int) and not isinstance(r, bool)][-MAX_PER_WORKFLOW:]


def _clean(raw: object) -> Dismissed:
    if not isinstance(raw, dict):
        return {}
    cleaned: Dismissed = {}
    for repo, workflows in raw.items():
        if not isinstance(repo, str) or not isinstance(workflows, dict):
            continue
        kept = {
            name: ids for name, run_ids in workflows.items()
            if isinstance(name, str) and (ids := _run_ids(run_ids))
        }
        if kept:
            cleaned[repo] = kept
    return cleaned


def read_dismissed(path: Optional[Path] = None) -> Dismissed:
    """The dismissed run ids per repo and workflow; ``{}`` when nothing (readable) is on file."""
    target = Path(path or CI_DISMISSED_FILE)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("⚠️ dismissed CI runs unreadable: %s", exc.__class__.__name__)
        return {}
    return _clean(raw)


def is_dismissed(dismissed: Dismissed, repo: str, workflow: str, run_id: object) -> bool:
    return run_id in dismissed.get(repo, {}).get(workflow, ())


def dismiss(repo: str, workflow: str, run_id: int, path: Optional[Path] = None) -> None:
    """Record ``run_id`` of ``repo``'s ``workflow`` as dealt with. Idempotent.

    The caller checks that the run is a red card on the Board first.
    Raises :class:`OSError` when the file can't be written.
    """
    target = Path(path or CI_DISMISSED_FILE)
    with _lock:
        current = read_dismissed(target)
        ids = current.setdefault(repo, {}).get(workflow, [])
        if run_id in ids:
            return
        current[repo][workflow] = (ids + [run_id])[-MAX_PER_WORKFLOW:]
        runtime_files.make_dirs(target.parent)
        atomic_write_json(target, current)
    logger.info("ℹ️ CI alert dismissed: %s %s run %s", repo, workflow, run_id)
