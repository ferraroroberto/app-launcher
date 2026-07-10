"""Shared types for the job-kind registry (issue #70).

Deliberately a leaf module — no import of :mod:`src.jobs_preflight` or
:mod:`src.jobs_kinds` (the registry) — so that ``jobs_preflight`` can
import from here and *also* import the registry (``src.jobs_kinds``)
without a cycle: ``jobs_kinds.<kind module>`` → ``jobs_kinds.base`` →
``jobs_config`` only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Protocol, Tuple

from src.jobs_config import Job

# Kinds backed by a real on-disk ``script_path`` the executor spawns
# directly. Everything else (``inline-shell``, ``http-check``, and any
# future synthetic kind) carries its configuration in ``kind_config``
# instead and leaves ``script_path`` empty.
FILE_KINDS = frozenset({"python", "batch", "powershell", "shell-wsl"})


@dataclass
class Problem:
    """One pre-flight finding, structured so the UI can render it inline.

    ``field`` points the dialog at the offending input (``script_path`` /
    ``kind_config``) so the message lands next to the thing that's wrong.
    """

    level: str  # "error" | "warning"
    field: str
    message: str

    def to_dict(self) -> Dict[str, str]:
        return {"level": self.level, "field": self.field, "message": self.message}


def require_script(job: Job, label: str) -> Path:
    """Resolve ``job.script_path`` and existence-check it, or raise ``OSError``.

    Every file-backed kind's ``build_argv`` needs this same fire-time guard
    right before spawning (existence is checked once, generically, by
    ``preflight()`` at save time — this is the re-check immediately before
    the subprocess launch, in case the file moved/vanished since).
    """
    script = Path(job.script_path)
    if not script.is_file():
        raise OSError(f"{label} not found: {script}")
    return script


class JobKind(Protocol):
    """One entry in the job-kind registry.

    ``validate`` is a pure function (no subprocess, no disk writes) run
    at save time by ``src.jobs_preflight``. ``build_argv`` is called by
    the executor (``app.cli.commands.run_job_cmd``) at fire time; it may
    write files (``inline-shell`` writes its body into ``run_dir``) but
    must never spawn a subprocess itself — that stays the executor's job
    so every kind gets the same output-capture / resource-sampling /
    exit-code handling for free.
    """

    name: str

    def validate(self, job: Job) -> List[Problem]:
        ...

    def build_argv(
        self, job: Job, tail: List[str], param_env: Dict[str, str], run_dir: Path
    ) -> Tuple[List[str], Path, Dict[str, str]]:
        """Return ``(argv, cwd, env_overlay)`` for spawning ``job``.

        ``tail`` and ``param_env`` are already composed by the caller
        (``src.jobs_argv.compose_argv`` plus the legacy whitespace-split
        ``job.args`` tail) — every kind just appends ``tail`` to its own
        interpreter/wrapper argv and merges its own env additions (e.g.
        ``python``'s ``PYTHONPATH``) with ``param_env``; none of them
        re-derive parameter composition themselves.

        ``run_dir`` is the run's own directory
        (``webapp/jobs/<job_id>/<run_id>/``), already created by the
        executor before this is called — ``inline-shell`` writes its temp
        script there so it's preserved alongside ``run.json`` /
        ``output.log``.
        """
        ...
