"""Post-run sweep for leaked Playwright browser helper processes (issue #203).

The companion to `docs/playwright-ui-testing.md`'s "Bounded WebKit driver
teardown" watchdog. That watchdog protects the *current* run's exit path from
a *pre-existing* wedge; it does nothing about browser **child** processes
(`WebKitNetworkProcess.exe` and siblings) that outlive the run. This module is
the missing half: after the session ends, look for helper processes that
belong to *this* checkout and are genuinely orphaned, and tree-kill them.

Two findings from #203's investigation shape the design — read them before
changing anything here:

1. **Classify before killing; an already-exited helper is not a leak.** Every
   "orphan" inspected on the fleet host reported `GetExitCodeProcess` == 0 —
   it had already exited cleanly. Windows keeps an exited process's object
   (and its row in `tasklist` / `Win32_Process` / bulk `Get-Process`) alive
   until the last handle to it closes, so a *zombie* looks exactly like a
   live orphan to a name-based scan. It cannot be killed (`taskkill` and
   `Stop-Process` correctly answer "no such process") and it holds no
   sockets. A sweep that treats these as failures never goes green, so they
   get their own verdict (`VERDICT_ZOMBIE`) and no kill attempt.
2. **Never kill by name.** A kill needs three independent facts: the process
   is really running, its parent is dead (a *live* parent means a legitimate
   in-flight session — an agent's headed verification loop, a sibling job),
   and its working directory is under the scope path this run owns. Anything
   the sweep cannot establish gets its own verdict, never folded into
   "killed" or "clean" — the same rule as the fleet's shared-Chrome-profile
   and safe-restart conventions: never kill a live holder.

Working directory is what attributes a helper back to the checkout that
spawned it: helpers inherit the pytest process's cwd, so a run inside
`…/repo-wt-203` leaves helpers whose cwd is `…/repo-wt-203` — which is also
why such a leak blocks `git worktree remove`. There is no Win32 accessor for
another process's cwd, so `_read_process_cwd` walks the PEB via
`NtQueryInformationProcess` + `ReadProcessMemory`. That reach is best-effort
by design and degrades to `None` (verdict `skipped:cwd-unknown`, no kill).

Vendorable in the same shape as `tests/e2e/_e2e_live_guard.py` and
`tests/e2e/_geometry.py`: the scope path is the only call-site argument, so a
byte-identical copy never forks. Stdlib only. Non-Windows platforms get an
honest `supported=False` result rather than a false "nothing to clean".
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

#: Image names swept. Deliberately WebKit-only plus WebKit's browser-main
#: process (Playwright ships it as `Playwright.exe`): those are the processes
#: #203 actually observed outliving runs, and they are unambiguously
#: Playwright's. Chromium is *not* listed — its helpers are plain `chrome.exe`
#: on this platform, and the fleet's shared-Chrome-profile convention forbids
#: killing anything that might be the user's own browser. Extend only with an
#: image name that cannot belong to a human's session.
HELPER_IMAGE_NAMES: frozenset[str] = frozenset(
    {
        "Playwright.exe",
        "WebKitNetworkProcess.exe",
        "WebKitWebProcess.exe",
        "WebKitGPUProcess.exe",
    }
)

VERDICT_KILLED = "killed"
VERDICT_KILL_FAILED = "kill-failed"
VERDICT_ZOMBIE = "zombie"
VERDICT_PARENT_ALIVE = "skipped:parent-alive"
VERDICT_OUT_OF_SCOPE = "skipped:out-of-scope"
VERDICT_CWD_UNKNOWN = "skipped:cwd-unknown"

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ = 0x0010
_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE = 0xFFFFFFFFFFFFFFFF
#: x64 offsets: PEB.ProcessParameters, then
#: RTL_USER_PROCESS_PARAMETERS.CurrentDirectory.DosPath (a UNICODE_STRING,
#: whose Buffer pointer sits 8 bytes past its Length/MaximumLength header).
_PEB_PROCESS_PARAMETERS_OFFSET = 0x20
_PARAMS_CURDIR_DOSPATH_OFFSET = 0x38
_UNICODE_STRING_BUFFER_OFFSET = 0x08


@dataclass(frozen=True)
class HelperProcess:
    """One browser helper process, with the facts the sweep decides on."""

    pid: int
    ppid: int
    name: str
    exited: bool
    """True when the OS reports a real exit code — an already-dead zombie object."""
    parent_alive: bool
    cwd: str | None
    """Working directory, or ``None`` when it could not be read (never assume)."""


@dataclass(frozen=True)
class SweepEntry:
    """A helper process plus what the sweep decided to do about it."""

    process: HelperProcess
    verdict: str

    def __str__(self) -> str:
        return (
            f"{self.process.name}#{self.process.pid} {self.verdict} "
            f"cwd={self.process.cwd or '<unreadable>'}"
        )


@dataclass(frozen=True)
class SweepResult:
    """Outcome of one sweep. ``supported=False`` means *unknown*, not clean."""

    supported: bool
    scope: str
    entries: tuple[SweepEntry, ...]

    def with_verdict(self, verdict: str) -> tuple[SweepEntry, ...]:
        return tuple(entry for entry in self.entries if entry.verdict == verdict)

    @property
    def killed(self) -> tuple[SweepEntry, ...]:
        return self.with_verdict(VERDICT_KILLED)

    @property
    def zombies(self) -> tuple[SweepEntry, ...]:
        return self.with_verdict(VERDICT_ZOMBIE)

    def summary(self) -> str:
        if not self.supported:
            return (
                f"[e2e] browser sweep unsupported on {sys.platform} - leaked "
                "helper state UNKNOWN, not verified clean"
            )
        if not self.entries:
            return f"[e2e] browser sweep: no helper processes seen (scope {self.scope})"
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry.verdict] = counts.get(entry.verdict, 0) + 1
        breakdown = ", ".join(f"{verdict}={n}" for verdict, n in sorted(counts.items()))
        return f"[e2e] browser sweep (scope {self.scope}): {breakdown}"


def path_is_within(candidate: str | None, scope: Path) -> bool:
    """True when *candidate* is *scope* itself or lives under it.

    Compares resolved paths so a junctioned worktree or an 8.3 short path does
    not read as out-of-scope. An unreadable/invalid path is False — never in
    scope by accident.
    """
    if not candidate:
        return False
    try:
        resolved = Path(candidate).resolve()
        scope_resolved = scope.resolve()
    except (OSError, ValueError):
        return False
    return resolved == scope_resolved or scope_resolved in resolved.parents


def classify(process: HelperProcess, scope: Path) -> str:
    """Decide what to do about one helper. Pure — the unit-tested core.

    Order matters: an already-exited zombie is reported first (nothing to
    kill, whatever its cwd says), then a live parent (a legitimate in-flight
    session), then an unreadable cwd (unknown, so hands off), and only a
    running + orphaned + in-scope process is nominated for the kill.
    """
    if process.exited:
        return VERDICT_ZOMBIE
    if process.parent_alive:
        return VERDICT_PARENT_ALIVE
    if process.cwd is None:
        return VERDICT_CWD_UNKNOWN
    if not path_is_within(process.cwd, scope):
        return VERDICT_OUT_OF_SCOPE
    return VERDICT_KILLED


def kill_process_tree(pid: int) -> bool:
    """Force-kill *pid* **and its descendants**. True when the tree is gone.

    `/T` is the whole point: a bare `Popen.kill()` reaches only the immediate
    process, so helpers it spawned in turn survive as fresh orphans. Mirrors
    `fleet-config`'s `claude_progress.py:_kill_process_tree()`.
    """
    if sys.platform != "win32":
        return False
    try:
        completed = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=10,
            creationflags=NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # 128 == "process not found": already gone, which is the desired end state.
    return completed.returncode in (0, 128)


# Everything below is Win32. Guarded at module level so the file stays
# importable on a POSIX host (where the sweep honestly reports `supported=False`)
# rather than exploding on `from ctypes import wintypes`.
if sys.platform == "win32":
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")

    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetExitCodeProcess.restype = wintypes.BOOL
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.GetProcessTimes.restype = wintypes.BOOL
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.ReadProcessMemory.restype = wintypes.BOOL

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = (
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        )


def _open_process(access: int, pid: int) -> int:
    handle = _k32.OpenProcess(access, False, pid)
    return int(handle) if handle else 0


def _exit_code(pid: int) -> int | None:
    """Raw exit code, or None when the process cannot be opened/queried."""
    handle = _open_process(_PROCESS_QUERY_LIMITED_INFORMATION, pid)
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not _k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return int(code.value)
    finally:
        _k32.CloseHandle(handle)


def _creation_time(pid: int) -> int | None:
    handle = _open_process(_PROCESS_QUERY_LIMITED_INFORMATION, pid)
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        ok = _k32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None
        return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
    finally:
        _k32.CloseHandle(handle)


def _parent_is_alive(pid: int, ppid: int) -> bool:
    """True only when *ppid* names a process that is still running *and* older.

    Windows recycles PIDs, so "a process with that id exists" is not enough —
    a newer process wearing the dead parent's id must read as *dead parent*,
    or a genuine orphan is silently skipped. Comparing creation times settles
    it; when either timestamp is unreadable, err towards *alive* (skip the
    kill) rather than killing on a guess.
    """
    if ppid <= 0:
        return False
    if _exit_code(ppid) != _STILL_ACTIVE:
        return False
    parent_created = _creation_time(ppid)
    child_created = _creation_time(pid)
    if parent_created is None or child_created is None:
        return True
    return parent_created <= child_created


def _read_bytes(handle: int, address: int, size: int) -> bytes | None:
    buffer = ctypes.create_string_buffer(size)
    read = ctypes.c_size_t()
    ok = _k32.ReadProcessMemory(
        wintypes.HANDLE(handle), ctypes.c_void_p(address), buffer, ctypes.c_size_t(size), ctypes.byref(read)
    )
    if not ok or read.value != size:
        return None
    return buffer.raw


def _read_pointer(handle: int, address: int) -> int:
    raw = _read_bytes(handle, address, ctypes.sizeof(ctypes.c_size_t))
    return 0 if raw is None else int.from_bytes(raw, "little")


def _read_u16(handle: int, address: int) -> int | None:
    raw = _read_bytes(handle, address, 2)
    return None if raw is None else int.from_bytes(raw, "little")


def _read_process_cwd(pid: int) -> str | None:
    """Read another process's current directory via its PEB. None on any failure.

    There is no Win32 accessor for this, so it walks
    `NtQueryInformationProcess(ProcessBasicInformation)` -> PEB ->
    `RTL_USER_PROCESS_PARAMETERS.CurrentDirectory.DosPath`. Deliberately
    best-effort: every failure path returns None so the caller reports
    "unknown" and keeps its hands off — the same graceful degradation as
    `_driver_pid()` in the teardown watchdog.
    """
    handle = _open_process(_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, pid)
    if not handle:
        return None
    try:
        # PROCESS_BASIC_INFORMATION is six pointer-sized fields on x64;
        # PebBaseAddress is the second. Read the block, then pick it out.
        basic = (ctypes.c_size_t * 6)()
        returned = ctypes.c_ulong()
        status = _ntdll.NtQueryInformationProcess(
            wintypes.HANDLE(handle),
            0,
            ctypes.byref(basic),
            ctypes.sizeof(basic),
            ctypes.byref(returned),
        )
        if status != 0:
            return None
        peb_address = int(basic[1])
        if not peb_address:
            return None
        params = _read_pointer(handle, peb_address + _PEB_PROCESS_PARAMETERS_OFFSET)
        if not params:
            return None
        dospath = params + _PARAMS_CURDIR_DOSPATH_OFFSET
        length = _read_u16(handle, dospath)
        if not length:
            return None
        buffer_address = _read_pointer(handle, dospath + _UNICODE_STRING_BUFFER_OFFSET)
        if not buffer_address:
            return None
        raw = _read_bytes(handle, buffer_address, length)
        if raw is None:
            return None
        return raw.decode("utf-16-le", errors="replace").rstrip("\\") or None
    finally:
        _k32.CloseHandle(handle)


def _iter_process_table() -> Iterable[tuple[int, int, str]]:
    """Yield (pid, ppid, image name) for every process, via Toolhelp32."""
    snapshot = _open_snapshot()
    if not snapshot:
        return
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not _k32.Process32FirstW(wintypes.HANDLE(snapshot), ctypes.byref(entry)):
            return
        while True:
            yield (
                int(entry.th32ProcessID),
                int(entry.th32ParentProcessID),
                str(entry.szExeFile),
            )
            if not _k32.Process32NextW(wintypes.HANDLE(snapshot), ctypes.byref(entry)):
                return
    finally:
        _k32.CloseHandle(snapshot)


def _open_snapshot() -> int:
    handle = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not handle or int(handle) == _INVALID_HANDLE:
        return 0
    return int(handle)


def enumerate_browser_helpers(
    image_names: frozenset[str] = HELPER_IMAGE_NAMES,
) -> list[HelperProcess]:
    """Every live-or-zombie browser helper process visible on this host."""
    if sys.platform != "win32":
        return []
    helpers: list[HelperProcess] = []
    for pid, ppid, name in _iter_process_table():
        if name not in image_names:
            continue
        code = _exit_code(pid)
        if code is None:
            # Cannot even open it. Report it as an unreadable already-gone
            # entry rather than nominating an un-inspectable process to kill.
            helpers.append(
                HelperProcess(
                    pid=pid, ppid=ppid, name=name, exited=True, parent_alive=False, cwd=None
                )
            )
            continue
        exited = code != _STILL_ACTIVE
        helpers.append(
            HelperProcess(
                pid=pid,
                ppid=ppid,
                name=name,
                exited=exited,
                parent_alive=False if exited else _parent_is_alive(pid, ppid),
                cwd=None if exited else _read_process_cwd(pid),
            )
        )
    return helpers


def sweep_browser_helpers(
    scope: Path,
    *,
    dry_run: bool = False,
    processes: Sequence[HelperProcess] | None = None,
) -> SweepResult:
    """Kill genuinely-orphaned browser helpers whose cwd is under *scope*.

    *scope* must be a directory only this run owns — the repo/worktree root
    the suite ran from. Pass *processes* to classify an already-captured
    table (tests, or a caller enumerating once for several scopes);
    *dry_run* classifies without killing anything.
    """
    if sys.platform != "win32" and processes is None:
        return SweepResult(supported=False, scope=str(scope), entries=())

    table = list(enumerate_browser_helpers()) if processes is None else list(processes)
    entries: list[SweepEntry] = []
    for process in table:
        verdict = classify(process, scope)
        if verdict == VERDICT_KILLED and not dry_run and not kill_process_tree(process.pid):
            verdict = VERDICT_KILL_FAILED
        entries.append(SweepEntry(process=process, verdict=verdict))
    return SweepResult(supported=True, scope=str(scope), entries=tuple(entries))


def main(argv: Sequence[str] | None = None) -> int:
    """Standalone entry point: sweep a repo/worktree path, print the verdicts.

    Worth running before `git worktree remove` — a leaked helper holding the
    worktree as its cwd is what makes the removal fail as "busy".

        python tests/e2e/_browser_sweep.py E:/automation/my-repo-wt-203 [--dry-run]
    """
    args = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in args
    paths = [arg for arg in args if not arg.startswith("--")]
    scope = Path(paths[0]) if paths else Path.cwd()
    result = sweep_browser_helpers(scope, dry_run=dry_run)
    print(result.summary())
    for entry in result.entries:
        print(f"  {entry}")
    return 0 if result.supported else 1


if __name__ == "__main__":
    raise SystemExit(main())
