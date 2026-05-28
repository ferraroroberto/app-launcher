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
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.jobs import (
    MAX_RUNS_PER_JOB,
    consecutive_failed_runs,
    cooldown_check,
    delete_schtasks,
    dispatch_chain_run,
    drain_mutex_queue,
    invalidate_stats_cache,
    new_run_dir,
    new_run_id,
    prune_runs,
    read_output_tail,
    resolve_venv_python,
    runs_dir,
    write_run_json,
)
from src.jobs_argv import compose_argv
from src.jobs_config import Job, get_by_id, load_jobs
from src.notifications import (
    Notifier,
    NoopNotifier,
    build_notifier_from_config,
    summarise_failure,
)
from src.webapp_config import WebappConfig, load_webapp_config

from .base import BaseCommand

logger = logging.getLogger(__name__)

# How often the resource sampler thread walks the process tree.
_RESOURCE_SAMPLE_INTERVAL_SECONDS = 1.0


def build_invocation(
    job: Job, values: Optional[Dict[str, Any]] = None
) -> Tuple[List[str], Path, Dict[str, str]]:
    """Resolve how to spawn ``job``.

    Returns ``(argv, cwd, extra_env)``:

    * ``argv`` — list passed to :func:`subprocess.Popen`.
    * ``cwd`` — working directory for the spawn.
    * ``extra_env`` — env-var overlay merged onto ``os.environ``. For
      ``.py`` targets includes ``PYTHONPATH = <project root>``; both
      target kinds also carry any ``env``-mapped typed params (issue #67).

    ``values`` is the typed-parameter payload (issue #67). When empty,
    composition collapses to today's behaviour: argv = [script] +
    job.args.split() with no extra env.
    """
    # Typed parameters compose first; the legacy free-form ``args`` field
    # is whitespace-split and appended as a tail, so parameter-less jobs
    # land at exactly the same argv as before this feature shipped.
    param_argv, param_env = compose_argv(job, values or {})
    legacy_args = job.args.split() if job.args else []
    tail = param_argv + legacy_args

    script = Path(job.script_path)
    suffix = script.suffix.lower()

    if suffix == ".bat":
        if not script.is_file():
            raise OSError(f"BAT file not found: {script}")
        argv = ["cmd.exe", "/c", str(script)] + tail
        return argv, script.parent, dict(param_env)

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
        argv = [python_exe, str(script)] + tail
        extra_env: Dict[str, str] = {"PYTHONPATH": str(cwd)}
        # User-declared env-mapped params override PYTHONPATH only if the
        # user explicitly named the collision — that is their call.
        extra_env.update(param_env)
        return argv, cwd, extra_env

    raise ValueError(f"unsupported script_path suffix: {suffix!r}")


def _tee_pipe_to_file_and_console(pipe: Any, fh: Any) -> None:
    """Stream a child's output ``pipe`` to both ``fh`` and this process's console.

    Used for ``visible`` jobs: ``output.log`` (``fh``) is the remote
    run-history record, and the launcher's own console is what the user
    watches on the PC. A scheduled visible job runs under ``python.exe``
    (see ``src.jobs.task_run_command``) so ``sys.stdout.buffer`` is a real
    console; a pythonw / detached run has no console, so the console half
    is silently dropped while the file half always works. Blocks until the
    child closes the pipe (EOF at child exit); the caller then ``wait()``s.
    """
    console = getattr(sys.stdout, "buffer", None)
    for chunk in iter(lambda: pipe.read(4096), b""):
        fh.write(chunk)
        fh.flush()
        if console is not None:
            try:
                console.write(chunk)
                console.flush()
            except (OSError, ValueError):
                # Broken / closed console — stop teeing, keep filling the log.
                console = None


class _ResourceSampler:
    """Background thread tracking peak RSS + accumulated CPU for a tree.

    Runs once per second while the child process is alive. Every tick
    walks the parent + ``parent.children(recursive=True)``, summing
    ``memory_info().rss`` across the live tree (keeping the running max)
    and recording each PID's max-observed ``cpu_times().user + .system``.
    PIDs are tracked individually because children come and go inside a
    job run; summing across vanished children would otherwise undercount.

    All ``psutil`` errors are swallowed silently — sampling is
    best-effort and must never crash the executor.
    """

    def __init__(self, pid: int) -> None:
        import psutil  # local — keeps psutil out of cold start

        self._psutil = psutil
        self._pid = pid
        self._peak_rss = 0
        self._cpu_per_pid: Dict[int, float] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name=f"run-job-sampler-{pid}", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)

    @property
    def peak_rss_bytes(self) -> int:
        return self._peak_rss

    @property
    def cpu_seconds(self) -> float:
        # Sum the per-pid maximums — gives an upper bound on total CPU
        # spent across the lifetime of the tree, even when some children
        # exited before the next tick.
        return sum(self._cpu_per_pid.values())

    def _loop(self) -> None:
        try:
            parent = self._psutil.Process(self._pid)
        except (self._psutil.NoSuchProcess, self._psutil.AccessDenied):
            return
        while not self._stop.is_set():
            try:
                procs = [parent] + parent.children(recursive=True)
            except (self._psutil.NoSuchProcess, self._psutil.AccessDenied):
                return
            tree_rss = 0
            for p in procs:
                try:
                    mem = p.memory_info().rss
                    tree_rss += mem
                except (self._psutil.NoSuchProcess, self._psutil.AccessDenied):
                    continue
                try:
                    cpu_times = p.cpu_times()
                    total = (cpu_times.user or 0.0) + (cpu_times.system or 0.0)
                    prior = self._cpu_per_pid.get(p.pid, 0.0)
                    if total > prior:
                        self._cpu_per_pid[p.pid] = total
                except (self._psutil.NoSuchProcess, self._psutil.AccessDenied):
                    continue
            if tree_rss > self._peak_rss:
                self._peak_rss = tree_rss
            # Use the stop-event's wait so stop() returns promptly when
            # the child exits — no spinning, no 1 s tail latency.
            if self._stop.wait(_RESOURCE_SAMPLE_INTERVAL_SECONDS):
                return


def _maybe_notify_failure(
    cfg: WebappConfig,
    job: Job,
    run_dir: Path,
    *,
    status: str,
    exit_code: int,
    notifier: Optional[Notifier] = None,
) -> None:
    """Push a Pushover notification for a ``failed`` finalisation.

    No-op when ``notify_on_failure`` is off, when the notifier resolves
    to :class:`NoopNotifier` (no creds), or when the run succeeded. The
    optional LLM summary is best-effort; hub down → raw tail only. Any
    error inside this path is logged and swallowed — finalisation must
    keep going.
    """
    try:
        if status != "failed" or not cfg.notify_on_failure:
            return
        notifier = notifier or build_notifier_from_config(cfg)
        if isinstance(notifier, NoopNotifier):
            return
        tail = read_output_tail(run_dir, max_bytes=8 * 1024)
        body_parts: List[str] = []
        if cfg.notify_failure_summary:
            summary = summarise_failure(tail)
            if summary:
                body_parts.append(summary)
        # The raw tail is what an operator wants when the summary is
        # missing or wrong — always include the last 500 chars.
        body_parts.append(tail[-500:] if tail else "(no output captured)")
        body_parts.append(
            f"— job={job.id} run={run_dir.name} exit={exit_code}"
        )
        title = f"❌ {job.name}"
        notifier.notify(title, "\n\n".join(body_parts), severity="error")

        streak = cfg.notify_failure_streak
        if streak and streak > 1:
            count = consecutive_failed_runs(job.id)
            if count == streak:
                notifier.notify(
                    f"🔁 {job.name} — {count} consecutive failures",
                    f"Failure streak reached {count} runs.\n"
                    f"Most recent: {run_dir.name} (exit {exit_code}).",
                    severity="error",
                )
    except Exception as exc:  # noqa: BLE001 — never block finalisation
        logger.warning(f"⚠️  notification path raised: {exc}")


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
        p.add_argument(
            "--params",
            default=None,
            help=(
                "JSON-encoded {name: value} payload from the run-now "
                "dialog (issue #67). Composed into argv/env via "
                "src.jobs_argv.compose_argv. Omit for parameter-less runs."
            ),
        )
        p.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Dry-run 'execute' mode (issue #69): spawn the target with "
                "JOB_DRY_RUN=1 in its env so opted-in scripts suppress "
                "side effects, and stamp dry_run:true on the run record."
            ),
        )

    def execute(self, args: argparse.Namespace) -> int:
        cfg = load_jobs()
        job = get_by_id(cfg, args.job_id)
        if job is None:
            logger.error(f"❌ unknown job id: {args.job_id!r}")
            return 2

        # Older test scaffolding builds the args namespace by hand and may
        # not set --params; getattr keeps that path working without forcing
        # every caller to fake the new field.
        values: Dict[str, Any] = {}
        params_raw = getattr(args, "params", None)
        if params_raw:
            try:
                values = json.loads(params_raw)
            except json.JSONDecodeError as exc:
                logger.error(f"❌ run-job {job.id}: --params is not JSON ({exc})")
                return 2
            if not isinstance(values, dict):
                logger.error(
                    f"❌ run-job {job.id}: --params must encode a JSON object"
                )
                return 2

        # Scheduled fires that land inside the cooldown window finalise
        # as a no-op `skipped` record. Manual fires are already 429'd at
        # the route — by the time we get here on the manual path either
        # there was no overlap or the caller deliberately bypassed the
        # gate, so we let those through.
        if args.trigger == "scheduled":
            cooldown_state = cooldown_check(job)
            if cooldown_state is not None:
                remaining, cooldown_seconds, anchor_id = cooldown_state
                skip_run_id = args.run_id or new_run_id()
                skip_dir = runs_dir(job.id) / skip_run_id
                skip_dir.mkdir(parents=True, exist_ok=True)
                stamped = datetime.now().isoformat(timespec="seconds")
                write_run_json(
                    skip_dir,
                    run_id=skip_dir.name,
                    job_id=job.id,
                    name=job.name,
                    trigger=args.trigger,
                    script_path=job.script_path,
                    args=job.args,
                    started_at=stamped,
                    finished_at=stamped,
                    status="skipped",
                    note="cooldown",
                    cooldown_seconds=cooldown_seconds,
                    cooldown_remaining_seconds=remaining,
                    cooldown_anchor_run_id=anchor_id or None,
                )
                prune_runs(job.id, keep=MAX_RUNS_PER_JOB)
                invalidate_stats_cache(job.id)
                logger.info(
                    f"⏭ run-job {job.id} skipped (cooldown: {remaining}s "
                    f"remaining of {cooldown_seconds}s; anchor={anchor_id!r})"
                )
                return 0

        try:
            argv, cwd, extra_env = build_invocation(job, values)
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
        dry_run = bool(getattr(args, "dry_run", False))
        run_meta: Dict[str, Any] = dict(
            run_id=run_dir.name,
            job_id=job.id,
            name=job.name,
            trigger=args.trigger,
            script_path=job.script_path,
            args=job.args,
            started_at=started_at,
            status="running",
        )
        # Persist the typed-parameter payload (issue #67) so the history
        # row can replay it and the meta line can show the values back.
        if values:
            run_meta["params"] = values
        # Dry-run 'execute' mode (issue #69): the child still spawns, but
        # JOB_DRY_RUN=1 lets an opted-in script no-op its side effects.
        # The flag is stamped so history shows the 🧪 chip.
        if dry_run:
            run_meta["dry_run"] = True
        write_run_json(run_dir, **run_meta)
        logger.info(
            f"🚀 run-job {job.id} → {run_dir.name} (trigger={args.trigger})"
        )

        env = os.environ.copy()
        env.update(extra_env)
        if dry_run:
            env["JOB_DRY_RUN"] = "1"
        output_log = run_dir / "output.log"
        exit_code: int
        status: str
        sampler: Optional[_ResourceSampler] = None
        spawn_started = time.monotonic()
        try:
            with output_log.open("wb") as fh:
                # A ``visible`` job streams the child's combined output to
                # BOTH output.log (remote run-history) and the launcher's
                # own console (the user watching on the PC). Non-visible
                # jobs write straight to the file as before — no pipe, no
                # reader, byte-for-byte unchanged behaviour.
                stdout_target = subprocess.PIPE if job.visible else fh
                proc = subprocess.Popen(
                    argv,
                    cwd=str(cwd),
                    env=env,
                    stdout=stdout_target,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                )
                # Persist the pid so the kill endpoint can find the tree
                # even if the executor itself crashes before wait() returns.
                write_run_json(run_dir, pid=proc.pid)
                try:
                    sampler = _ResourceSampler(proc.pid)
                    sampler.start()
                except Exception as exc:  # noqa: BLE001 — sampling optional
                    logger.warning(f"⚠️  resource sampler init failed: {exc}")
                    sampler = None
                if job.visible and proc.stdout is not None:
                    _tee_pipe_to_file_and_console(proc.stdout, fh)
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
        finally:
            if sampler is not None:
                sampler.stop()

        finished_at_dt = datetime.now()
        finished_at = finished_at_dt.isoformat(timespec="seconds")
        duration_seconds = round(time.monotonic() - spawn_started, 3)
        fields: Dict[str, object] = {
            "finished_at": finished_at,
            "exit_code": exit_code,
            "status": status,
            "duration_seconds": duration_seconds,
        }
        if sampler is not None:
            fields["peak_rss_bytes"] = sampler.peak_rss_bytes
            fields["cpu_seconds"] = round(sampler.cpu_seconds, 3)
        write_run_json(run_dir, **fields)
        prune_runs(job.id, keep=MAX_RUNS_PER_JOB)
        invalidate_stats_cache(job.id)

        # Failure notification — load the live webapp config so a user
        # change between spawn and finalisation takes effect on the next
        # run without needing a webapp restart.
        try:
            cfg = load_webapp_config()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"⚠️  notify: could not load webapp config: {exc}")
        else:
            _maybe_notify_failure(
                cfg, job, run_dir, status=status, exit_code=exit_code
            )

        # DAG chain — fire configured downstream jobs (on_success /
        # on_failure). Runs BEFORE mutex drain so a chained downstream
        # that shares the same mutex group as a queued sibling lands in
        # the same queue, in fire order. Re-load the registry so a user
        # edit between spawn and finalisation takes effect on the next
        # chain hop without a webapp restart.
        downstream_ids: List[str] = []
        if status == "success":
            downstream_ids = list(job.on_success or [])
        elif status == "failed":
            downstream_ids = list(job.on_failure or [])
        if downstream_ids:
            try:
                chain_cfg = load_jobs()
                for did in downstream_ids:
                    downstream = get_by_id(chain_cfg, did)
                    if downstream is None:
                        logger.warning(
                            f"⚠️  chain: unknown downstream {did!r} from "
                            f"{job.id} (skipping)"
                        )
                        continue
                    try:
                        dispatch_chain_run(chain_cfg.jobs, downstream, job.id)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            f"⚠️  chain: dispatch {did!r} failed: {exc}"
                        )
            except Exception as exc:  # noqa: BLE001 — chain must not block finalise
                logger.warning(f"⚠️  chain: outer dispatch raised: {exc}")

        # One-shot schedules clean themselves up: a `once` job that has
        # just been fired by Task Scheduler removes its schtasks entry
        # (and the in-memory schedule on the registry — leaving it as
        # type=once with an at in the past would let the user re-fire by
        # editing the dialog, but the operational expectation is "fired,
        # done"). Only on scheduled triggers; manual runs of a `once`
        # job leave the schedule alone so a deferred future fire still
        # works. Skip if already paused (defensive — schedule.type is
        # none then anyway).
        if (
            args.trigger == "scheduled"
            and job.schedule.type == "once"
            and not job.is_paused
        ):
            try:
                delete_schtasks(job.id)
                # Mutate the registry so the row stops showing "once …"
                # and surfaces as a plain manual job.
                from src.jobs_config import (  # local import to avoid cycles
                    JobsConfig,
                    Schedule,
                    save_jobs,
                )
                fresh_cfg = load_jobs()
                fresh_job = next(
                    (j for j in fresh_cfg.jobs if j.id == job.id), None
                )
                if fresh_job is not None and fresh_job.schedule.type == "once":
                    fresh_job.schedule = Schedule(type="none")
                    save_jobs(fresh_cfg)
            except Exception as exc:  # noqa: BLE001 — never block finalise
                logger.warning(
                    f"⚠️  once cleanup for {job.id} raised: {exc}"
                )

        # Drain any queued sibling fire in this mutex group. Runs after
        # the head's status has finalised on disk so a parallel route
        # call doing mutex_collision sees this job as done.
        if job.mutex_group:
            try:
                drain_mutex_queue(job.mutex_group)
            except Exception as exc:  # noqa: BLE001 — never block finalisation
                logger.warning(
                    f"⚠️  mutex drain {job.mutex_group!r} raised: {exc}"
                )

        logger.info(
            f"🏁 run-job {job.id} {status} (exit={exit_code}, run={run_dir.name})"
        )
        return 0 if exit_code == 0 else 1
