"""Job-kind registry (issue #70).

Replaces the old ``if suffix == ".py": … elif ".bat": …`` ladder in
``app/cli/commands/run_job_cmd.py`` with one module per *kind* — each
contributing a :func:`~src.jobs_kinds.base.JobKind.validate` (save-time
pre-flight problems) and :func:`~src.jobs_kinds.base.JobKind.build_argv`
(the actual invocation). Adding a new kind is registering one more module
here; nothing else in the executor or pre-flight layer changes.

Shipped kinds:

* ``python`` / ``batch`` — today's ``.py`` / ``.bat`` behaviour, lifted
  out of the old switch unchanged.
* ``powershell`` — a ``.ps1`` script via the absolute pwsh-5.1 path.
* ``shell-wsl`` — a ``.sh`` script via ``wsl bash``.
* ``inline-shell`` — a body stored directly in ``jobs.json``
  (``kind_config.script_body`` + ``kind_config.ext``), written to a temp
  file under the run dir and dispatched through the matching file-kind.
* ``http-check`` — a synthetic kind with no script on disk: polls a URL
  via a tiny built-in probe script.
"""

from __future__ import annotations

from typing import Dict

from src.jobs_config import Job
from src.jobs_kinds import batch, http_check, inline_shell, powershell, python, shell_wsl
from src.jobs_kinds.base import FILE_KINDS, JobKind, Problem

KINDS: Dict[str, JobKind] = {
    "python": python.PythonKind(),
    "batch": batch.BatchKind(),
    "powershell": powershell.PowershellKind(),
    "shell-wsl": shell_wsl.ShellWslKind(),
    "inline-shell": inline_shell.InlineShellKind(),
    "http-check": http_check.HttpCheckKind(),
}

# Legacy suffix → kind fallback, for jobs.json rows saved before this
# registry existed (no explicit ``kind`` field). Keeps every pre-existing
# row dispatching exactly as it did before, with no migration required.
_SUFFIX_FALLBACK = {".py": "python", ".bat": "batch"}


def resolve_kind(job: Job) -> str:
    """The effective kind name for ``job``.

    An explicit ``job.kind`` always wins. Otherwise falls back to
    inferring from ``script_path``'s suffix (the only shape a job could
    have before this registry existed). ``"unknown"`` when neither
    resolves to a registered kind.
    """
    if job.kind:
        return job.kind if job.kind in KINDS else "unknown"
    suffix = ""
    if job.script_path:
        # Local import avoids a module-level Path() call for the common
        # case where job.kind is already set.
        from pathlib import Path

        suffix = Path(job.script_path).suffix.lower()
    return _SUFFIX_FALLBACK.get(suffix, "unknown")


__all__ = ["KINDS", "FILE_KINDS", "JobKind", "Problem", "resolve_kind"]
