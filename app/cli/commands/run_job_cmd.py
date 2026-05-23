"""``run-job`` subcommand — the sole executor for the Jobs tab.

Every Jobs-tab trigger funnels through this one entry point:

* Windows Task Scheduler fires ``pythonw launcher.py run-job <id>``
  (scheduled runs).
* The webapp's ``POST /api/jobs/<id>/run`` spawns the same command
  detached (manual / Stream Deck / phone tap).

Both paths produce identical run records under ``webapp/jobs/<id>/<rid>/``
— ``run.json`` (metadata) plus ``output.log`` (combined stdout+stderr).

Target dispatch:

* ``.py`` — walks up from ``script_path.parent`` looking for a sibling
  ``.venv\\Scripts\\python.exe``; falls back to ``sys.executable``. The
  subprocess runs with ``cwd = <project root>`` and ``PYTHONPATH =
  <project root>`` so the script can ``from project.module import …``
  exactly as it would when invoked from a CMD window at the root (see
  the global CLAUDE.md "PYTHONPATH for out-of-tree Python scripts"
  gotcha — Task Scheduler does not inherit user env by default).
* ``.bat`` — invoked via ``cmd.exe /c <script_path> <args>`` with
  ``cwd = script_path.parent``.

``args`` is split on whitespace before being appended to argv. Jobs that
need arguments containing spaces should put the argument inside the
``.bat`` / ``.py`` wrapper itself rather than relying on shell quoting.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.jobs import (
    MAX_RUNS_PER_JOB,
    new_run_dir,
    new_run_id,
    prune_runs,
    runs_dir,
    write_run_json,
)
from src.jobs_config import Job, get_by_id, load_jobs

from .base import BaseCommand

logger = logging.getLogger(__name__)


def resolve_venv_python(script_path: Path) -> Optional[Path]:
    """Walk up from ``script_path.parent`` looking for ``.venv\\Scripts\\python.exe``.

    Returns the resolved interpreter path, or ``None`` when no ancestor
    directory contains a ``.venv``. The walk stops at the filesystem root.
    """
    try:
        cur = script_path.parent.resolve()
    except OSError:
        return None
    for parent in (cur, *cur.parents):
        candidate = parent / ".venv" / "Scripts" / "python.exe"
        if candidate.is_file():
            return candidate
    return None


def build_invocation(job: Job) -> Tuple[List[str], Path, Dict[str, str]]:
    """Resolve how to spawn ``job``.

    Returns ``(argv, cwd, extra_env)``:

    * ``argv`` — list passed to :func:`subprocess.Popen`.
    * ``cwd`` — working directory for the spawn.
    * ``extra_env`` — env-var overlay merged onto ``os.environ`` (only
      meaningful for ``.py`` targets, where ``PYTHONPATH`` is set to the
      resolved project root).
    """
    script = Path(job.script_path)
    suffix = script.suffix.lower()
    args_split = job.args.split() if job.args else []

    if suffix == ".bat":
        if not script.is_file():
            raise OSError(f"BAT file not found: {script}")
        argv = ["cmd.exe", "/c", str(script)] + args_split
        return argv, script.parent, {}

    if suffix == ".py":
        if not script.is_file():
            raise OSError(f"Python script not found: {script}")
        venv_py = resolve_venv_python(script)
        if venv_py is not None:
            python_exe = str(venv_py)
            # <root>/.venv/Scripts/python.exe → <root>
            cwd = venv_py.parent.parent.parent
        else:
            python_exe = sys.executable
            cwd = script.parent
        argv = [python_exe, str(script)] + args_split
        return argv, cwd, {"PYTHONPATH": str(cwd)}

    raise ValueError(f"unsupported script_path suffix: {suffix!r}")


class RunJobCommand(BaseCommand):
    """Argparse subcommand: ``launcher.py run-job <id>``."""

    @classmethod
    def add_parser(cls, subparsers: argparse._SubParsersAction) -> None:
        p = subparsers.add_parser(
            "run-job",
            help="Run a registered job by id (Jobs tab executor)",
        )
        p.add_argument("job_id", help="Job id from config/jobs.json")
        p.add_argument(
            "--trigger",
            default="scheduled",
            choices=["scheduled", "manual"],
            help="Where the run was triggered from (recorded in run.json)",
        )
        p.add_argument(
            "--run-id",
            default=None,
            help=(
                "Reuse an already-created run dir (the webapp pre-creates "
                "one to know the id before spawning detached). When omitted "
                "a fresh timestamped run id is generated."
            ),
        )

    def execute(self, args: argparse.Namespace) -> int:
        cfg = load_jobs()
        job = get_by_id(cfg, args.job_id)
        if job is None:
            logger.error(f"❌ unknown job id: {args.job_id!r}")
            return 2

        try:
            argv, cwd, extra_env = build_invocation(job)
        except (OSError, ValueError) as exc:
            logger.error(f"❌ cannot run job {job.id}: {exc}")
            return 2

        # Webapp-spawned runs pre-create the run dir so the API can
        # return the run id immediately. Scheduled runs (Task Scheduler)
        # arrive without --run-id and create a fresh one.
        if args.run_id:
            run_dir = runs_dir(job.id) / args.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
        else:
            run_dir = new_run_dir(job.id, new_run_id())
        started_at = datetime.now().isoformat(timespec="seconds")
        write_run_json(
            run_dir,
            run_id=run_dir.name,
            job_id=job.id,
            name=job.name,
            trigger=args.trigger,
            script_path=job.script_path,
            args=job.args,
            started_at=started_at,
            status="running",
        )
        logger.info(
            f"🚀 run-job {job.id} → {run_dir.name} (trigger={args.trigger})"
        )

        env = os.environ.copy()
        env.update(extra_env)
        output_log = run_dir / "output.log"
        exit_code: int
        status: str
        try:
            with output_log.open("wb") as fh:
                proc = subprocess.Popen(
                    argv,
                    cwd=str(cwd),
                    env=env,
                    stdout=fh,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                )
                exit_code = proc.wait()
            status = "success" if exit_code == 0 else "failed"
        except OSError as exc:
            logger.error(f"❌ run-job {job.id} spawn failed: {exc}")
            exit_code = -1
            status = "failed"
            try:
                with output_log.open("ab") as fh:
                    fh.write(f"[run-job spawn error] {exc}\n".encode("utf-8"))
            except OSError:
                pass

        finished_at = datetime.now().isoformat(timespec="seconds")
        write_run_json(
            run_dir,
            finished_at=finished_at,
            exit_code=exit_code,
            status=status,
        )
        prune_runs(job.id, keep=MAX_RUNS_PER_JOB)
        logger.info(
            f"🏁 run-job {job.id} {status} (exit={exit_code}, run={run_dir.name})"
        )
        return 0 if exit_code == 0 else 1
