"""Box load behind the page header's CPU reading (#1522).

When the Board or the Code tab feels slow, the header's reading answers "is
the box loaded?" and its sheet answers "by what?": total CPU, memory, and
the top three processes by cores used over the last sample window.

**One shared sampler, never a per-request scan.** A background tick
(:func:`tick_forever`, started from the webapp lifespan) samples every
:data:`TICK_SECONDS` while someone is asking (a read within
:data:`DEMAND_WINDOW_SECONDS`) and idles otherwise. ``GET /api/system/load``
only reads the cached sample (:func:`read`).

**Cheap by construction.** psutil's per-process ``cpu_times()`` costs ~1 ms
a process on Windows (measured: ~600 ms of CPU for ~560 processes, about 12%
of a core every 5 s). One ``NtQuerySystemInformation(SystemProcessInformation)``
call returns every process's name and CPU times at once, in a few
milliseconds, so the Windows sampler uses that. Command lines are read for
the top three only, and only to name an interpreter's script.

**Unknown is its own state.** No sample yet (``warming``), a sample older
than :data:`MAX_AGE_SECONDS` (``stale``: the tick died or stalled) or a
sampler that raised (``error``) all read ``unknown``, never ``0%``.

Breadcrumbs go to ``webapp/slow-requests.log`` (the ``launcher.slowreq``
logger): the live webapp's own stdout is discarded by the tray.
"""

from __future__ import annotations

import asyncio
import ctypes
import functools
import logging
import os
import struct
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

import psutil

breadcrumbs = logging.getLogger("launcher.slowreq")

TICK_SECONDS = 5.0
# How often the tick wakes to check demand; the work only happens on a due tick.
WAKE_SECONDS = 1.0
# The first sample after a baseline publishes this soon, so a page that just
# opened waits ~2 s for its first reading, not a whole tick.
FIRST_WINDOW_SECONDS = 2.0
DEMAND_WINDOW_SECONDS = 60.0
MAX_AGE_SECONDS = 3 * TICK_SECONDS
SUSTAIN_SECONDS = 30.0
ATTENTION_PERCENT = 70.0
DANGER_PERCENT = 90.0
TOP_N = 3
# A sample slower than this leaves a breadcrumb: sampling must stay cheap.
SLOW_SAMPLE_SECONDS = 0.25

# Interpreters whose bare name says nothing ("python 3.1 cores"): their
# script or ``-m`` module is added as a hint, never the command line itself.
_INTERPRETERS = frozenset({"python", "pythonw", "py", "node"})
# Interpreter options whose value is the next argument, not the script.
_VALUE_OPTIONS = frozenset({"-X", "-W", "--check-hash-based-pycs", "-r", "--require"})

LEVEL_NORMAL = "normal"
LEVEL_ATTENTION = "attention"
LEVEL_DANGER = "danger"
LEVEL_UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProcLoad:
    name: str
    cores: float


@dataclass(frozen=True)
class Sample:
    at: float  # time.monotonic() when taken
    window_seconds: float  # the span its averages cover
    cpu_percent: float
    mem_used: int
    mem_total: int
    cores: Optional[int]  # logical CPUs, so "3.1 cores" reads against the box
    top: Tuple[ProcLoad, ...]


@dataclass
class _Baseline:
    at: float
    busy: float
    total: float
    procs: Dict[Tuple[int, int], Tuple[str, float]]


_lock = threading.Lock()
_baseline: Optional[_Baseline] = None
_history: Deque[Sample] = deque()
_last_demand = 0.0
_last_error: Optional[str] = None


# --------------------------------------------------------------- sources


# (pid, create time) -> (image name, cumulative CPU seconds). The create time
# keeps a reused PID from reading as one process's delta.
ProcTimes = Dict[Tuple[int, int], Tuple[str, float]]

_SYSTEM_PROCESS_INFORMATION = 5
_STATUS_INFO_LENGTH_MISMATCH = ctypes.c_long(0xC0000004).value


@functools.lru_cache(maxsize=1)
def _nt_query_system_information() -> Any:
    query = ctypes.WinDLL("ntdll").NtQuerySystemInformation
    query.restype = ctypes.c_long
    query.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    return query


def _windows_process_times() -> ProcTimes:
    """Every process's name and CPU seconds, in one system call.

    Parses ``SYSTEM_PROCESS_INFORMATION`` (64-bit layout): CreateTime at
    +32, UserTime +40, KernelTime +48 (100 ns units), ImageName length +56
    and buffer +64, UniqueProcessId +80.
    """
    query = _nt_query_system_information()
    size = 1 << 20
    while True:
        buf = ctypes.create_string_buffer(size)
        needed = ctypes.c_ulong(0)
        status = query(_SYSTEM_PROCESS_INFORMATION, buf, size, ctypes.byref(needed))
        if status == _STATUS_INFO_LENGTH_MISMATCH:
            size = max(size * 2, needed.value + 65536)
            continue
        if status != 0:
            raise OSError(f"NtQuerySystemInformation failed: 0x{status & 0xFFFFFFFF:08X}")
        break
    out: ProcTimes = {}
    offset = 0
    while True:
        (next_offset,) = struct.unpack_from("<I", buf, offset)
        create, user, kernel = struct.unpack_from("<qqq", buf, offset + 32)
        (name_len,) = struct.unpack_from("<H", buf, offset + 56)
        (name_ptr,) = struct.unpack_from("<Q", buf, offset + 64)
        (pid,) = struct.unpack_from("<Q", buf, offset + 80)
        # PID 0 is the Idle "process": its time is the box doing nothing.
        if pid:
            name = ctypes.wstring_at(name_ptr, name_len // 2) if name_ptr and name_len else ""
            out[(pid, create)] = (name, (user + kernel) / 1e7)
        if not next_offset:
            break
        offset += next_offset
    return out


def _psutil_process_times() -> ProcTimes:
    out: ProcTimes = {}
    for proc in psutil.process_iter(["name", "cpu_times", "create_time"]):
        info = proc.info
        times = info.get("cpu_times")
        if not proc.pid or times is None:
            continue
        create = int((info.get("create_time") or 0) * 1e6)
        out[(proc.pid, create)] = (info.get("name") or "", times.user + times.system)
    return out


def _process_times() -> ProcTimes:
    if sys.platform == "win32" and ctypes.sizeof(ctypes.c_void_p) == 8:
        return _windows_process_times()
    return _psutil_process_times()


def _cpu_busy_total() -> Tuple[float, float]:
    """(busy, total) CPU seconds across every core, cumulative since boot."""
    times = psutil.cpu_times()
    total = sum(times)
    return total - times.idle, total


def _memory() -> Tuple[int, int]:
    mem = psutil.virtual_memory()
    return mem.total - mem.available, mem.total


def _cmdline(pid: int) -> List[str]:
    try:
        return psutil.Process(pid).cmdline()
    except (psutil.Error, OSError):
        return []


# --------------------------------------------------------------- pure rules


def display_name(image: str, cmdline: Iterable[str]) -> str:
    """The process's name for the sheet: never a path or a command line.

    ``python.exe -m pytest ...`` reads ``python (pytest)``; a script reads its
    file stem (``python (launcher)``); anything else is its bare image name.
    """
    base = os.path.basename(image or "")
    stem = base[:-4] if base.lower().endswith(".exe") else base
    if stem.lower() not in _INTERPRETERS:
        return stem or "?"
    args = list(cmdline)[1:]
    hint = ""
    skip_value = False
    for i, arg in enumerate(args):
        if skip_value:
            skip_value = False
            continue
        if arg == "-m" and i + 1 < len(args):
            hint = args[i + 1].split(".")[0]
            break
        if arg == "-c":
            break
        if arg.startswith("-"):
            skip_value = arg in _VALUE_OPTIONS
            continue
        hint = os.path.splitext(os.path.basename(arg.replace("\\", "/")))[0]
        break
    return f"{stem} ({hint})" if hint else stem


def level_for(samples: Iterable[Sample]) -> str:
    """The reading's tone from the samples so far (oldest first).

    ``danger`` when the latest window averaged ≥ :data:`DANGER_PERCENT`;
    ``attention`` when every sample across a full :data:`SUSTAIN_SECONDS`
    stayed ≥ :data:`ATTENTION_PERCENT`; else ``normal``.
    """
    samples = list(samples)
    if not samples:
        return LEVEL_UNKNOWN
    latest = samples[-1]
    if latest.cpu_percent >= DANGER_PERCENT:
        return LEVEL_DANGER
    covered = 0.0
    for sample in reversed(samples):
        if sample.cpu_percent < ATTENTION_PERCENT:
            return LEVEL_NORMAL
        covered += sample.window_seconds
        if covered >= SUSTAIN_SECONDS - 0.5:
            return LEVEL_ATTENTION
    return LEVEL_NORMAL


def _top(before: ProcTimes, after: ProcTimes, elapsed: float) -> List[Tuple[int, str, float]]:
    rows = []
    for key, (name, seconds) in after.items():
        # A process born inside the window spent all its CPU time inside it,
        # so it counts from zero: a short pytest worker must not hide.
        prev = before.get(key)
        cores = (seconds - (prev[1] if prev else 0.0)) / elapsed
        if cores > 0:
            rows.append((key[0], name, cores))
    rows.sort(key=lambda r: r[2], reverse=True)
    return rows[:TOP_N]


# --------------------------------------------------------------- sampler


def tick_once(now: Optional[float] = None) -> Optional[Sample]:
    """Baseline or sample, whichever is due. Blocking; the tick runs it off the loop.

    A missing or stale baseline (demand lapsed) is re-taken and the history
    dropped, so a sustained reading never spans an idle gap.
    """
    global _baseline, _last_error
    now = time.monotonic() if now is None else now
    with _lock:
        baseline = _baseline
        has_sample = bool(_history)
    if baseline is None or now - baseline.at > 2 * TICK_SECONDS:
        busy, total = _cpu_busy_total()
        procs = _process_times()
        with _lock:
            _baseline = _Baseline(now, busy, total, procs)
            _history.clear()
        return None
    due = TICK_SECONDS if has_sample else FIRST_WINDOW_SECONDS
    if now - baseline.at < due:
        return None

    started = time.perf_counter()
    busy, total = _cpu_busy_total()
    procs = _process_times()
    used, mem_total = _memory()
    elapsed = now - baseline.at
    total_delta = total - baseline.total
    cpu = 100.0 * (busy - baseline.busy) / total_delta if total_delta > 0 else 0.0
    top = tuple(
        ProcLoad(display_name(name, _cmdline(pid)), round(cores, 2))
        for pid, name, cores in _top(baseline.procs, procs, elapsed)
    )
    sample = Sample(
        now, elapsed, max(0.0, min(100.0, cpu)), used, mem_total, psutil.cpu_count() or None, top
    )
    took = time.perf_counter() - started
    with _lock:
        _baseline = _Baseline(now, busy, total, procs)
        _history.append(sample)
        while _history and _history[0].at < now - SUSTAIN_SECONDS - 2 * TICK_SECONDS:
            _history.popleft()
        if _last_error is not None:
            breadcrumbs.info("✅ system load sampler recovered")
        _last_error = None
    if took > SLOW_SAMPLE_SECONDS:
        breadcrumbs.warning("⚠️ system load sample took %.2fs (budget %.2fs)", took, SLOW_SAMPLE_SECONDS)
    return sample


def _demanded() -> bool:
    with _lock:
        return time.monotonic() - _last_demand <= DEMAND_WINDOW_SECONDS


async def tick_forever() -> None:
    """Sample every TICK_SECONDS while the header polls; idle otherwise."""
    global _last_error
    while True:
        await asyncio.sleep(WAKE_SECONDS)
        if not _demanded():
            continue
        try:
            await asyncio.to_thread(tick_once)
        except Exception as exc:  # noqa: BLE001 — a failed tick must not end the loop
            with _lock:
                first = _last_error is None
                _last_error = f"{type(exc).__name__}: {exc}"
            if first:
                breadcrumbs.exception("❌ system load sample failed; the header reads unknown")


def read(now: Optional[float] = None) -> Dict[str, Any]:
    """The cached reading, for ``GET /api/system/load``. Never samples."""
    global _last_demand
    now = time.monotonic() if now is None else now
    with _lock:
        _last_demand = now
        samples = list(_history)
        error = _last_error
    latest = samples[-1] if samples else None
    reason: Optional[str] = None
    if error is not None:
        reason = "error"
    elif latest is None:
        reason = "warming"
    elif now - latest.at > MAX_AGE_SECONDS:
        reason = "stale"
    if reason is not None:
        return {
            "level": LEVEL_UNKNOWN,
            "reason": reason,
            "cpu_percent": None,
            "memory": None,
            "top": [],
            "cores": None,
            "age_seconds": None if latest is None else round(now - latest.at, 1),
        }
    return {
        "level": level_for(samples),
        "reason": None,
        "cpu_percent": round(latest.cpu_percent, 1),
        "memory": {
            "used_bytes": latest.mem_used,
            "total_bytes": latest.mem_total,
            "percent": round(100.0 * latest.mem_used / latest.mem_total, 1) if latest.mem_total else None,
        },
        "top": [{"name": p.name, "cores": p.cores} for p in latest.top],
        "cores": latest.cores,
        "age_seconds": round(now - latest.at, 1),
    }


def reset() -> None:
    """Drop all state. Tests only."""
    global _baseline, _last_demand, _last_error
    with _lock:
        _baseline = None
        _history.clear()
        _last_demand = 0.0
        _last_error = None
