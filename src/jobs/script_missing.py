"""Whether a Job's script is still on disk.

Save-time pre-flight (:mod:`src.jobs.preflight`) checks ``script_path`` once,
when the job is saved. A later change that moves the script leaves the job
pointing at nothing until its next scheduled run fails, so this check runs
again over the saved jobs: on every Jobs list read and on the coverage tick.

The check is one ``stat`` per job and edits nothing. A path the filesystem
cannot answer for (a permission error, a dead network share) is ``None``,
"not established", never folded into "present" or "missing".
"""

from __future__ import annotations

import os
import stat as stat_mod
from typing import Any, Dict, Iterable, Optional

from src.jobs_kinds import FILE_KINDS, resolve_kind

#: ``stat`` errors that mean the path is simply not there.
_ABSENT = (FileNotFoundError, NotADirectoryError)


def script_missing(job: Any) -> Optional[bool]:
    """``True`` when the script is established missing, ``False`` when present.

    ``None`` when there is nothing to check (a paused job, a kind with no
    script on disk) or the filesystem could not answer.
    """
    if job.is_paused or resolve_kind(job) not in FILE_KINDS:
        return None
    try:
        info = os.stat(job.script_path)
    except _ABSENT:
        return True
    except (OSError, ValueError):
        return None
    return not stat_mod.S_ISREG(info.st_mode)


def missing_scripts(jobs: Iterable[Any]) -> Dict[str, Any]:
    """The jobs whose script is established missing, keyed by job id."""
    return {job.id: job for job in jobs if script_missing(job) is True}
