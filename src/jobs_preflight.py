"""Save-time pre-flight for Jobs-tab authoring safety (issue #69 PR #1).

Adding a job used to be a leap of faith: the first scheduled fire is when
you discover the path was wrong, the venv didn't walk up, or the args
didn't quote cleanly. This module front-loads those checks so the job
dialog can surface problems *before* the schedule starts ticking.

``preflight(job)`` is a pure function — no subprocess, no globals, no
disk writes beyond ``stat``/``read``. The router runs it on POST/PUT;
errors block the save (400), warnings save once acknowledged. Keeping it
pure means it is trivially unit-testable and never shells out to
``schtasks.exe`` inside a request handler.

Two severities:

* ``error``   — the job cannot run as configured; the save is blocked.
* ``warning`` — the job will run, but probably not the way the author
  expects (e.g. a ``.py`` target with no ``.venv`` will fall back to the
  launcher's own interpreter). Surfaced in the dialog; saved on confirm.

The kind-specific checks (``.py`` venv walk-up, ``.bat`` embedded-venv
scan, ``shell-wsl``'s missing ``wsl.exe``, ``inline-shell``/``http-check``
config shape, …) live one per module under :mod:`src.jobs_kinds`
(issue #70) — this module only owns the one check every file-kind shares
(does ``script_path`` exist) and delegates everything else to the
resolved kind's ``validate()``.

Deferred (issue #69, not this PR): the schtasks ``/TR`` round-trip check
and the schtasks id-collision query. Both require shelling out to
``schtasks.exe`` from the request path; the ``/TR`` string carries only
launcher-internal paths (never user input), so the value is low and the
cost — forcing schtasks mocking into every create test — is high.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from src.jobs_config import Job
from src.jobs_kinds import FILE_KINDS, KINDS, resolve_kind
from src.jobs_kinds.base import Problem  # re-exported for back-compat


def preflight(job: Job) -> List[Problem]:
    """Return the list of pre-flight problems for ``job`` (empty == clean).

    ``job`` is assumed to already have passed :func:`~src.jobs_config.job_from_dict`
    / :func:`~src.jobs_config.validate_kind_shape` (structurally valid kind +
    script_path/kind_config combination); pre-flight checks the things that
    validation can't, because they depend on the filesystem or environment.
    """
    problems: List[Problem] = []
    kind = resolve_kind(job)

    if kind == "unknown":
        problems.append(
            Problem(
                level="error",
                field="kind",
                message=(
                    f"Could not resolve a job kind for script_path "
                    f"{job.script_path!r} — set an explicit kind."
                ),
            )
        )
        return problems

    # The one check every file-kind shares: the script must exist. This is
    # the headline check — a typo'd path is the single most common
    # authoring mistake and silently fails at fire time today. Non-file
    # kinds (inline-shell, http-check) have no script_path to check.
    if kind in FILE_KINDS:
        script = Path(job.script_path)
        try:
            exists = script.is_file()
        except OSError:
            exists = False
        if not exists:
            problems.append(
                Problem(
                    level="error",
                    field="script_path",
                    message=f"Script not found: {job.script_path}",
                )
            )

    problems.extend(KINDS[kind].validate(job))
    return problems


def has_errors(problems: List[Problem]) -> bool:
    """True when any problem is error-level (blocks the save)."""
    return any(p.level == "error" for p in problems)
