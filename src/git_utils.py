"""Shared ``git`` subprocess runner (issue #794).

Five call sites hand-rolled their own ``git -C <repo> <args>`` subprocess
wrapper — three raw ``subprocess.run`` blocks inline in
:mod:`src.build_info` plus one each in :mod:`src.scanner` and
:mod:`src.session_host_paths` — all sharing the same
``capture_output=True, stdin=DEVNULL, text=True, timeout=5,
check=False, creationflags=NO_WINDOW`` shape and the same "empty/failed →
``None`` / ``unknown``" tail. This module is the one place that shape lives
now; every caller treats ``None`` as "couldn't determine", never a
confident empty answer (same convention `fleet-config` adopted for its own
``git_run.run_git``).

Being that one place is also why :func:`git_env`'s ``GIT_OPTIONAL_LOCKS=0``
belongs here and nowhere else (#1111): a hand-rolled
``subprocess.run(["git", ...])`` opts back out of it invisibly, still
exiting 0 with the right answer, so ``tests/test_git_spawn_gate.py`` fails
the build on any ``git`` spawn under ``src/`` or ``app/`` outside this file.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional, Sequence, Union

from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 5.0


def git_env(base: Optional[dict] = None) -> dict:
    """``base`` (default ``os.environ``) plus ``GIT_OPTIONAL_LOCKS=0``.

    **Do not "clean up" this variable.** ``git status`` takes
    ``.git/index.lock`` purely to write back a refreshed **stat cache** — an
    optimisation, not part of producing the output. Kill such a process
    mid-write and it leaves exactly a 0-byte ``index.lock`` and nothing else
    touched. Measured, and narrower than the usual "status and diff and
    friends" phrasing: of ``status --porcelain=v2 --branch``,
    ``diff --name-only HEAD``, ``diff --name-only --cached HEAD``,
    ``ls-files --others``, ``rev-parse --verify`` and ``symbolic-ref``, **only
    ``status`` takes it** — which is exactly the one command
    :func:`src.scanner.git_status` runs on every repo, every poll.

    This repo is the fleet's only *fleet-wide* git reader, which is what makes
    it the one that can strand dozens at once. ``/api/claude-code/git-status``
    fans :func:`src.scanner.git_status` across every child of ``projects_dir``
    (default: this repo's parent, ``E:/automation``) in parallel threads, on a
    ~45 s poll. Measured on this host: 40 of the 42 sibling repos take the
    optional lock on every poll, each holding it 4–6 ms starting ~75 % into a
    ~30 ms run — so the fan-out's lock windows **overlap in real time**, and
    one kill of the process tree in that band strands most of the wave at
    once, every lock stamped within a single Windows file-time tick. That is
    the 2026-09-20 incident: 23 repos, one timestamp (#1111,
    ferraroroberto/fleet-config#939), and nine repos on 2026-08-01 before it
    (fleet-config#667).

    It matters far more than a leftover file suggests, because a stale lock is
    **invisible to a read**: ``git status --porcelain``, ``git fetch``,
    ``git rev-list`` and an up-to-date ``git pull --ff-only`` all exit 0 with
    correct output while the repo cannot ``add``, ``commit``, ``pull`` or
    ``stash``. The 2026-08-01 set went unnoticed for fifteen days; the
    2026-09-20 set made two ``git merge --ff-only`` calls fail while the next
    command in each chain read the old HEAD, so two tray restarts verified and
    reported the **old** ``git_sha`` as live.

    ``GIT_OPTIONAL_LOCKS=0`` suppresses only *optional* locks — a requested
    write still takes the real index lock and still refuses when one is
    already held, which is correct and must stay. Verified, not assumed:
    ``tests/test_git_utils.py`` drives both halves against a real repo.
    """
    env = dict(os.environ if base is None else base)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def run_git(
    repo: Union[str, Path],
    args: Sequence[str],
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    warn_on_failure: bool = False,
) -> Optional[str]:
    """Run ``git -C <repo> <args>`` and return stripped stdout, or ``None``.

    ``None`` on a non-zero exit, a missing ``git`` binary, a timeout, or any
    other spawn error — never a confident empty string. Failures log at
    DEBUG by default: several call sites use this to probe a ref that's
    routinely absent (e.g. checking whether a ``master`` branch exists once
    ``main`` already matched), where a WARNING would be noise for expected
    behaviour. Pass ``warn_on_failure=True`` where a failure is itself
    notable (e.g. resolving the checkout's own ``HEAD``) so it surfaces at
    WARNING with the exit code / stderr.
    """
    cmd = ["git", "-C", str(repo), *args]
    log = logger.warning if warn_on_failure else logger.debug
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=NO_WINDOW,
            env=git_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log("⚠️ run_git: %s raised %s: %s", " ".join(cmd), type(exc).__name__, exc)
        return None
    if result.returncode != 0:
        log(
            "⚠️ run_git: %s exit=%s stderr=%r",
            " ".join(cmd), result.returncode, (result.stderr or "").strip(),
        )
        return None
    return result.stdout.strip()


def resolve_default_ref(
    repo: Union[str, Path],
    *,
    fallback_refs: Sequence[str],
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> Optional[str]:
    """Resolve ``origin/HEAD``'s target (e.g. ``"origin/main"``), falling
    back to the first ref in ``fallback_refs`` that verifies via
    ``rev-parse --verify --quiet``. ``None`` when neither resolves (no
    ``origin`` remote, git missing, not a repo, or no fallback matches).

    Shared by :func:`src.scanner._default_branch` (local
    ``refs/heads/main`` / ``refs/heads/master``, which strips the result to
    a bare branch name) and :func:`src.build_info._resolve_default_remote_ref`
    (remote-tracking ``origin/main`` / ``origin/master``, which returns the
    resolved ref as-is) — same ``origin/HEAD``-first resolution, different
    fallback ref sets and return shape, so each caller passes its own
    ``fallback_refs`` and adapts the result itself.
    """
    head = run_git(
        repo,
        ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        timeout=timeout,
    )
    if head:
        return head
    for candidate in fallback_refs:
        if run_git(repo, ["rev-parse", "--verify", "--quiet", candidate], timeout=timeout) is not None:
            return candidate
    return None
