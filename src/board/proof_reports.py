"""Read-only view of the per-repo visual-proof stores.

``ui_proof`` (fleet-config) writes one private, self-contained HTML per merged
UI PR to ``<repo>/.proof/<PR>/index.html`` in the repo's primary checkout and
records it in ``<repo>/.proof/index.json`` under ``"<PR>"``. The Board's Done
lane links those reports; this module only reads them and never writes, moves
or deletes one.

A report holds real screenshots, so it never goes to or from GitHub. The lookup
is jailed to the repo's ``.proof`` folder: the file path is rebuilt from a
validated repo name and an integer PR number, never taken from an index row.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PROOF_DIR = ".proof"
INDEX_NAME = "index.json"
PAGE_NAME = "index.html"
# A repo name as GitHub allows it; also keeps a separator or ".." out of a file name.
_REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
# A report is a few hundred KB of inline PNGs; far past this is not one of ours.
MAX_REPORT_BYTES = 40 * 1024 * 1024

# What a PR body's ``VISUAL_PROOF=<state>`` line says when the proof is not there.
# ``fresh`` and ``n/a`` mean nothing is owed, so they are not listed.
OWED_STATES = frozenset({"missing", "stale", "failing", "unknown"})

PROOF_AVAILABLE = "available"
PROOF_MISSING = "missing"

_index_lock = threading.Lock()
# One entry per repo's index, keyed by path: the Board reads many repos per poll.
_index_cache: Dict[str, Tuple[Tuple[int, int], Dict[str, Any]]] = {}


def store_dir(repo: str, projects_dir: str) -> Optional[Path]:
    """``<projects_dir>/<repo>/.proof``, or ``None`` for an unset dir or a bad repo name."""
    if not projects_dir or not isinstance(repo, str) or not _REPO_NAME.match(repo):
        return None
    return Path(projects_dir) / repo / PROOF_DIR


def _read_index(folder: Path) -> Dict[str, Any]:
    """``index.json`` as a dict, re-read only when its size or mtime moved.

    The Board polls every 5 s, so an unchanged file costs one ``stat``. A
    missing or unreadable index is an empty one: the repo may have no proof yet.
    """
    path = folder / INDEX_NAME
    try:
        stat = path.stat()
    except OSError:
        return {}
    sig = (stat.st_size, stat.st_mtime_ns)
    key = str(path)
    with _index_lock:
        cached = _index_cache.get(key)
        if cached is not None and cached[0] == sig:
            return cached[1]
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("⚠️ proof index unreadable: %s", exc)
        return {}
    if not isinstance(rows, dict):
        return {}
    with _index_lock:
        _index_cache[key] = (sig, rows)
    return rows


def report_path(repo: str, pr: int, projects_dir: str) -> Optional[Path]:
    """The stored report for ``repo#pr``, or ``None`` when there is none.

    ``None`` covers every way the answer is no: a repo name that is not a plain
    repo name, no index row, a missing or oversized file, or a path that does
    not resolve to ``<PR>/index.html`` inside the repo's ``.proof`` folder.
    """
    if isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0:
        return None
    root = store_dir(repo, projects_dir)
    if root is None or str(pr) not in _read_index(root):
        return None
    try:
        resolved = (root / str(pr) / PAGE_NAME).resolve(strict=True)
        jail = root.resolve(strict=True)
    except OSError:
        return None
    if resolved.parent.parent != jail or resolved.parent.name != str(pr) or not resolved.is_file():
        return None
    try:
        if resolved.stat().st_size > MAX_REPORT_BYTES:
            return None
    except OSError:
        return None
    return resolved


def annotate(items: List[Dict[str, Any]], projects_dir: str) -> List[Dict[str, Any]]:
    """Done cards with a ``proof`` field: ``available``, ``missing`` or absent.

    ``available`` is a stored report for the closing PR (the index is the
    authority). ``missing`` is the PR body declaring ``VISUAL_PROOF=<owed state>``
    with no report stored: the finisher says a UI diff shipped without proof.
    A card with neither stays without the field, since an index miss alone
    cannot tell a UI change from a docs change. ``proof_pr`` names the PR the
    report belongs to. The internal ``visual_proof`` marker is dropped.
    """
    out: List[Dict[str, Any]] = []
    for item in items:
        card = {k: v for k, v in item.items() if k != "visual_proof"}
        pr = item.get("pr")
        if isinstance(pr, int) and report_path(str(item.get("repo") or ""), pr, projects_dir) is not None:
            card["proof"] = PROOF_AVAILABLE
            card["proof_pr"] = pr
        elif isinstance(pr, int) and item.get("visual_proof") in OWED_STATES:
            card["proof"] = PROOF_MISSING
            card["proof_pr"] = pr
        out.append(card)
    return out
