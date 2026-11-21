"""``powershell`` job-kind: runs a ``.ps1`` script through the AllSigned-safe launcher.

The launcher (:mod:`src.launching.ps_script`) starts the script with a plain ``-File`` where the
machine's execution policy lets an unsigned local file run, and loads it as a ScriptBlock under
any other policy (``AllSigned``, ``Restricted``) or one that could not be read, so a job never
overrides the machine's policy. The absolute Windows PowerShell 5.1 path is the launcher's;
``pwsh`` on PATH is an unreliable WindowsApps reparse stub.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

from src.jobs.config import Job
from src.jobs_kinds.base import Problem, require_script
from src.launching import ps_script


class PowershellKind:
    name = "powershell"

    def validate(self, job: Job) -> List[Problem]:
        return []  # existence is checked once, generically, by preflight()

    def build_argv(
        self, job: Job, tail: List[str], param_env: Dict[str, str], run_dir: Path
    ) -> Tuple[List[str], Path, Dict[str, str]]:
        script = require_script(job, "PowerShell script")
        # The ScriptBlock launch carries the script path and arguments in the child's
        # environment, so they ride in the kind's env overlay next to the typed params.
        argv, launch_env = ps_script.script_command(script, tail, policy=ps_script.effective_policy())
        return argv, script.parent, {**param_env, **launch_env}
