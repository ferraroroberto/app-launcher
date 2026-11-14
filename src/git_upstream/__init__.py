"""Where each project stands against its remote, and the safe fast-forward.

Three parts, kept in one stdlib-only module so the lite launcher can take it
whole:

* :func:`fetch` -- the one network call. It runs on the Projects refresh, on a
  slow cadence and inside Sync all, never inside the ~45 s git-status poll;
  every outcome lands in an in-memory ledger.
* :func:`upstream_state` -- ahead/behind of the local default branch against
  ``origin/<default>``, read from local refs only (no network), so the poll can
  carry it. A failed last fetch reads ``unknown``, never "up to date": the
  refs it compared may be old.
* :func:`sync_repo` -- fetch, then fast-forward only a repo that is clean, on
  its default branch and only behind; every other repo is skipped with its
  reason. It never stashes, resets, rebases or merges. The fast-forward keeps
  any live file the incoming range untracks (the behaviour of fleet-config's
  ``untrack_guard.py``): git deletes a working-tree file whose path the
  incoming commit drops from the index, even when that path is ignored
  afterwards, so a merge that untracks a machine-local config would delete it.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.git_utils import run_git, run_git_proc

logger = logging.getLogger(__name__)

# A fetch that takes longer than this is treated as failed: a dead remote or
# a hung credential helper must not hold a worker thread for minutes.
FETCH_TIMEOUT_S = 30.0
_LOCAL_TIMEOUT_S = 10.0

UP_TO_DATE = "up-to-date"
BEHIND = "behind"
AHEAD = "ahead"
DIVERGED = "diverged"
UNKNOWN = "unknown"      # the last fetch failed, or the refs cannot be compared
UNCHECKED = "unchecked"  # no fetch since this process started; refs show nothing
NO_REMOTE = "no-remote"


@dataclass(frozen=True)
class FetchRecord:
    ok: bool
    at: float
    error: str = ""


_ledger_lock = threading.Lock()
_fetches: Dict[str, FetchRecord] = {}
# Repos Sync all fast-forwarded in this process's lifetime: a process started
# from the old checkout still runs the old code, so its tile says so.
_pulled: Dict[str, Tuple[str, str, float]] = {}


def _key(project_dir: Path) -> str:
    return str(Path(project_dir).resolve()).lower()


def last_fetch(project_dir: Path) -> Optional[FetchRecord]:
    with _ledger_lock:
        return _fetches.get(_key(project_dir))


def was_pulled(project_dir: Path) -> bool:
    with _ledger_lock:
        return _key(project_dir) in _pulled


def reset_ledgers() -> None:
    """Forget every fetch and pull (tests; a fresh process starts empty)."""
    with _ledger_lock:
        _fetches.clear()
        _pulled.clear()


def _first_line(proc: Any) -> str:
    lines = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()
    return lines[0] if lines else "?"


def has_origin(project_dir: Path) -> bool:
    return run_git(project_dir, ["remote", "get-url", "origin"], timeout=_LOCAL_TIMEOUT_S) is not None


def fetch(project_dir: Path) -> FetchRecord:
    """``git fetch origin`` for one repo; the outcome is recorded and returned."""
    proc = run_git_proc(
        project_dir, ["fetch", "--quiet", "origin"], timeout=FETCH_TIMEOUT_S, no_prompt=True
    )
    if proc is None:
        record = FetchRecord(False, time.time(), "fetch timed out or git could not run")
    elif proc.returncode != 0:
        record = FetchRecord(False, time.time(), _first_line(proc))
    else:
        record = FetchRecord(True, time.time())
    if not record.ok:
        logger.info("⚠️ git fetch failed in %s: %s", project_dir, record.error)
    with _ledger_lock:
        _fetches[_key(project_dir)] = record
    return record


@dataclass(frozen=True)
class UpstreamState:
    state: str
    ahead: int = 0
    behind: int = 0
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"state": self.state, "ahead": self.ahead, "behind": self.behind, "reason": self.reason}


def ahead_behind(project_dir: Path, default_branch: str) -> Optional[Tuple[int, int]]:
    """``(ahead, behind)`` of local ``<default>`` against ``origin/<default>``, or ``None``."""
    out = run_git(
        project_dir,
        ["rev-list", "--left-right", "--count",
         f"refs/heads/{default_branch}...refs/remotes/origin/{default_branch}"],
        timeout=_LOCAL_TIMEOUT_S,
    )
    if out is None:
        return None
    parts = out.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1])


def upstream_state(project_dir: Path, default_branch: Optional[str]) -> UpstreamState:
    """Ahead/behind from local refs plus the last fetch's outcome. No network."""
    if not has_origin(project_dir):
        return UpstreamState(NO_REMOTE)
    if not default_branch:
        return UpstreamState(UNKNOWN, reason="default branch unknown")
    counts = ahead_behind(project_dir, default_branch)
    record = last_fetch(project_dir)
    if counts is None:
        return UpstreamState(UNKNOWN, reason="cannot compare with origin/" + default_branch)
    ahead, behind = counts
    if record is not None and not record.ok:
        # The counts are kept: the refs only lag, so a behind they show is real.
        return UpstreamState(UNKNOWN, ahead, behind, "fetch failed: " + record.error)
    if ahead and behind:
        return UpstreamState(DIVERGED, ahead, behind)
    if behind:
        return UpstreamState(BEHIND, ahead, behind)
    if ahead:
        return UpstreamState(AHEAD, ahead, behind)
    if record is None:
        return UpstreamState(UNCHECKED)
    return UpstreamState(UP_TO_DATE)


def launch_refusal(project_dir: Path, default_branch: Optional[str]) -> Optional[str]:
    """Why a session may not start here yet: the default branch is behind its remote.

    Read from local refs (no network), as the tile shows it. ``None`` when
    the repo is not behind or the refs cannot be compared.
    """
    if not default_branch:
        return None
    counts = ahead_behind(project_dir, default_branch)
    if counts is None or counts[1] == 0:
        return None
    behind = counts[1]
    return (
        f"{default_branch} is {behind} commit{'s' if behind != 1 else ''} behind "
        f"origin/{default_branch}: pull first"
    )


# --------------------------------------------------------------- fast-forward


@dataclass
class FastForward:
    ok: bool
    detail: str = ""
    before: str = ""
    after: str = ""
    restored: List[str] = field(default_factory=list)
    kept_aside: List[str] = field(default_factory=list)
    stash: Optional[Path] = None


def parse_removed_paths(name_status_z: str) -> List[str]:
    """Old paths of the ``D`` and ``R`` rows in ``git diff --name-status -z`` output."""
    tokens = name_status_z.split("\0")
    removed: List[str] = []
    i = 0
    while i < len(tokens):
        status = tokens[i]
        if not status:
            i += 1
            continue
        paths = 2 if status.startswith("R") else 1
        if status[0] in "DR" and i + 1 < len(tokens):
            removed.append(tokens[i + 1])
        i += 1 + paths
    return removed


def _rev(repo: Path, ref: str) -> Optional[str]:
    return run_git(repo, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], timeout=_LOCAL_TIMEOUT_S) or None


def _is_ignored(repo: Path, rel: str) -> Optional[bool]:
    proc = run_git_proc(repo, ["check-ignore", "-q", "--no-index", "--", rel], timeout=_LOCAL_TIMEOUT_S)
    if proc is None:
        return None
    return {0: True, 1: False}.get(proc.returncode)


def guarded_fast_forward(repo: Path, target: str) -> FastForward:
    """``merge --ff-only`` to ``target``, keeping every live file the range untracks.

    A path the merge deletes and does not ignore stays deleted (a tracked
    deletion the merge intends). A path whose ignore state git cannot report
    is kept aside in the stash directory, reported, never guessed.
    """
    repo = Path(repo)
    sha = _rev(repo, target)
    if sha is None:
        return FastForward(False, f"cannot resolve {target}")
    before = _rev(repo, "HEAD") or ""
    listed = run_git_proc(
        repo, ["diff", "--name-status", "-z", "-M", "--diff-filter=DR", f"HEAD..{sha}"],
        timeout=_LOCAL_TIMEOUT_S,
    )
    if listed is None or listed.returncode != 0:
        return FastForward(False, f"cannot list the paths HEAD..{target} removes")
    live = [p for p in parse_removed_paths(listed.stdout or "") if (repo / p).is_file()]

    stash: Optional[Path] = None
    if live:
        stash = Path(tempfile.mkdtemp(prefix="untrack-guard-"))
        try:
            for rel in live:
                dst = stash / rel
                dst.parent.mkdir(parents=True, exist_ok=True)  # runtime-write-exempt: under a fresh temp dir
                shutil.copy2(repo / rel, dst)  # runtime-write-exempt: under a fresh temp dir
        except OSError as exc:
            shutil.rmtree(stash, ignore_errors=True)
            return FastForward(False, f"could not preserve {rel}: {exc}")

    merge = run_git_proc(repo, ["merge", "--ff-only", "--quiet", sha], timeout=60)
    if merge is None or merge.returncode != 0:
        if stash:
            shutil.rmtree(stash, ignore_errors=True)
        why = "timed out" if merge is None else _first_line(merge)
        return FastForward(False, f"merge --ff-only failed: {why}", before=before)

    result = FastForward(True, before=before, after=_rev(repo, "HEAD") or "")
    for rel in live:
        dst = repo / rel
        if dst.exists():
            continue
        ignored = _is_ignored(repo, rel)
        if ignored is False:
            continue
        if ignored is None:
            result.kept_aside.append(rel)
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)  # runtime-write-exempt: restores a live file the merge untracked
            shutil.copy2(stash / rel, dst)  # runtime-write-exempt: restores a live file the merge untracked
            result.restored.append(rel)
        except OSError:
            result.kept_aside.append(rel)
    if stash:
        if result.kept_aside:
            result.stash = stash
        else:
            shutil.rmtree(stash, ignore_errors=True)
    return result


# ----------------------------------------------------------------- sync one repo

PULLED = "pulled"
SKIPPED = "skipped"
CURRENT = "up-to-date"
FAILED = "failed"


@dataclass
class SyncOutcome:
    outcome: str          # pulled | skipped | up-to-date | failed
    reason: str = ""      # why it was skipped or failed
    commits: int = 0
    restored: List[str] = field(default_factory=list)
    kept_aside: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome, "reason": self.reason, "commits": self.commits,
            "restored": list(self.restored), "kept_aside": list(self.kept_aside),
        }


def _working_state(repo: Path) -> Optional[Tuple[Optional[str], bool]]:
    """``(branch, dirty)`` from one ``status`` call; branch ``None`` when detached."""
    out = run_git(repo, ["status", "--porcelain=v2", "--branch"], timeout=_LOCAL_TIMEOUT_S)
    if out is None:
        return None
    branch: Optional[str] = None
    dirty = False
    for line in out.splitlines():
        if line.startswith("# branch.head "):
            head = line[len("# branch.head "):].strip()
            branch = None if head == "(detached)" else head
        elif line and not line.startswith("#"):
            dirty = True
    return branch, dirty


def sync_repo(repo: Path, default_branch: Optional[str], *, do_fetch: bool = True) -> SyncOutcome:
    """Fetch, then fast-forward only a clean, on-default, behind-only repo."""
    repo = Path(repo)
    if not has_origin(repo):
        return SyncOutcome(SKIPPED, "no remote")
    if do_fetch and not fetch(repo).ok:
        return SyncOutcome(SKIPPED, "fetch failed")
    if not default_branch:
        return SyncOutcome(SKIPPED, "default branch unknown")
    working = _working_state(repo)
    if working is None:
        return SyncOutcome(SKIPPED, "git status failed")
    branch, dirty = working
    if dirty:
        return SyncOutcome(SKIPPED, "dirty")
    if branch != default_branch:
        return SyncOutcome(SKIPPED, "not on " + default_branch)
    counts = ahead_behind(repo, default_branch)
    if counts is None:
        return SyncOutcome(SKIPPED, "cannot compare with origin/" + default_branch)
    ahead, behind = counts
    if ahead and behind:
        return SyncOutcome(SKIPPED, "diverged")
    if ahead:
        return SyncOutcome(SKIPPED, "ahead")
    if not behind:
        return SyncOutcome(CURRENT)
    ff = guarded_fast_forward(repo, f"refs/remotes/origin/{default_branch}")
    if not ff.ok:
        logger.warning("❌ sync: fast-forward refused in %s: %s", repo, ff.detail)
        return SyncOutcome(FAILED, ff.detail)
    with _ledger_lock:
        _pulled[_key(repo)] = (ff.before, ff.after, time.time())
    logger.info("✅ sync: %s fast-forwarded %s..%s (%d commits)", repo, ff.before[:7], ff.after[:7], behind)
    for rel in ff.kept_aside:
        logger.warning("⚠️ sync: %s kept aside %s in %s", repo, rel, ff.stash)
    return SyncOutcome(PULLED, commits=behind, restored=ff.restored, kept_aside=ff.kept_aside)
