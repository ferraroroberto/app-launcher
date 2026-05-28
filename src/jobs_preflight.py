"""Save-time pre-flight for Jobs-tab authoring safety (issue #69 PR #1).

Adding a job today is a leap of faith: the first scheduled fire is when
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

Deferred (issue #69, not this PR): the schtasks ``/TR`` round-trip check
and the schtasks id-collision query. Both require shelling out to
``schtasks.exe`` from the request path; the ``/TR`` string carries only
launcher-internal paths (never user input), so the value is low and the
cost — forcing schtasks mocking into every create test — is high.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from src.jobs import resolve_venv_python
from src.jobs_config import Job

# A ``.venv\Scripts\python(w).exe`` (or ``activate``) reference embedded in
# a ``.bat`` wrapper. Best-effort: we only flag a reference that clearly
# points at a venv and whose target doesn't resolve, so a hand-rolled
# launcher that forgot to create its venv gets caught at save time.
_BAT_VENV_RE = re.compile(
    r"([A-Za-z]:\\[^\"'\r\n]*?\.venv\\Scripts\\(?:python\.exe|pythonw\.exe|activate(?:\.bat)?))",
    re.IGNORECASE,
)


@dataclass
class Problem:
    """One pre-flight finding, structured so the UI can render it inline.

    ``field`` points the dialog at the offending input (``script_path`` /
    ``args``) so the message lands next to the thing that's wrong.
    """

    level: str  # "error" | "warning"
    field: str
    message: str

    def to_dict(self) -> Dict[str, str]:
        return {"level": self.level, "field": self.field, "message": self.message}


def preflight(job: Job) -> List[Problem]:
    """Return the list of pre-flight problems for ``job`` (empty == clean).

    ``job`` is assumed to already have passed ``job_from_dict`` validation
    (id/name present, suffix is ``.py``/``.bat``); pre-flight checks the
    things that validation can't, because they depend on the filesystem.
    """
    problems: List[Problem] = []
    script = Path(job.script_path)

    # 1. The script must exist. This is the headline check — a typo'd path
    #    is the single most common authoring mistake and silently fails at
    #    fire time today.
    exists = False
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

    suffix = script.suffix.lower()

    # 2. .py venv walk-up. Mirrors the executor's resolution exactly
    #    (resolve_venv_python). No ancestor .venv → the executor falls back
    #    to sys.executable, which is rarely what the author wants — warn.
    if suffix == ".py" and exists and resolve_venv_python(script) is None:
        problems.append(
            Problem(
                level="warning",
                field="script_path",
                message=(
                    "No .venv found walking up from the script's folder — "
                    "the executor will fall back to the launcher's own "
                    "interpreter (sys.executable)."
                ),
            )
        )

    # 3. .bat embedded .venv reference. Best-effort scan: if the wrapper
    #    names a .venv interpreter/activate that doesn't resolve, the run
    #    will fail inside the .bat with a confusing error — warn early.
    if suffix == ".bat" and exists:
        try:
            text = script.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for m in _BAT_VENV_RE.finditer(text):
            ref = m.group(1)
            try:
                ref_ok = Path(ref).is_file()
            except OSError:
                ref_ok = False
            if not ref_ok:
                problems.append(
                    Problem(
                        level="warning",
                        field="script_path",
                        message=(
                            f"The .bat references a venv path that doesn't "
                            f"resolve: {ref}"
                        ),
                    )
                )
                break  # one warning is enough; don't spam per reference

    # 4. args must lex cleanly. The executor splits args on whitespace
    #    (shlex, posix=False on Windows); an unbalanced quote would mangle
    #    a value silently. Surface the parse error instead.
    if job.args:
        try:
            shlex.split(job.args, posix=False)
        except ValueError as exc:
            problems.append(
                Problem(
                    level="error",
                    field="args",
                    message=f"args do not parse: {exc}",
                )
            )

    return problems


def has_errors(problems: List[Problem]) -> bool:
    """True when any problem is error-level (blocks the save)."""
    return any(p.level == "error" for p in problems)
