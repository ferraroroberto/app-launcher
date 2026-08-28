"""``run-job`` subcommand — the sole executor for the Jobs tab.

Every Jobs-tab trigger funnels through this one entry point:

* Windows Task Scheduler fires ``pythonw launcher.py run-job <id>``
  (scheduled runs).
* The webapp's ``POST /api/jobs/<id>/run`` spawns the same command
  detached (manual / Stream Deck / phone tap).

Both paths produce identical run records under ``webapp/jobs/<id>/<rid>/``
— ``run.json`` (metadata) plus ``output.log`` (combined stdout+stderr).

Target dispatch (issue #70) goes through the job-kind registry in
:mod:`src.jobs_kinds` — one module per kind (``python``, ``batch``,
``powershell``, ``shell-wsl``, ``inline-shell``, ``http-check``), each
contributing a ``build_argv()``. ``build_invocation`` here just resolves
which kind a job is (:func:`src.jobs_kinds.resolve_kind`) and delegates;
see that package's docstring for the dispatch shape each kind produces.

``args`` is split on whitespace before being appended to argv. Jobs that
need arguments containing spaces should put the argument inside the
script/wrapper itself rather than relying on shell quoting.

Every spawned run is guarded by
:class:`~app.cli.commands.run_job_supervision.RunWatchdog` (issue #695), the
last-resort backstop that kills a wedged tree and finalises the record
``failed`` no matter *why* it wedged — because every safety net further
downstream shares fate with the thing it is guarding.

This module owns job-lifecycle *orchestration* (cooldown, the mutex queue,
chain dispatch, finalisation) plus the CLI wiring. The OS-level process
supervision a run depends on — the output tee, the resource sampler and that
watchdog — lives next door in :mod:`app.cli.commands.run_job_supervision`,
split out in issue #723.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src import jobs_kinds
from src.jobs import (
    MAX_RUNS_PER_JOB,
    admit_and_spawn,
    cooldown_check,
    delete_schtasks,
    dispatch_chain_run,
    drain_mutex_queue,
    invalidate_stats_cache,
    new_run_dir,
    new_run_id,
    prune_runs,
    runs_dir,
    seed_run_meta,
    write_run_json,
)
from src.jobs_argv import compose_argv
from src.jobs_config import Job, get_by_id, load_jobs
from src.jobs_secrets import resolve_env_overlay
from src.jobs_trigger import TRIGGER_SYNTAX, is_valid_trigger
from src.notifications import notify_failure
from src.subprocess_flags import NO_WINDOW
from src.webapp_config import load_webapp_config

from .base import BaseCommand
from .run_job_supervision import (
    ResourceSampler,
    RunWatchdog,
    resolve_watchdog_limits,
    tee_pipe_to_file_and_console,
)

logger = logging.getLogger(__name__)


def build_invocation(
    job: Job, values: Optional[Dict[str, Any]] = None, run_dir: Optional[Path] = None
) -> Tuple[List[str], Path, Dict[str, str]]:
    """Resolve how to spawn ``job`` by dispatching through the job-kind
    registry (:mod:`src.jobs_kinds`, issue #70).

    Returns ``(argv, cwd, extra_env)``:

    * ``argv`` — list passed to :func:`subprocess.Popen`.
    * ``cwd`` — working directory for the spawn.
    * ``extra_env`` — env-var overlay merged onto ``os.environ``. Every
      kind carries any ``env``-mapped typed params (issue #67); ``python``
      additionally sets ``PYTHONPATH = <project root>``.

    ``values`` is the typed-parameter payload (issue #67). When empty,
    composition collapses to today's behaviour: argv = [script] +
    job.args.split() with no extra env. ``run_dir`` is the run's own
    directory — required so ``inline-shell`` can write its temp script
    there; every other kind ignores it.
    """
    # Typed parameters compose first; the legacy free-form ``args`` field
    # is whitespace-split and appended as a tail, so parameter-less jobs
    # land at exactly the same argv as before this feature shipped.
    param_argv, param_env = compose_argv(job, values or {})
    legacy_args = job.args.split() if job.args else []
    tail = param_argv + legacy_args

    kind_name = jobs_kinds.resolve_kind(job)
    kind_impl = jobs_kinds.KINDS.get(kind_name)
    if kind_impl is None:
        raise ValueError(f"unsupported job kind: {kind_name!r} (job {job.id})")
    if run_dir is None:
        raise ValueError("run_dir is required to build a job invocation")
    return kind_impl.build_argv(job, tail, param_env, run_dir)


def _parse_run_params(
    job: Job, params_raw: Optional[str]
) -> Tuple[Dict[str, Any], Optional[int]]:
    """Decode the ``--params`` JSON payload (issue #67).

    Returns ``(values, error_exit_code)``. ``error_exit_code`` is ``None``
    on success (``values`` may be an empty dict for parameter-less runs);
    otherwise ``values`` is ``{}`` and the caller should return the code
    immediately.
    """
    if not params_raw:
        return {}, None
    try:
        values = json.loads(params_raw)
    except json.JSONDecodeError as exc:
        logger.error(f"❌ run-job {job.id}: --params is not JSON ({exc})")
        return {}, 2
    if not isinstance(values, dict):
        logger.error(f"❌ run-job {job.id}: --params must encode a JSON object")
        return {}, 2
    return values, None


def _finalize_cooldown_skip(job: Job, args: argparse.Namespace) -> Optional[int]:
    """Finalise a scheduled fire that lands inside the cooldown window as
    a no-op ``skipped`` run record.

    Manual fires are already 429'd at the route — by the time we get here
    on the manual path either there was no overlap or the caller
    deliberately bypassed the gate, so those are let through (``None``).
    Returns ``0`` when the run was skipped and finalised; ``None`` when
    there's no cooldown to apply and the caller should proceed with a
    real invocation.

    Unlike :func:`_finalize_mutex_queue`, this used to have no "already
    drained" scoping — a fire that was itself mutex-queued and later
    released by :func:`_drain_mutex_queue_for` re-enters here with
    ``trigger="scheduled"`` and ``--run-id`` set, and can still land
    inside its own cooldown window (e.g. a manual fire of the same job
    landed while it sat queued). Finalising that as skipped without
    draining again stranded any sibling still queued behind it until some
    unrelated job in the group happened to finish (issue #755).

    Fixed by draining the job's own ``mutex_group`` here too — but only
    for that replay case (``args.run_id`` set), mirroring
    :func:`_finalize_mutex_queue`'s own "already admitted" scoping. A
    *fresh* scheduled fire (no ``run_id``) was never popped from the
    queue, so its group may currently be held by a live, still-running
    sibling; draining unconditionally here would spawn the queue's head
    concurrently with that holder instead of after it, defeating the
    mutex entirely. Only a replay is guaranteed to mean the group's
    previous holder already finished (that's the drain precondition that
    let this fire be popped and spawned in the first place).
    """
    if args.trigger != "scheduled":
        return None
    cooldown_state = cooldown_check(job)
    if cooldown_state is None:
        return None
    remaining, cooldown_seconds, anchor_id = cooldown_state
    skip_run_id = args.run_id or new_run_id()
    skip_dir = runs_dir(job.id) / skip_run_id
    skip_dir.mkdir(parents=True, exist_ok=True)
    stamped = datetime.now().isoformat(timespec="seconds")
    write_run_json(
        skip_dir,
        **seed_run_meta(
            job,
            args.trigger,
            stamped,
            run_id=skip_dir.name,
            finished_at=stamped,
            status="skipped",
            note="cooldown",
            cooldown_seconds=cooldown_seconds,
            cooldown_remaining_seconds=remaining,
            cooldown_anchor_run_id=anchor_id or None,
        ),
    )
    prune_runs(job.id, keep=MAX_RUNS_PER_JOB)
    invalidate_stats_cache(job.id)
    logger.info(
        f"⏭ run-job {job.id} skipped (cooldown: {remaining}s "
        f"remaining of {cooldown_seconds}s; anchor={anchor_id!r})"
    )
    # Only a replay of an already-popped queue entry (run_id set) is safe
    # to drain here — see the scoping note in the docstring above.
    if job.mutex_group and args.run_id:
        _drain_mutex_queue_for(job)
    return 0


def _finalize_mutex_queue(
    job: Job,
    jobs: List[Job],
    args: argparse.Namespace,
    values: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """Queue a **scheduled** fire that collides with a live sibling in the
    same ``mutex_group`` instead of running it concurrently (issue #696).

    ``mutex_group`` used to be enforced only on the webapp admission path,
    so a schtasks-fired run walked straight past it — which is exactly
    where cross-job serialisation matters most (the weekly fleet chain).
    This is the executor-side half of that gate; the drain half already
    lived here (:func:`_drain_mutex_queue_for`). The queueing itself is
    :func:`~src.jobs_queue.admit_and_spawn` in its ``spawn_when_free=False``
    mode, shared with the route and the chain dispatcher (issue #794) —
    only the scoping below is executor-specific.

    Returns ``0`` when the fire was queued and finalised, ``None`` when
    there's nothing to queue and the caller should run normally.

    Scoped to fires that have not already been through an admission gate:

    * ``trigger != "scheduled"`` — manual / webhook / API fires are admitted
      at the route, chain fires at :func:`~src.jobs_queue.dispatch_chain_run`.
      A second gate here would re-queue a fire the caller was already told
      would run. (Same shape as the ``scheduled``-only cooldown gate in
      :func:`_finalize_cooldown_skip`.)
    * ``args.run_id`` set — the run dir was pre-created by whoever admitted
      it. Task Scheduler is the only caller that arrives without one
      (``src.jobs_schtasks.task_run_command``); every admitted path
      (route + :func:`~src.jobs_queue.drain_mutex_queue`) passes
      ``--run-id``. This is what stops a *drained* queue entry — which
      replays its original ``trigger="scheduled"`` — from being pushed
      back onto the tail of the very queue it was just released from.
    """
    if args.trigger != "scheduled" or not job.mutex_group:
        return None
    if getattr(args, "run_id", None):
        return None
    # Queue half only: this process is already the executor that would run
    # the job inline, so spawn_when_free=False means "no collision → touch
    # nothing, tell the caller to proceed". Shares the queued-record shape
    # with the route and the chain dispatcher (issue #794).
    meta = admit_and_spawn(
        jobs,
        job,
        args.trigger,
        params=values,
        extra_meta={"trigger_source": "schtasks"},
        spawn_when_free=False,
    )
    if meta is None:
        return None
    return 0


def _record_failed_run(
    job: Job,
    args: argparse.Namespace,
    run_dir: Path,
    note: str,
) -> None:
    """Finalise ``run_dir`` as a visible ``failed`` record, then prune and
    invalidate the stats cache (issue #689).

    The shared bookkeeping of every pre-spawn failure path: the run
    directory already exists, so leaving it empty would silently strand a
    directory on each fire of a misconfigured job. ``note`` is the only
    thing that differs between callers.
    """
    stamped = datetime.now().isoformat(timespec="seconds")
    write_run_json(
        run_dir,
        **seed_run_meta(
            job,
            args.trigger,
            stamped,
            run_id=run_dir.name,
            finished_at=stamped,
            status="failed",
            exit_code=-1,
            note=note,
        ),
    )
    prune_runs(job.id, keep=MAX_RUNS_PER_JOB)
    invalidate_stats_cache(job.id)


def _build_invocation_or_record_failure(
    job: Job,
    args: argparse.Namespace,
    values: Dict[str, Any],
    run_dir: Path,
) -> Optional[Tuple[List[str], Path, Dict[str, str]]]:
    """Resolve the job's invocation, or finalise a ``failed`` run record
    on a build error and return ``None``.

    ``run_dir`` already exists at this point — created by the caller so
    inline-shell can write its temp script into it — so the failure path
    goes through :func:`_record_failed_run`, matching the Popen-spawn
    failure path in :func:`_spawn_and_wait`.
    """
    try:
        return build_invocation(job, values, run_dir)
    except (OSError, ValueError) as exc:
        logger.error(f"❌ cannot run job {job.id}: {exc}")
        _record_failed_run(job, args, run_dir, f"invocation error: {exc}")
        return None


def _resolve_job_env_or_record_failure(
    job: Job,
    args: argparse.Namespace,
    run_dir: Path,
) -> Optional[Dict[str, str]]:
    """Resolve the job's ``env`` overlay (issue #72), or finalise a
    ``failed`` run record on an unresolvable ``$secret:`` reference and
    return ``None``.

    Loads the live webapp config so a secrets edit takes effect on the
    next fire without a restart — same freshness contract as the
    notification path in :func:`_finalize_run`. No ``env`` → ``{}``.
    """
    if not job.env:
        return {}
    try:
        cfg = load_webapp_config()
        return resolve_env_overlay(job.env, cfg.secrets)
    except ValueError as exc:
        logger.error(f"❌ cannot run job {job.id}: {exc}")
        _record_failed_run(job, args, run_dir, str(exc))
        return None


def _spawn_and_wait(
    job: Job,
    argv: List[str],
    cwd: Path,
    env: Dict[str, str],
    run_dir: Path,
) -> Tuple[int, str, Optional[ResourceSampler], Optional[RunWatchdog]]:
    """Spawn the job's process, tee output for ``visible`` jobs, sample
    resource usage, guard it with the last-resort watchdog, and wait for
    it to exit.

    Returns ``(exit_code, status, sampler, watchdog)`` — ``status`` is
    ``"success"`` or ``"failed"``; ``sampler`` / ``watchdog`` are ``None``
    when that thread couldn't start. Persists the child's ``pid`` onto the
    run record as soon as it's known so the kill endpoint can find the
    tree even if this executor crashes before ``wait()`` returns.
    """
    output_log = run_dir / "output.log"
    sampler: Optional[ResourceSampler] = None
    watchdog: Optional[RunWatchdog] = None
    # Resolved here, on the main thread: the watchdog thread must never
    # read run history or config itself (issue #695).
    max_runtime_seconds, no_output_seconds = resolve_watchdog_limits(job)
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
                creationflags=NO_WINDOW,
            )
            # Persist the pid so the kill endpoint can find the tree
            # even if the executor itself crashes before wait() returns.
            # pid_create_time (captured immediately, mirroring
            # src.app_runtime.record_spawn) lets a later reap check
            # (src.jobs_reap) tell "still this process" apart from "a
            # since-recycled pid" — Windows reuses pids, so a bare
            # pid_exists() isn't enough (issue #591).
            write_run_json(run_dir, pid=proc.pid, pid_create_time=time.time())
            try:
                sampler = ResourceSampler(proc.pid)
                sampler.start()
            except Exception as exc:  # noqa: BLE001 — sampling optional
                logger.warning(f"⚠️  resource sampler init failed: {exc}")
                sampler = None
            try:
                watchdog = RunWatchdog(
                    proc,
                    output_log,
                    max_runtime_seconds=max_runtime_seconds,
                    no_output_seconds=no_output_seconds,
                )
                watchdog.start()
            except Exception as exc:  # noqa: BLE001 — backstop is best-effort
                logger.warning(f"⚠️  run watchdog init failed: {exc}")
                watchdog = None
            if job.visible and proc.stdout is not None:
                tee_pipe_to_file_and_console(proc.stdout, fh)
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
        if watchdog is not None:
            watchdog.stop()
    if watchdog is not None and watchdog.fired:
        # A watchdog kill is a failure whatever exit code the torn-down
        # tree happened to report on its way out.
        status = "failed"
    return exit_code, status, sampler, watchdog


def _finalize_run(
    job: Job,
    run_dir: Path,
    *,
    exit_code: int,
    status: str,
    spawn_started: float,
    sampler: Optional[ResourceSampler],
    watchdog: Optional[RunWatchdog] = None,
) -> None:
    """Stamp the run's terminal fields, prune history, invalidate the
    stats cache, and fire a failure notification if warranted.

    Runs after the child process has exited (or failed to spawn) but
    before chain dispatch / once-cleanup / mutex drain — those steps read
    the finalised run record.

    A watchdog kill (issue #695) is stamped as its own state —
    ``watchdog: true`` plus a ``watchdog_reason`` and a human ``note`` —
    so it is never confused with a plain ``failed`` (the job's own bad
    exit code) or with ``killed: true`` (an operator tapped Kill). A run
    the watchdog never touched gains none of those keys, so the common
    case's record shape is unchanged.
    """
    finished_at = datetime.now().isoformat(timespec="seconds")
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
    if watchdog is not None and watchdog.fired:
        fields["watchdog"] = True
        fields["watchdog_reason"] = watchdog.reason
        fields["note"] = watchdog.note
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
        notify_failure(cfg, job, run_dir, status=status, exit_code=exit_code)


def _dispatch_chain(job: Job, status: str) -> None:
    """Fire configured downstream jobs (``on_success`` / ``on_failure``).

    Runs BEFORE mutex drain so a chained downstream that shares the same
    mutex group as a queued sibling lands in the same queue, in fire
    order. Re-loads the registry so a user edit between spawn and
    finalisation takes effect on the next chain hop without a webapp
    restart.
    """
    downstream_ids: List[str] = []
    if status == "success":
        downstream_ids = list(job.on_success or [])
    elif status == "failed":
        downstream_ids = list(job.on_failure or [])
    if not downstream_ids:
        return
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
                logger.warning(f"⚠️  chain: dispatch {did!r} failed: {exc}")
    except Exception as exc:  # noqa: BLE001 — chain must not block finalise
        logger.warning(f"⚠️  chain: outer dispatch raised: {exc}")


def _cleanup_once_schedule(job: Job, args: argparse.Namespace) -> None:
    """One-shot schedules clean themselves up.

    A ``once`` job that has just been fired by Task Scheduler removes its
    schtasks entry (and the in-memory schedule on the registry — leaving
    it as ``type=once`` with an ``at`` in the past would let the user
    re-fire by editing the dialog, but the operational expectation is
    "fired, done"). Only on scheduled triggers; manual runs of a ``once``
    job leave the schedule alone so a deferred future fire still works.
    Skips if already paused (defensive — ``schedule.type`` is ``none``
    then anyway).
    """
    if not (
        args.trigger == "scheduled"
        and job.schedule.type == "once"
        and not job.is_paused
    ):
        return
    try:
        delete_schtasks(job.id)
        # Mutate the registry so the row stops showing "once …"
        # and surfaces as a plain manual job. Under the shared
        # jobs_file_lock (issue #755) so this unlocked-until-now
        # load-mutate-save can't race the webapp's own PUT/POST
        # /api/jobs/* mutators (which take the same lock) and silently
        # clobber one writer's change with a stale os.replace.
        from src.jobs_config import (  # local import to avoid cycles
            Schedule,
            jobs_file_lock,
            save_jobs,
        )
        with jobs_file_lock():
            fresh_cfg = load_jobs()
            fresh_job = next((j for j in fresh_cfg.jobs if j.id == job.id), None)
            if fresh_job is not None and fresh_job.schedule.type == "once":
                fresh_job.schedule = Schedule(type="none")
                save_jobs(fresh_cfg)
    except Exception as exc:  # noqa: BLE001 — never block finalise
        logger.warning(f"⚠️  once cleanup for {job.id} raised: {exc}")


def _drain_mutex_queue_for(job: Job) -> None:
    """Drain any queued sibling fire in this job's mutex group.

    Runs after the head's status has finalised on disk so a parallel
    route call doing ``mutex_collision`` sees this job as done.
    """
    if not job.mutex_group:
        return
    try:
        drain_mutex_queue(job.mutex_group)
    except Exception as exc:  # noqa: BLE001 — never block finalisation
        logger.warning(f"⚠️  mutex drain {job.mutex_group!r} raised: {exc}")


def _trigger_arg(value: str) -> str:
    """argparse ``type=`` for ``--trigger``.

    A plain ``choices=`` list cannot express the ``chain:<upstream_id>``
    shape, and enumerating only the two literals the *user* ever types
    silently rejected the two values the code itself constructs —
    ``chain:<id>`` and ``webhook`` — killing every chained and every
    webhook fire in argparse before the executor ran (issue #687). The
    vocabulary lives in :mod:`src.jobs_trigger`; this only adapts it to
    argparse's error protocol, so an unknown value still fails loudly
    rather than being accepted as free text.
    """
    if not is_valid_trigger(value):
        raise argparse.ArgumentTypeError(
            f"invalid trigger {value!r}: expected {TRIGGER_SYNTAX}"
        )
    return value


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
            type=_trigger_arg,
            help=(
                "Where the run was triggered from (recorded in run.json): "
                f"{TRIGGER_SYNTAX}"
            ),
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
        values, params_error = _parse_run_params(job, getattr(args, "params", None))
        if params_error is not None:
            return params_error

        skip_exit_code = _finalize_cooldown_skip(job, args)
        if skip_exit_code is not None:
            return skip_exit_code

        # Mutex-group admission for schtasks-fired runs (issue #696). Runs
        # after cooldown, matching the route's order: a cooled-down fire is
        # a no-op that shouldn't occupy a slot in the group's queue.
        queued_exit_code = _finalize_mutex_queue(job, cfg.jobs, args, values)
        if queued_exit_code is not None:
            return queued_exit_code

        # Webapp-spawned runs pre-create the run dir so the API can
        # return the run id immediately. Scheduled runs (Task Scheduler)
        # arrive without --run-id and create a fresh one. This has to
        # happen before build_invocation: the inline-shell kind (issue #70)
        # writes its temp script into run_dir so it's preserved alongside
        # run.json / output.log.
        if args.run_id:
            run_dir = runs_dir(job.id) / args.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
        else:
            run_dir = new_run_dir(job.id, new_run_id())

        invocation = _build_invocation_or_record_failure(job, args, values, run_dir)
        if invocation is None:
            return 2
        argv, cwd, extra_env = invocation

        # Per-job env overlay (issue #72) — resolved before the run flips
        # to "running" so an unresolvable $secret: reference finalises as
        # a clean failed record, mirroring the invocation-error path.
        job_env = _resolve_job_env_or_record_failure(job, args, run_dir)
        if job_env is None:
            return 2

        started_at = datetime.now().isoformat(timespec="seconds")
        dry_run = bool(getattr(args, "dry_run", False))
        run_meta: Dict[str, Any] = seed_run_meta(
            job, args.trigger, started_at, run_id=run_dir.name, status="running"
        )
        # Persist the typed-parameter payload (issue #67) so the history
        # row can replay it and the meta line can show the values back.
        if values:
            run_meta["params"] = values
        # Provenance (issue #72): a Task Scheduler fire reaches this
        # executor directly, so it stamps its own source here; API fires
        # carry trigger_source="api" (+ip/ua/token id) pre-written by the
        # route into the run dir this executor merges into.
        if args.trigger == "scheduled":
            run_meta["trigger_source"] = "schtasks"
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
        # Job env overlay first, then the invocation's own extra_env (typed
        # params + kind env) so a per-run param can override a static value.
        env.update(job_env)
        env.update(extra_env)
        artifact_dir = run_dir / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        env["JOB_ARTIFACT_DIR"] = str(artifact_dir.resolve())
        if dry_run:
            env["JOB_DRY_RUN"] = "1"
        spawn_started = time.monotonic()
        exit_code, status, sampler, watchdog = _spawn_and_wait(
            job, argv, cwd, env, run_dir
        )

        _finalize_run(
            job,
            run_dir,
            exit_code=exit_code,
            status=status,
            spawn_started=spawn_started,
            sampler=sampler,
            watchdog=watchdog,
        )
        _dispatch_chain(job, status)
        _cleanup_once_schedule(job, args)
        _drain_mutex_queue_for(job)

        watchdog_note = (
            f", {watchdog.note}" if watchdog is not None and watchdog.fired else ""
        )
        logger.info(
            f"🏁 run-job {job.id} {status} "
            f"(exit={exit_code}, run={run_dir.name}{watchdog_note})"
        )
        # A watchdog kill is a failed run even in the pathological case
        # where the torn-down tree still reported a zero exit code.
        if status == "failed":
            return 1
        return 0 if exit_code == 0 else 1
