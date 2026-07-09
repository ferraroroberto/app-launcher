"""``inline-shell`` job-kind (issue #70) — the script body lives directly in
``jobs.json`` (``kind_config.script_body`` + ``kind_config.ext``) instead of
as a standalone file on disk. At fire time the body is written to a temp
file inside the run's own directory (preserved alongside ``run.json`` /
``output.log`` for reproducibility) and dispatched through whichever
file-kind matches the declared extension — the three native invocation
shapes (batch / powershell / wsl-bash) are not re-implemented a second time.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Tuple

from src.jobs_config import Job
from src.jobs_kinds.base import Problem
# Dotted-path submodule imports (not ``from src.jobs_kinds import batch``) —
# this module is itself imported *by* ``src/jobs_kinds/__init__.py``, so
# resolving siblings via an attribute lookup on the partially-initialised
# package would be import-order-fragile. A direct submodule import always
# works regardless of __init__.py's progress.
from src.jobs_kinds.batch import BatchKind
from src.jobs_kinds.powershell import PowershellKind
from src.jobs_kinds.shell_wsl import ShellWslKind

# Inline-shell extension → the sibling file-kind that actually knows how to
# invoke it, and the kind instances themselves (leaf modules — no import of
# the registry, so no cycle).
_SIBLINGS = {
    ".ps1": PowershellKind(),
    ".bat": BatchKind(),
    ".sh": ShellWslKind(),
}


class InlineShellKind:
    name = "inline-shell"

    def validate(self, job: Job) -> List[Problem]:
        cfg = job.kind_config or {}
        body = cfg.get("script_body")
        ext = cfg.get("ext")
        problems: List[Problem] = []
        if not body or not str(body).strip():
            problems.append(
                Problem(
                    level="error",
                    field="kind_config",
                    message="inline-shell requires a non-empty script_body.",
                )
            )
        sibling = _SIBLINGS.get(ext)
        if sibling is None:
            problems.append(
                Problem(
                    level="error",
                    field="kind_config",
                    message=(
                        f"inline-shell ext must be one of "
                        f"{sorted(_SIBLINGS)}, got {ext!r}."
                    ),
                )
            )
            return problems
        # Delegate to the sibling kind for environment-shaped warnings (e.g.
        # shell-wsl's "wsl.exe not on PATH") — it never sees a real
        # script_path yet at save time, so any of its checks that depend on
        # the file existing are harmless no-ops here.
        problems.extend(sibling.validate(job))
        return problems

    def build_argv(
        self, job: Job, tail: List[str], param_env: Dict[str, str], run_dir: Path
    ) -> Tuple[List[str], Path, Dict[str, str]]:
        cfg = job.kind_config or {}
        body = cfg.get("script_body")
        ext = cfg.get("ext")
        if not body or not str(body).strip():
            raise ValueError("inline-shell job has no script_body")
        sibling = _SIBLINGS.get(ext)
        if sibling is None:
            raise ValueError(f"inline-shell job has unsupported ext: {ext!r}")

        temp_script = run_dir / f"_inline{ext}"
        temp_script.write_text(str(body), encoding="utf-8")

        # A synthetic Job pointed at the temp file so the sibling kind's
        # build_argv (which reads job.script_path) needs no changes at all.
        synthetic_job = replace(job, script_path=str(temp_script))
        return sibling.build_argv(synthetic_job, tail, param_env, run_dir)
