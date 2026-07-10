"""``python`` job-kind — today's ``.py`` dispatch, lifted out of the old
suffix switch in ``app/cli/commands/run_job_cmd.py::build_invocation``
unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Tuple

from src.jobs_config import Job
from src.jobs_kinds.base import Problem, require_script
from src.jobs_schtasks import resolve_venv_python


class PythonKind:
    name = "python"

    def validate(self, job: Job) -> List[Problem]:
        script = Path(job.script_path)
        if not script.is_file():
            return []  # existence is checked once, generically, by preflight()
        if resolve_venv_python(script) is None:
            return [
                Problem(
                    level="warning",
                    field="script_path",
                    message=(
                        "No .venv found walking up from the script's folder — "
                        "the executor will fall back to the launcher's own "
                        "interpreter (sys.executable)."
                    ),
                )
            ]
        return []

    def build_argv(
        self, job: Job, tail: List[str], param_env: Dict[str, str], run_dir: Path
    ) -> Tuple[List[str], Path, Dict[str, str]]:
        script = require_script(job, "Python script")
        venv_py = resolve_venv_python(script)
        if venv_py is not None:
            python_exe = str(venv_py)
            # <root>/.venv/Scripts/python.exe → <root>
            cwd = venv_py.parent.parent.parent
        else:
            python_exe = sys.executable
            cwd = script.parent
        argv = [python_exe, str(script)] + tail
        extra_env: Dict[str, str] = {"PYTHONPATH": str(cwd)}
        # User-declared env-mapped params override PYTHONPATH only if the
        # user explicitly named the collision — that is their call.
        extra_env.update(param_env)
        return argv, cwd, extra_env
