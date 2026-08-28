"""``batch`` job-kind — today's ``.bat`` dispatch, lifted out of the old
suffix switch in ``app/cli/commands/run_job_cmd.py::build_invocation``
unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

from src.jobs_argv import reject_cmd_unsafe
from src.jobs_config import Job
from src.jobs_kinds.base import Problem, require_script

# A ``.venv\Scripts\python(w).exe`` (or ``activate``) reference embedded in
# a ``.bat`` wrapper. Best-effort: we only flag a reference that clearly
# points at a venv and whose target doesn't resolve, so a hand-rolled
# launcher that forgot to create its venv gets caught at save time.
_BAT_VENV_RE = re.compile(
    r"([A-Za-z]:\\[^\"'\r\n]*?\.venv\\Scripts\\(?:python\.exe|pythonw\.exe|activate(?:\.bat)?))",
    re.IGNORECASE,
)

# .bat execution always goes through cmd.exe, whose own quote-state
# re-parsing doesn't honour Python subprocess's argv-quoting convention —
# a tail value containing one of these can break out of its argv slot and
# have cmd.exe run additional commands (issue #409). A webhook-triggered
# job can map an unsanitized payload field straight into a string param
# value (src.jobs_webhook.resolve_mapping), so this has to be enforced
# here, not just at the UI layer.
#
# The character set and the check live in ``src.jobs_argv`` because this is
# not the only hop that hands argv to cmd — ``src.jobs_schtasks`` re-parents
# the executor through ``cmd /c start`` one layer up, and two copies of a
# contract like this drift apart silently.


class BatchKind:
    name = "batch"

    def validate(self, job: Job) -> List[Problem]:
        script = Path(job.script_path)
        if not script.is_file():
            return []  # existence is checked once, generically, by preflight()
        try:
            text = script.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        for m in _BAT_VENV_RE.finditer(text):
            ref = m.group(1)
            try:
                ref_ok = Path(ref).is_file()
            except OSError:
                ref_ok = False
            if not ref_ok:
                return [
                    Problem(
                        level="warning",
                        field="script_path",
                        message=(
                            f"The .bat references a venv path that doesn't "
                            f"resolve: {ref}"
                        ),
                    )
                ]  # one warning is enough; don't spam per reference
        return []

    def build_argv(
        self, job: Job, tail: List[str], param_env: Dict[str, str], run_dir: Path
    ) -> Tuple[List[str], Path, Dict[str, str]]:
        script = require_script(job, "BAT file")
        reject_cmd_unsafe(tail, "batch job argument")
        argv = ["cmd.exe", "/c", str(script)] + tail
        return argv, script.parent, dict(param_env)
