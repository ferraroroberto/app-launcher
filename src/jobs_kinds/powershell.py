"""``powershell`` job-kind (issue #70) — runs a ``.ps1`` script natively via
the absolute Windows PowerShell 5.1 path, per the global CLAUDE.md
"Windows PowerShell in spawned commands" convention (``pwsh`` on PATH is an
unreliable WindowsApps reparse stub).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

from src.jobs_config import Job
from src.jobs_kinds.base import Problem, require_script

POWERSHELL_EXE = (
    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
)


class PowershellKind:
    name = "powershell"

    def validate(self, job: Job) -> List[Problem]:
        return []  # existence is checked once, generically, by preflight()

    def build_argv(
        self, job: Job, tail: List[str], param_env: Dict[str, str], run_dir: Path
    ) -> Tuple[List[str], Path, Dict[str, str]]:
        script = require_script(job, "PowerShell script")
        argv = [
            POWERSHELL_EXE,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ] + tail
        return argv, script.parent, dict(param_env)
