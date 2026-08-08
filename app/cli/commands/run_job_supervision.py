"""Process supervision for a ``run-job`` execution (issue #723).

Split out of :mod:`app.cli.commands.run_job_cmd`, which had grown to bundle
three unrelated concerns in one module: low-level OS process supervision
(this file), job-lifecycle orchestration, and CLI argument parsing. Nothing
here knows what a *job* is beyond the two watchdog ceilings it reads off
one; it supervises a spawned process tree and reports what it saw.

Three pieces, all of them threads that must survive whatever the child does:

* :func:`tee_pipe_to_file_and_console` — drains the child's output pipe to
  ``output.log`` and, best-effort and lossily, to the launcher's console
  (issue #694).
* :class:`ResourceSampler` — peak RSS and accumulated CPU across the live
  tree (issue #67).
* :class:`RunWatchdog` — the last-resort kill for a wedged run, plus
  :func:`resolve_watchdog_limits`, which resolves its ceilings on the main
  thread so the watchdog thread itself never reads config or run history
  (issue #695).

The one rule the whole module is built around: a safety net that shares fate
with the thing it guards is not a safety net. Nothing in here may block on
the console, on the child's pipes, or on the root logger — see
:func:`_log_off_thread`.
"""

from __future__ import annotations

import logging
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from src.diagnostics import kill_process_tree
from src.jobs_config import Job
from src.jobs_stats import derived_runtime_ceiling_seconds

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
    :func:`tee_pipe_to_file_and_console`) so a console that stops
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


def tee_pipe_to_file_and_console(pipe: Any, fh: Any) -> None:
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


class ResourceSampler:
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


class RunWatchdog:
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
    starts (see :func:`resolve_watchdog_limits`); ``None`` disables that
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


def resolve_watchdog_limits(job: Job) -> Tuple[Optional[float], Optional[float]]:
    """Resolve ``(max_runtime_seconds, no_output_seconds)`` for one run.

    Called on the **main** thread before :class:`RunWatchdog` starts, so
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
