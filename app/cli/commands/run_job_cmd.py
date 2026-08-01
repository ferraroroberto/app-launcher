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

Every spawned run is guarded by :class:`_RunWatchdog` (issue #695), the
last-resort backstop that kills a wedged tree and finalises the record
``failed`` no matter *why* it wedged — because every safety net further
downstream shares fate with the thing it is guarding.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src import jobs_kinds
from src.diagnostics import kill_process_tree
from src.jobs import (
    MAX_RUNS_PER_JOB,
    consecutive_failed_runs,
    cooldown_check,
    delete_schtasks,
    derived_runtime_ceiling_seconds,
    dispatch_chain_run,
    drain_mutex_queue,
    enqueue_mutex,
    invalidate_stats_cache,
    mutex_collision,
    new_run_dir,
    new_run_id,
    prune_runs,
    read_output_tail,
    runs_dir,
    write_run_json,
)
from src.jobs_argv import compose_argv
from src.jobs_config import Job, get_by_id, load_jobs
from src.jobs_secrets import resolve_env_overlay
from src.jobs_trigger import TRIGGER_SYNTAX, is_valid_trigger
from src.notifications import (
    Notifier,
    NoopNotifier,
    build_notifier_from_config,
    build_telegram_notifier_from_config,
    summarise_failure,
)
from src.subprocess_flags import NO_WINDOW
from src.webapp_config import WebappConfig, load_webapp_config

from .base import BaseCommand

logger = logging.getLogger(__name__)

# How often the resource sampler thread walks the process tree.
_RESOURCE_SAMPLE_INTERVAL_SECONDS = 1.0

# --------------------------------------------------- executor watchdog (#695)
# How often the watchdog thread re-checks its two signals. Coarse on
# purpose: both thresholds are minutes-to-hours, and the tick costs one
# ``stat`` plus one ``poll`` — there is nothing to gain from checking
# faster and a wedged-run diagnosis is never 5 s sensitive.
_WATCHDOG_POLL_INTERVAL_SECONDS = 5.0
# Default ceiling on how long ``output.log`` may stay byte-for-byte
# unchanged before the run is presumed wedged, for a job that doesn't set
# ``no_output_seconds``. Deliberately generous — a job that only prints
# when it finishes is common and must not be killed for being quiet — but
# an hour of total silence from a job the executor is still waiting on is
# the signature of a jam, not of work.
_WATCHDOG_DEFAULT_NO_OUTPUT_SECONDS = 3600.0
# Grace given to the child's tree between ``terminate()`` and ``kill()``,
# matching the manual-kill route (``POST /api/jobs/…/kill``).
_WATCHDOG_KILL_GRACE_SECONDS = 5.0


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


# The console half of a ``visible`` job's tee is best-effort live display
# only (issue #694). A console that stops *draining* — a Windows console
# put into mark/select mode by a click is the classic trigger — blocks
# ``console.write()`` indefinitely with no exception and no timeout. When
# that write sat on the reader thread it stopped the child's pipe being
# drained, the pipe filled, and the whole process tree deadlocked. So the
# console is fed through a bounded queue drained by its own thread, and a
# full queue drops chunks instead of blocking. ``output.log`` stays the
# complete, sequential, flushed record either way.
#
# 64 chunks × 4096 bytes ≈ 256 KB of slack — ample for a briefly-slow
# console, small enough to stay bounded when one wedges for hours.
_CONSOLE_QUEUE_MAX_CHUNKS = 64
# How long to wait at EOF for the console writer to finish what it still
# holds. Bounded on purpose: a wedged console costs this once per run,
# never forever.
_CONSOLE_DRAIN_TIMEOUT_SECONDS = 5.0


def _log_off_thread(name: str, level: int, msg: str, *args: Any) -> None:
    """Emit one log record from a throwaway daemon thread.

    The root logger's ``StreamHandler`` writes to ``stderr``, which for a
    ``visible`` job is the console the user is watching — and a console
    put into mark/select mode blocks every write to it indefinitely, with
    no exception and no timeout. Any breadcrumb emitted *by* the machinery
    that exists to survive a wedged console must therefore not be written
    on that machinery's own thread; ``fleet-config#514`` is the same
    lesson learned the expensive way (a stall watchdog that announced the
    kill before performing it, and was captured by the very jam it was
    built to break). Fire-and-forget on a daemon thread: worst case the
    record is never written, which costs a log line, not a run.
    """
    threading.Thread(
        target=logger.log,
        args=(level, msg, *args),
        name=name,
        daemon=True,
    ).start()


def _log_console_tee_drops(dropped: int) -> None:
    """Leave a breadcrumb that live console output was dropped."""
    _log_off_thread(
        "run-job-console-tee-log",
        logging.WARNING,
        "⚠️  console tee fell behind — dropped %d chunk(s) of live console "
        "output for this run; output.log is unaffected and complete",
        dropped,
    )


def _console_writer_loop(console: Any, chunks: Any) -> None:
    """Drain the ``chunks`` queue onto ``console`` until the ``None`` sentinel.

    Runs on its own daemon thread (see
    :func:`_tee_pipe_to_file_and_console`) so a console that stops
    accepting writes can only ever stall itself — never the reader
    draining the child's pipe. A broken / closed console raises
    ``OSError``/``ValueError``; from then on the loop keeps consuming the
    queue but discards it, so the producer's non-blocking put never
    depends on this thread's health.
    """
    broken = False
    while True:
        chunk = chunks.get()
        if chunk is None:
            return
        if broken:
            continue
        try:
            console.write(chunk)
            console.flush()
        except (OSError, ValueError):
            # Broken / closed console — stop teeing, keep filling the log.
            broken = True


def _tee_pipe_to_file_and_console(pipe: Any, fh: Any) -> None:
    """Stream a child's output ``pipe`` to ``fh``, and best-effort to the console.

    Used for ``visible`` jobs: ``output.log`` (``fh``) is the remote
    run-history record, and the launcher's own console is what the user
    watches on the PC. A scheduled visible job runs under ``python.exe``
    (see ``src.jobs.task_run_command``) so ``sys.stdout.buffer`` is a real
    console; a pythonw / detached run has no console, so the console half
    is never started while the file half always works. Blocks until the
    child closes the pipe (EOF at child exit); the caller then ``wait()``s.

    This thread does pipe-read → file-write only, so nothing the console
    does can stop the child's pipe being drained (issue #694). Console
    chunks go onto a bounded queue consumed by :func:`_console_writer_loop`;
    when that queue is full the chunk is **dropped** — the console is
    lossy live display, ``fh`` is the record.
    """
    console = getattr(sys.stdout, "buffer", None)
    chunks: Optional[Any] = None
    writer: Optional[threading.Thread] = None
    if console is not None:
        chunks = queue.Queue(maxsize=_CONSOLE_QUEUE_MAX_CHUNKS)
        writer = threading.Thread(
            target=_console_writer_loop,
            args=(console, chunks),
            name="run-job-console-tee",
            daemon=True,
        )
        writer.start()
    dropped = 0
    try:
        for chunk in iter(lambda: pipe.read(4096), b""):
            fh.write(chunk)
            fh.flush()
            if chunks is not None:
                try:
                    chunks.put_nowait(chunk)
                except queue.Full:
                    # Console isn't keeping up — drop this chunk rather
                    # than backpressure the child. ``fh`` already has it.
                    dropped += 1
    finally:
        if chunks is not None and writer is not None:
            try:
                chunks.put_nowait(None)
            except queue.Full:
                # Queue full means the console is wedged, so the writer
                # would never reach the sentinel anyway. It's a daemon
                # thread — abandoned at interpreter exit.
                pass
            writer.join(timeout=_CONSOLE_DRAIN_TIMEOUT_SECONDS)
        if dropped:
            _log_console_tee_drops(dropped)


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


def _positive_or_none(seconds: Optional[float]) -> Optional[float]:
    """A watchdog ceiling, or ``None`` when there is nothing to enforce."""
    if seconds is None or seconds <= 0:
        return None
    return float(seconds)


def _fmt_seconds(seconds: float) -> str:
    """Compact human duration for a watchdog note (``45s`` / ``42min`` / ``3h10m``)."""
    total = max(int(seconds), 0)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}min"
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    return f"{hours}h" if minutes == 0 else f"{hours}h{minutes:02d}m"


class _RunWatchdog:
    """Last-resort thread that kills a wedged run (issue #695).

    Every *inner* safety net in a job's stack shares fate with the thing
    it guards — the visible-job console tee, ``claude_progress``'s own
    stall watchdog several layers downstream, whatever a future job kind
    adds. On 2026-07-30 a run froze on a console-backpressure deadlock
    and sat ``running`` for five hours because the inner watchdog that
    should have caught it deadlocked on the same jammed pipe chain.

    The executor is the one layer whose health depends on nothing the
    child does: it needs only to keep a thread ticking and be able to
    call :func:`~src.diagnostics.kill_process_tree`. So this watchdog
    assumes nothing about *why* a run is stuck, and watches two signals
    that need no cooperation from anything downstream:

    * **no output growth** — ``output.log`` hasn't gained a byte in
      ``no_output_seconds``;
    * **total runtime** — wall-clock since spawn exceeds
      ``max_runtime_seconds``.

    Either breach kills the child's whole process tree. That also
    unwedges the main thread for free: the child's pipe closes, the tee
    loop hits EOF, ``proc.wait()`` returns, and ``execute()``'s normal
    finalisation runs — so this class never finalises a run itself, it
    only records *why* it fired for the caller to stamp.

    Both ceilings are resolved on the **main** thread before the thread
    starts (see :func:`_resolve_watchdog_limits`); ``None`` disables that
    signal. Inside the loop nothing is touched but ``Path.stat``,
    ``Popen.poll``, and a bounded ``Event.wait`` — no config load, no
    notifier, no logging (see :func:`_log_off_thread`) — so the watchdog
    can never become the thing that hangs the executor.
    """

    def __init__(
        self,
        proc: "subprocess.Popen[bytes]",
        output_log: Path,
        *,
        max_runtime_seconds: Optional[float],
        no_output_seconds: Optional[float],
    ) -> None:
        self._proc = proc
        self._output_log = output_log
        # Normalise "off" to a single representation. A non-positive
        # ceiling is not a ceiling of zero seconds — it would fire on the
        # first tick of every healthy run — so it disables its signal,
        # exactly like ``None``.
        self._max_runtime = _positive_or_none(max_runtime_seconds)
        self._no_output = _positive_or_none(no_output_seconds)
        self._reason: Optional[str] = None
        self._note: Optional[str] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name=f"run-job-watchdog-{proc.pid}", daemon=True
        )

    @property
    def armed(self) -> bool:
        """``True`` when at least one signal has a ceiling to enforce."""
        return self._max_runtime is not None or self._no_output is not None

    @property
    def fired(self) -> bool:
        return self._reason is not None

    @property
    def reason(self) -> Optional[str]:
        """``"no_output"`` / ``"max_runtime"``, or ``None`` if it never fired."""
        return self._reason

    @property
    def note(self) -> Optional[str]:
        """Human one-liner for the run record, or ``None`` if it never fired."""
        return self._note

    def start(self) -> None:
        if self.armed:
            self._thread.start()

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _log_size(self) -> Optional[int]:
        """``output.log``'s current size, or ``None`` when it can't be read.

        Size rather than mtime on purpose: Windows does not refresh a
        file's directory-entry timestamp while a handle is open, so mtime
        would read as frozen for a perfectly healthy run. ``None`` is
        "can't tell" and the caller must not treat it as "no growth".
        """
        try:
            return self._output_log.stat().st_size
        except OSError:
            return None

    def _loop(self) -> None:
        started = time.monotonic()
        last_growth = started
        last_size = self._log_size()
        while not self._stop.wait(_WATCHDOG_POLL_INTERVAL_SECONDS):
            now = time.monotonic()
            if self._max_runtime is not None and now - started > self._max_runtime:
                self._fire(
                    "max_runtime",
                    f"max runtime {_fmt_seconds(self._max_runtime)} exceeded",
                )
                return
            if self._no_output is None:
                continue
            size = self._log_size()
            if size is None:
                # Unknown, not stalled — skip this tick entirely rather
                # than let an unreadable stat masquerade as no growth.
                continue
            if size != last_size:
                last_size = size
                last_growth = now
                continue
            quiet_for = now - last_growth
            if quiet_for > self._no_output:
                self._fire("no_output", f"no output for {_fmt_seconds(quiet_for)}")
                return

    def _fire(self, reason: str, detail: str) -> None:
        """Kill the child's tree and record why — kill first, log after.

        The ordering is the whole point (``fleet-config#514``): a
        breadcrumb emitted before the kill can be swallowed by the same
        wedge the kill exists to break, and then nothing happens at all.
        The race where the child exits normally a moment before this
        fires is closed by the ``poll()`` check — a run that finished on
        its own must not be reported as watchdog-killed.
        """
        if self._proc.poll() is not None:
            return
        self._reason = reason
        self._note = f"watchdog: {detail}"
        try:
            kill_process_tree(self._proc.pid, _WATCHDOG_KILL_GRACE_SECONDS)
        except Exception:  # noqa: BLE001 — the record still has to be stamped
            pass
        _log_off_thread(
            "run-job-watchdog-log",
            logging.ERROR,
            "🛑 watchdog killed run (pid=%s): %s",
            self._proc.pid,
            self._note,
        )


def _resolve_watchdog_limits(job: Job) -> Tuple[Optional[float], Optional[float]]:
    """Resolve ``(max_runtime_seconds, no_output_seconds)`` for one run.

    Called on the **main** thread before :class:`_RunWatchdog` starts, so
    the watchdog thread itself never reads config or run history — the
    "nothing that can block, inside the thread" constraint of issue #695.

    Both fields are tri-state on :class:`~src.jobs_config.Job`: ``None``
    means "use the default", a positive int is that many seconds, and
    ``0`` means "disable this signal for this job".

    The runtime default reuses the job's own duration history via
    :func:`~src.jobs_stats.derived_runtime_ceiling_seconds` — the same
    ``max(p95 × 3, 300 s)`` heuristic the UI's ⚠️ stuck badge shows — but
    only once there are enough completed runs to trust it. A job with
    thinner history gets no runtime ceiling rather than a fabricated one:
    an unfounded auto-kill of a healthy run is a far worse failure than
    the stuck run the watchdog is there to catch.
    """
    if job.max_runtime_seconds is None:
        try:
            max_runtime = derived_runtime_ceiling_seconds(job.id)
        except Exception as exc:  # noqa: BLE001 — no ceiling beats a wrong one
            logger.warning(f"⚠️  watchdog: could not derive runtime ceiling: {exc}")
            max_runtime = None
    else:
        max_runtime = _positive_or_none(job.max_runtime_seconds)

    if job.no_output_seconds is None:
        no_output: Optional[float] = _WATCHDOG_DEFAULT_NO_OUTPUT_SECONDS
    else:
        no_output = _positive_or_none(job.no_output_seconds)

    # One breadcrumb, on the main thread, before the child exists — so a
    # later "why did / didn't the watchdog fire?" is answerable from the
    # log alone without re-deriving the job's history.
    logger.info(
        f"🐕 watchdog for {job.id}: max_runtime="
        f"{_fmt_seconds(max_runtime) if max_runtime else 'off'}, "
        f"no_output={_fmt_seconds(no_output) if no_output else 'off'}"
    )
    return max_runtime, no_output


def _maybe_notify_failure(
    cfg: WebappConfig,
    job: Job,
    run_dir: Path,
    *,
    status: str,
    exit_code: int,
    notifier: Optional[Notifier] = None,
    telegram_notifier: Optional[Notifier] = None,
) -> None:
    """Push failure notifications for a ``failed`` finalisation.

    Two independent channels, both no-op on success:

    * Pushover (global, issue #66) — gated by ``cfg.notify_on_failure``,
      fires for every job.
    * Telegram (per-job, issue #597) — gated by ``job.alert_on_failure``,
      fires only for jobs that opted in.

    Either resolving to :class:`NoopNotifier` (no creds) is a silent
    no-op for that channel. The optional LLM summary (Pushover only) is
    best-effort; hub down → raw tail only. Any error inside this path is
    logged and swallowed — finalisation must keep going.
    """
    try:
        if status != "failed":
            return

        if cfg.notify_on_failure:
            notifier = notifier or build_notifier_from_config(cfg)
            if not isinstance(notifier, NoopNotifier):
                tail = read_output_tail(run_dir, max_bytes=8 * 1024)
                body_parts: List[str] = []
                if cfg.notify_failure_summary:
                    summary = summarise_failure(tail, base_url=cfg.llm_hub_url)
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

        if job.alert_on_failure:
            telegram_notifier = telegram_notifier or build_telegram_notifier_from_config(cfg)
            if not isinstance(telegram_notifier, NoopNotifier):
                when = datetime.now().strftime("%Y-%m-%d %H:%M")
                telegram_notifier.notify(
                    f"❌ {job.name} failed",
                    f"{when} — run={run_dir.name} exit={exit_code}",
                    severity="error",
                )
    except Exception as exc:  # noqa: BLE001 — never block finalisation
        logger.warning(f"⚠️  notification path raised: {exc}")


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


def _finalize_mutex_queue(
    job: Job,
    jobs: List[Job],
    args: argparse.Namespace,
    values: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """Queue a **scheduled** fire that collides with a live sibling in the
    same ``mutex_group`` instead of running it concurrently (issue #696).

    ``mutex_group`` used to be enforced only on the webapp admission path
    (``app/webapp/routers/jobs_run.py::_admit_and_spawn``), so a
    schtasks-fired run walked straight past it — which is exactly where
    cross-job serialisation matters most (the weekly fleet chain). This is
    the executor-side half of that gate; the drain half already lived here
    (:func:`_drain_mutex_queue_for`).

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
    holder = mutex_collision(jobs, job)
    if holder is None:
        return None
    run_dir = new_run_dir(job.id, new_run_id())
    write_run_json(
        run_dir,
        run_id=run_dir.name,
        job_id=job.id,
        name=job.name,
        trigger=args.trigger,
        trigger_source="schtasks",
        script_path=job.script_path,
        args=job.args,
        started_at=datetime.now().isoformat(timespec="seconds"),
        status="queued",
        mutex_group=job.mutex_group,
        mutex_blocked_by=holder.id,
        **({"params": values} if values else {}),
    )
    enqueue_mutex(
        job.mutex_group,
        {
            "job_id": job.id,
            "run_id": run_dir.name,
            "trigger": args.trigger,
            "params": values or None,
        },
    )
    logger.info(
        f"🪢 run-job {job.id}/{run_dir.name} queued behind {holder.id} "
        f"(mutex_group={job.mutex_group!r}, trigger=scheduled)"
    )
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
        run_id=run_dir.name,
        job_id=job.id,
        name=job.name,
        trigger=args.trigger,
        script_path=job.script_path,
        args=job.args,
        started_at=stamped,
        finished_at=stamped,
        status="failed",
        exit_code=-1,
        note=note,
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
) -> Tuple[int, str, Optional[_ResourceSampler], Optional[_RunWatchdog]]:
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
    sampler: Optional[_ResourceSampler] = None
    watchdog: Optional[_RunWatchdog] = None
    # Resolved here, on the main thread: the watchdog thread must never
    # read run history or config itself (issue #695).
    max_runtime_seconds, no_output_seconds = _resolve_watchdog_limits(job)
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
                sampler = _ResourceSampler(proc.pid)
                sampler.start()
            except Exception as exc:  # noqa: BLE001 — sampling optional
                logger.warning(f"⚠️  resource sampler init failed: {exc}")
                sampler = None
            try:
                watchdog = _RunWatchdog(
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
    sampler: Optional[_ResourceSampler],
    watchdog: Optional[_RunWatchdog] = None,
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
        _maybe_notify_failure(cfg, job, run_dir, status=status, exit_code=exit_code)


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
        # and surfaces as a plain manual job.
        from src.jobs_config import (  # local import to avoid cycles
            JobsConfig,
            Schedule,
            save_jobs,
        )
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
