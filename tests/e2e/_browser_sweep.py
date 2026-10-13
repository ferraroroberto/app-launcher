"""Post-run sweep for leaked Playwright browser helper processes (issue #203).

The companion to `docs/playwright-ui-testing.md`'s "Bounded WebKit driver
teardown" watchdog. That watchdog protects the *current* run's exit path from
a *pre-existing* wedge; it does nothing about browser **child** processes
(`WebKitNetworkProcess.exe` and siblings) that outlive the run. This module is
the missing half: after the session ends, look for helper processes that
belong to *this* checkout and are genuinely orphaned, and tree-kill them.

Three findings shape the design — read them before changing anything here:

1. **An exit code is not an exit. Classify before killing, but do not read
   "exited" off `GetExitCodeProcess` alone.** #203 measured every "orphan" on
   the fleet host reporting `GetExitCodeProcess` == 0 and concluded they had
   all exited cleanly. Windows *does* keep a genuinely-exited process's object
   (and its row in `tasklist` / `Win32_Process` / bulk `Get-Process`) alive
   until the last handle to it closes, so a real *zombie* looks exactly like a
   live orphan to a name-based scan: unkillable (`taskkill` and `Stop-Process`
   correctly answer "no such process"), holding no sockets, and harmless.
   That much held. What did not is the inverse inference — #236 measured 39
   such helpers on `home-automation`'s host (`home-automation#681`) where
   `GetExitCodeProcess` said `0` but `GetProcessTimes` reported **no exit
   time**, Toolhelp32 reported a **live thread**, `ReadProcessMemory` on the
   PEB **succeeded**, and the working directory was a real worktree path.
   Those are processes **wedged inside termination**: `ExitStatus` is set, so
   every Win32 accessor answers "gone", but a thread never finished dying and
   the **cwd handle is still held**. They are unkillable *and* still pinning a
   directory — the one state that makes `git worktree remove` fail as "busy",
   and precisely the state the old `exited -> VERDICT_ZOMBIE` shortcut
   swallowed before cwd was ever consulted. Six empty, undeletable worktrees
   accumulated over four days while the sweep reported `zombie=39` and exited
   green. So liveness is a **tri-state plus unknown** (`STATE_RUNNING` /
   `STATE_EXITED` / `STATE_WEDGED` / `STATE_UNKNOWN`, see `liveness`), a wedged
   helper gets its cwd read and its own verdict, and only a genuinely-exited
   one is a `zombie`.
2. **Never kill by name.** A kill needs three independent facts: the process
   is really running, its parent is dead (a *live* parent means a legitimate
   in-flight session — an agent's headed verification loop, a sibling job),
   and its working directory is under the scope path this run owns. Anything
   the sweep cannot establish gets its own verdict, never folded into
   "killed" or "clean" — the same rule as the fleet's shared-Chrome-profile
   and safe-restart conventions: never kill a live holder.
3. **Reporting a wedge is all this module can do about one — the fix is
   upstream.** Nothing can reap a process wedged mid-exit, so a sweep that
   *sees* one still cannot free the directory it pins. The actual remedy is to
   stop helpers rooting in the checkout at all: start the Playwright driver
   from a neutral cwd (`tests/e2e/conftest.py`'s `_neutral_driver_cwd`), and a
   wedged helper then pins `%TEMP%` instead of a worktree. For a pin already on
   disk, the cwd handle can be closed remotely
   (`NtDuplicateObject(..., DUPLICATE_CLOSE_SOURCE)`) — see
   `docs/playwright-ui-testing.md`; rebooting the host is *not* the remedy.

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

#: Liveness of a helper process. Deliberately four states, not a bool: the
#: whole point of #236 is that "Win32 reports an exit code" and "the process
#: is gone" are different claims, and folding them lost the only state that
#: actually pins a directory.
STATE_RUNNING = "running"
"""Still executing: `GetExitCodeProcess` returns `STILL_ACTIVE`."""
STATE_EXITED = "exited"
"""Really gone: an exit code *and* a recorded exit time. A harmless zombie object."""
STATE_WEDGED = "wedged"
"""Exit code set, no exit time — dying but not dead. Unkillable, still holds its cwd."""
STATE_UNKNOWN = "unknown"
"""Could not be established (no handle, unreadable times). Never assume either way."""

VERDICT_KILLED = "killed"
VERDICT_KILL_FAILED = "kill-failed"
VERDICT_WOULD_KILL = "would-kill"
"""Dry-run twin of `VERDICT_KILLED` — nominated for the kill, never attempted."""
VERDICT_ZOMBIE = "zombie"
VERDICT_WEDGED_PINNING = "wedged:pins-scope"
"""Wedged **and** rooted in this run's scope — the leak that blocks worktree removal."""
VERDICT_WEDGED_ELSEWHERE = "wedged:out-of-scope"
VERDICT_WEDGED_CWD_UNKNOWN = "wedged:cwd-unknown"
VERDICT_STATE_UNKNOWN = "skipped:state-unknown"
VERDICT_PARENT_ALIVE = "skipped:parent-alive"
VERDICT_OUT_OF_SCOPE = "skipped:out-of-scope"
VERDICT_CWD_UNKNOWN = "skipped:cwd-unknown"

#: Every verdict meaning "wedged inside termination", whatever its scope.
WEDGED_VERDICTS: frozenset[str] = frozenset(
    {VERDICT_WEDGED_PINNING, VERDICT_WEDGED_ELSEWHERE, VERDICT_WEDGED_CWD_UNKNOWN}
)

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
    state: str
    """One of `STATE_RUNNING` / `STATE_EXITED` / `STATE_WEDGED` / `STATE_UNKNOWN`."""
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
            f"state={self.process.state} cwd={self.process.cwd or '<unreadable>'}"
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
    def would_kill(self) -> tuple[SweepEntry, ...]:
        """Dry-run nominees: what a real run would have killed, untouched."""
        return self.with_verdict(VERDICT_WOULD_KILL)

    @property
    def zombies(self) -> tuple[SweepEntry, ...]:
        return self.with_verdict(VERDICT_ZOMBIE)

    @property
    def wedged(self) -> tuple[SweepEntry, ...]:
        """Helpers stuck inside termination — unkillable, and still holding a cwd."""
        return tuple(entry for entry in self.entries if entry.verdict in WEDGED_VERDICTS)

    @property
    def pinning_scope(self) -> tuple[SweepEntry, ...]:
        """The subset that pins *this* scope: why `git worktree remove` says "busy"."""
        return self.with_verdict(VERDICT_WEDGED_PINNING)

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
        line = f"[e2e] browser sweep (scope {self.scope}): {breakdown}"
        if self.pinning_scope:
            # Loud, because nothing here can fix it: a wedged helper cannot be
            # reaped, so this scope will refuse to delete until its cwd handle
            # is closed remotely (docs/playwright-ui-testing.md, #236).
            line += (
                f" -- {len(self.pinning_scope)} wedged helper(s) PIN this scope "
                "and cannot be killed; the directory will not delete until their "
                "cwd handle is closed (docs/playwright-ui-testing.md)"
            )
        return line


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


def liveness(exit_code: int | None, exit_time: int | None) -> str:
    """Turn the two OS probes into one honest liveness state (#236).

    `GetExitCodeProcess` alone cannot tell a finished process from one wedged
    inside termination: both report a real exit code. `GetProcessTimes`'s
    *exit* time is the discriminator — the kernel stamps it only once the
    process has actually finished dying, so an exit code with a zero exit time
    is a process that is still holding its address space and its cwd handle.

    *exit_time* is consulted only for an exit-code-bearing process (a running
    one has a zero exit time too, which is why order matters). Either probe
    coming back `None` means the fact was not established: `STATE_UNKNOWN`,
    never a guess in either direction.
    """
    if exit_code is None:
        return STATE_UNKNOWN
    if exit_code == _STILL_ACTIVE:
        return STATE_RUNNING
    if exit_time is None:
        return STATE_UNKNOWN
    return STATE_EXITED if exit_time > 0 else STATE_WEDGED


def classify(process: HelperProcess, scope: Path) -> str:
    """Decide what to do about one helper. Pure — the unit-tested core.

    Order matters. An unestablished state is reported first (hands off), then
    a genuinely-exited zombie (nothing to kill, whatever its cwd says), then a
    wedged helper — which *is* judged on its cwd, because a wedge rooted in
    this scope is exactly the leak that blocks the scope's deletion and the
    old code swallowed it as a zombie (#236). Only then the live cases: a live
    parent (a legitimate in-flight session), an unreadable cwd (unknown, so
    hands off), and finally a running + orphaned + in-scope process, the only
    one ever nominated for the kill.
    """
    if process.state == STATE_UNKNOWN:
        return VERDICT_STATE_UNKNOWN
    if process.state == STATE_EXITED:
        return VERDICT_ZOMBIE
    if process.state == STATE_WEDGED:
        # No kill is attempted either way — a wedged process is unkillable.
        # The cwd decides how loudly it is reported, not what is done to it.
        if process.cwd is None:
            return VERDICT_WEDGED_CWD_UNKNOWN
        if path_is_within(process.cwd, scope):
            return VERDICT_WEDGED_PINNING
        return VERDICT_WEDGED_ELSEWHERE
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


def _process_times(pid: int) -> tuple[int, int] | None:
    """`(creation, exit)` FILETIMEs as 64-bit ints, or None when unreadable.

    The *exit* half is what separates a finished process from one wedged
    inside termination: the kernel leaves it zero until the process has
    genuinely finished dying, however clean an exit code it already reports
    (#236). Both are returned together because they come from one call.
    """
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
        return (
            (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime),
            (int(exited.dwHighDateTime) << 32) | int(exited.dwLowDateTime),
        )
    finally:
        _k32.CloseHandle(handle)


def _creation_time(pid: int) -> int | None:
    times = _process_times(pid)
    return None if times is None else times[0]


def _exit_time(pid: int) -> int | None:
    """When the process finished dying, `0` if it has not. None when unreadable."""
    times = _process_times(pid)
    return None if times is None else times[1]


def _parent_is_alive(pid: int, ppid: int) -> bool:
    """True only when *ppid* names a process that is still running *and* older.

    Windows recycles PIDs, so "a process with that id exists" is not enough —
    a newer process wearing the dead parent's id must read as *dead parent*,
    or a genuine orphan is silently skipped. Comparing creation times settles
    it; when either timestamp is unreadable, err towards *alive* (skip the
    kill) rather than killing on a guess.

    A parent *wedged* inside termination (#236) reports an exit code and so
    reads as dead here — correctly: it is no longer an in-flight session, and
    the child it left behind is a genuine orphan.
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
    """Every running, wedged or zombie browser helper process on this host.

    The cwd is read for a **wedged** helper as well as a running one — that is
    the #236 fix. A wedged process still has an intact address space, so the
    PEB walk succeeds, and its cwd is the whole reason it matters; the old code
    passed `cwd=None` for anything the exit code called "exited" and so could
    not have seen the pin even in principle.
    """
    if sys.platform != "win32":
        return []
    helpers: list[HelperProcess] = []
    for pid, ppid, name in _iter_process_table():
        if name not in image_names:
            continue
        code = _exit_code(pid)
        # A running process has a zero exit time too, so only ask once the exit
        # code says the process claims to be finished.
        exit_time = None if code is None or code == _STILL_ACTIVE else _exit_time(pid)
        state = liveness(code, exit_time)
        helpers.append(
            HelperProcess(
                pid=pid,
                ppid=ppid,
                name=name,
                state=state,
                parent_alive=_parent_is_alive(pid, ppid) if state == STATE_RUNNING else False,
                cwd=(
                    _read_process_cwd(pid)
                    if state in (STATE_RUNNING, STATE_WEDGED)
                    else None
                ),
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
    *dry_run* classifies without killing anything — a kill-nominated helper
    is reported as `VERDICT_WOULD_KILL`, never `VERDICT_KILLED`, because
    nothing was actually touched.

    Only a `STATE_RUNNING` helper is ever killed. A wedged one is *reported*
    against the scope it pins and never touched — no signal can reap it
    (#236); see `SweepResult.pinning_scope`.
    """
    if sys.platform != "win32" and processes is None:
        return SweepResult(supported=False, scope=str(scope), entries=())

    table = list(enumerate_browser_helpers()) if processes is None else list(processes)
    entries: list[SweepEntry] = []
    for process in table:
        verdict = classify(process, scope)
        if verdict == VERDICT_KILLED:
            if dry_run:
                verdict = VERDICT_WOULD_KILL
            elif not kill_process_tree(process.pid):
                verdict = VERDICT_KILL_FAILED
        entries.append(SweepEntry(process=process, verdict=verdict))
    return SweepResult(supported=True, scope=str(scope), entries=tuple(entries))


def main(argv: Sequence[str] | None = None) -> int:
    """Standalone entry point: sweep a repo/worktree path, print the verdicts.

    Worth running before `git worktree remove` — a leaked helper holding the
    worktree as its cwd is what makes the removal fail as "busy".

        python tests/e2e/_browser_sweep.py E:/automation/my-repo-wt-203 [--dry-run]

    Exit codes: `0` nothing pins *scope*; `1` the sweep could not run at all
    (non-Windows — unknown, not clean) **or** a wedged helper pins *scope*, in
    which case the removal this is a preflight for will fail and no kill can
    help (#236). `--dry-run` classifies without killing.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in args
    paths = [arg for arg in args if not arg.startswith("--")]
    scope = Path(paths[0]) if paths else Path.cwd()
    result = sweep_browser_helpers(scope, dry_run=dry_run)
    print(result.summary())
    for entry in result.entries:
        print(f"  {entry}")
    return 0 if result.supported and not result.pinning_scope else 1


if __name__ == "__main__":
    raise SystemExit(main())
