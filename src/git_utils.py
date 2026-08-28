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
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional, Sequence, Union

from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 5.0


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
