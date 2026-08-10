"""Is *this* checkout the canonical installed launcher? (issue #736)

Anything that reaches out of the process and touches the user — a phone
alert, a machine-global desktop sweep — must be owned by exactly one
instance. Every other copy of this repo on the box is a scratch instance
whose derived state is empty by construction, and empty scratch state
looks identical to "the thing you were watching broke".

That is not hypothetical. On 2026-08-10 a webapp booted out of the git
worktree ``app-launcher-wt-727`` pushed three *false* "scheduled run never
fired" alerts to the phone: the worktree carried the real
``config/jobs.json`` and the real Telegram credentials, but
:data:`src.jobs_history.JOBS_RUNS_DIR` resolves under its own
``PROJECT_ROOT``, so it read an empty run history and concluded every
scheduled fire in its window had been missed. The only exemption in place
was the ``LAUNCHER_SESSION_HOST_PORT`` marker, which the e2e /
verify-before-ship autoboot sets and nothing else does — it covers the
gate's throwaway webapp and no other non-canonical instance.

The predicate, in order:

1. ``LAUNCHER_SESSION_HOST_PORT`` set → disposable autoboot instance, never
   canonical. Checked **first and unconditionally**, so no override can
   re-arm the e2e gate's webapp (issue #260 / #278's failure mode).
2. :data:`CANONICAL_OVERRIDE_ENV` set to a boolean → that answer, for the
   cases inference cannot reach: a second full clone (which has a perfectly
   normal ``.git`` directory and is indistinguishable from the real one), or
   an exotic deployment with no ``.git`` at all.
3. Otherwise, a filesystem fact: a **primary** checkout has ``.git`` as a
   *directory*; a **linked worktree** has ``.git`` as a *file* containing
   ``gitdir: …``. Deterministic, offline, no git binary, no Task Scheduler,
   no network — it cannot return the wrong answer for a worktree.
4. Neither → the role is *unestablished*, and an unestablished fact gets its
   own outcome rather than being folded into the acting one (the same rule
   :func:`src.jobs_coverage.coverage_for` follows for a failed ``schtasks``
   query). Stand down; the override in step 2 is the way to say otherwise.

Callers should log the returned reason: a stray instance that silently
declines to alert is only marginally better than one that alerts wrongly.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple

from src.webapp_config import SESSION_HOST_PORT_ENV

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Explicit operator override, tri-state: unset → infer (step 3 above),
#: truthy → force canonical, falsey → force non-canonical. Deliberately not
#: a ``webapp_config.json`` field: that file gets copied wholesale into a
#: worktree along with the credentials, so a config flag would travel with
#: the very copies it is meant to distinguish.
CANONICAL_OVERRIDE_ENV = "LAUNCHER_CANONICAL_INSTANCE"

REASON_CANONICAL = "canonical"
REASON_DISPOSABLE = "disposable-autoboot"
REASON_FORCED_ON = "forced-canonical"
REASON_FORCED_OFF = "forced-non-canonical"
REASON_LINKED_WORKTREE = "linked-worktree"
REASON_ROOT_UNVERIFIABLE = "root-unverifiable"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSEY = {"0", "false", "no", "off"}


def _override() -> Optional[bool]:
    """:data:`CANONICAL_OVERRIDE_ENV` as a bool, ``None`` when unset/garbage."""
    raw = os.environ.get(CANONICAL_OVERRIDE_ENV, "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSEY:
        return False
    return None


def canonical_instance(
    project_root: Optional[Path] = None,
) -> Tuple[bool, str]:
    """``(is_canonical, reason)`` for the checkout at ``project_root``.

    ``project_root`` defaults to this repo's root, read from the module
    attribute at *call* time (not bound as a default argument) so a test can
    monkeypatch :data:`PROJECT_ROOT` — the same late-binding
    :func:`src.jobs_coverage.coverage_alerts_path` uses for
    ``jobs_history.JOBS_RUNS_DIR``.

    ``reason`` is one of the ``REASON_*`` constants and is meant to be
    logged verbatim by the caller.
    """
    root = PROJECT_ROOT if project_root is None else project_root

    if os.environ.get(SESSION_HOST_PORT_ENV, "").strip():
        return False, REASON_DISPOSABLE

    forced = _override()
    if forced is not None:
        return forced, REASON_FORCED_ON if forced else REASON_FORCED_OFF

    dot_git = Path(root) / ".git"
    if dot_git.is_dir():
        return True, REASON_CANONICAL
    if dot_git.is_file():
        return False, REASON_LINKED_WORKTREE
    return False, REASON_ROOT_UNVERIFIABLE
