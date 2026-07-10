"""``shell-wsl`` job-kind (issue #70) — runs a ``.sh`` script via ``wsl
bash``. Opt-in: ``validate()`` warns (doesn't block) when ``wsl.exe`` isn't
resolvable on PATH, since WSL isn't installed on every machine this
launcher runs on.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Dict, List, Tuple

from src.jobs_config import Job
from src.jobs_kinds.base import Problem, require_script


class ShellWslKind:
    name = "shell-wsl"

    def validate(self, job: Job) -> List[Problem]:
        if shutil.which("wsl") is None:
            return [
                Problem(
                    level="warning",
                    field="script_path",
                    message=(
                        "wsl.exe not found on PATH — this job will fail to "
                        "spawn until WSL is installed on this machine."
                    ),
                )
            ]
        return []

    def build_argv(
        self, job: Job, tail: List[str], param_env: Dict[str, str], run_dir: Path
    ) -> Tuple[List[str], Path, Dict[str, str]]:
        script = require_script(job, "Shell script")
        argv = ["wsl", "bash", str(script)] + tail
        return argv, script.parent, dict(param_env)
