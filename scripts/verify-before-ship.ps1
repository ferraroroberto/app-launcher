# Pre-ship verification gate (issue #33, Phone-validation 4/5).
#
# Runs the full validation pipeline locally before a change is declared
# "done": byte-compile, the non-e2e pytest suite, then the Playwright e2e
# suite (Chromium + WebKit/iPhone projections) against a disposable webapp
# the script boots itself on a free port.
#
# Usage:
#   pwsh -File scripts/verify-before-ship.ps1
#   powershell -File scripts\verify-before-ship.ps1   # Windows PowerShell 5.1 works too
#
# A tray on :8445 may be running or not -- autoboot always picks a free port
# for its own webapp and spawns its own disposable session-host on a free
# port, never adopting the tray's live :8446 (issue #260). Exits non-zero on
# the first failure with the offending output left visible.
#
# Never pipe this script when its exit code matters (issue #1086) -- a
# pipeline's status is its last command's, so `... | tail -40` reports 0 for a
# failed gate. Redirect and read the file:
#   pwsh -File scripts/verify-before-ship.ps1 > gate.log 2>&1; echo "exit=$?"
# A caller that cannot avoid a pipe asserts on the final `GATE-RESULT: PASS`
# / `GATE-RESULT: FAIL` line, which a pipe cannot rewrite.

$ErrorActionPreference = "Stop"

# Machine-greppable verdict, emitted as the last line of every path this
# script can still run code on (issue #1086). A pipeline's status is its
# *last* command's status, so `verify-before-ship.ps1 | tail -40` hands the
# caller tail's 0 and the genuine failure disappears -- a verification gate
# reporting green on a red run. A caller that cannot avoid a pipe asserts on
# this line instead of `$?`.
#
# Defined first, and paired with the trap below, because the paths that most
# need it are the early ones: a terminating error under the "Stop" preference
# above (a read-only progress-log directory, a missing helper, an unusable
# interpreter path) printed its error to *stderr* and exited 1, so a caller
# piping stdout saw an empty, entirely silent, apparently-successful run --
# zero lines and $? of 0, worse than the failure this issue was filed for.
#
# Its absence still means something, for what is left: the script was killed,
# an outer timeout fired, or PowerShell could not parse the file at all. None
# of those is a pass either.
function Write-GateResult($verdict) {
    Write-Host "GATE-RESULT: $verdict"
}

# Any terminating error anywhere below lands here: report it on *stdout* where
# a piped caller can see it, then emit the verdict and keep the exit code the
# script already had (a terminating error under -File exits 1). Deliberately
# defensive -- it runs before $progressLog and the logging helpers exist, so
# the progress-log write is best-effort and never masks the original error.
trap {
    Write-Host ""
    Write-Host "[X] the gate aborted on an unhandled error:" -ForegroundColor Red
    Write-Host ("    {0}" -f $_.Exception.Message) -ForegroundColor Red
    if ($_.InvocationInfo) {
        Write-Host ("    at {0}:{1}" -f $_.InvocationInfo.ScriptName,
                                        $_.InvocationInfo.ScriptLineNumber) -ForegroundColor Red
    }
    try { Log-Progress ("ABORTED: {0}" -f $_.Exception.Message) } catch { }
    try { Remove-Item Env:\LAUNCHER_VERIFY_PROGRESS_LOG -ErrorAction SilentlyContinue } catch { }
    Write-GateResult "FAIL"
    exit 1
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$sw = [System.Diagnostics.Stopwatch]::StartNew()

# Persistent progress log (issue #534): phase markers land here from this
# script, per-test START/DONE lines from the pytest hook in tests/_progress_log.py
# (via LAUNCHER_VERIFY_PROGRESS_LOG). If an outer timeout kills the gate, the
# last lines name the active phase + node id and the per-test timings survive.
# Gitignored via the blanket *.log rule; overwritten each run.
$progressLog = Join-Path $repoRoot "webapp\verify-progress.log"
[void][System.IO.Directory]::CreateDirectory((Split-Path -Parent $progressLog))
Set-Content -Path $progressLog -Encoding UTF8 -Value (
    "verify-before-ship run started {0}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
)
$env:LAUNCHER_VERIFY_PROGRESS_LOG = $progressLog

function Log-Progress($message) {
    Add-Content -Path $progressLog -Encoding UTF8 -Value (
        "[{0} +{1,7:n1}s] {2}" -f (Get-Date -Format "HH:mm:ss"), $sw.Elapsed.TotalSeconds, $message
    )
}

function Phase($message) {
    Write-Host "==> $message" -ForegroundColor Cyan
    Log-Progress "==> phase: $message"
}

function Fail($message) {
    Write-Host ""
    Write-Host "[X] $message" -ForegroundColor Red
    Write-Host ("Failed after {0:n1}s." -f $sw.Elapsed.TotalSeconds) -ForegroundColor Red
    Log-Progress ("FAILED: {0} (after {1:n1}s)" -f $message, $sw.Elapsed.TotalSeconds)
    Remove-Item Env:\LAUNCHER_VERIFY_PROGRESS_LOG -ErrorAction SilentlyContinue
    Write-GateResult "FAIL"
    exit 1
}

if (-not (Test-Path $python)) {
    Fail ".venv missing -- run setup.bat first."
}

Push-Location $repoRoot
try {
    Phase "py_compile (app, src, tests)..."
    & $python -m compileall -q app src tests
    if ($LASTEXITCODE -ne 0) { Fail "byte-compile failed." }

    Phase "pytest (non-e2e)..."
    & $python -m pytest -q --ignore=tests/e2e
    if ($LASTEXITCODE -ne 0) { Fail "non-e2e pytest suite failed." }

    # Diff-proportionate e2e routing (issues #568, #955). Classify the branch's
    # changed files vs main and run an e2e slice proportionate to the diff:
    # static assets -> Chromium smoke only, real UI/behaviour -> the full
    # dual-projection suite, or only one declared surface's tests when every
    # full-tier path sits in that surface, backend/docs-only -> no browser
    # suite. Fail-safe: any mixed/ambiguous/unrecognized diff runs the full
    # suite. The path->tier rules and surfaces live in .fleet.toml [e2e];
    # scripts/classify_e2e.py is the scaffold's mechanism, and
    # scripts/e2e-gate-route.ps1 turns its verdict into pytest arguments. On CI
    # the browser suite is skipped entirely (#1041) -- that job runs the
    # clean-machine half, and this gate, here, is the contract for the rest.
    . (Join-Path $PSScriptRoot "e2e-gate-route.ps1")
    $classifyOut = @()
    if ($env:CI -ne "true") {
        $classifyOut = & $python (Join-Path $repoRoot "scripts\classify_e2e.py")
    }
    $route = Get-E2ERoute -ClassifierOutput $classifyOut -IsCI ($env:CI -eq "true")
    $tier = $route.Tier
    $routeReason = $route.Reason
    $e2eTargetLabel = @($route.Targets) -join " "
    $e2eBrowsers = @($route.Browsers) -join ","

    if ($tier -eq "skip") {
        Phase "e2e routing: SKIP browser suite"
        Write-Host "    reason: $routeReason" -ForegroundColor DarkGray
        Log-Progress "e2e routing: tier=skip reason=$routeReason"
    } else {
        Phase "e2e routing: $tier ($routeReason)"
        Log-Progress "e2e routing: tier=$tier target=$e2eTargetLabel browsers=$e2eBrowsers reason=$routeReason"

        # Serialize dual-projection e2e legs across concurrent primary/worktree
        # gate runs on this machine (issue #685). Two overlapping runs -- a
        # normal outcome of the fleet's own claim-or-worktree concurrency
        # model -- each spinning up hundreds of browser contexts is the
        # ephemeral-port burst fleet-config#498 traced to this suite (Tcpip
        # 4231, TIME_WAIT 206->979). A kernel-managed named mutex, not a
        # lockfile: auto-released if a holder crashes, no stale-file cleanup
        # path. Scoped to only this e2e leg -- byte-compile and the non-e2e
        # pytest phase above never wait on it -- and taken by the "full" and
        # "surface" tiers, which both run both projections (#955); "static"
        # (chromium-smoke-only) runs are cheap enough not to need it.
        $e2eMutexName = "Global\AppLauncherFullE2EGate"
        $e2eMutex = $null
        $e2eMutexAcquired = $false
        if ($route.Serialize) {
            $e2eMutex = New-Object System.Threading.Mutex($false, $e2eMutexName)
            $e2eMutexAcquired = $e2eMutex.WaitOne(0)
            if (-not $e2eMutexAcquired) {
                Phase "e2e gate: queued behind another dual-projection gate run (waiting on $e2eMutexName)..."
                Log-Progress "e2e gate: queued -- another verify-before-ship dual-projection e2e run holds $e2eMutexName"
                $e2eMutexAcquired = $e2eMutex.WaitOne([TimeSpan]::FromMinutes(30))
                if (-not $e2eMutexAcquired) {
                    $e2eMutex.Dispose()
                    Fail ("Timed out after 30 min waiting for another dual-projection e2e gate run to release " +
                          "$e2eMutexName -- check for a stuck/hung gate elsewhere on this machine (issue #685).")
                }
                Log-Progress "e2e gate: acquired $e2eMutexName after queueing"
            }
        }

        try {
            $env:LAUNCHER_E2E_AUTOBOOT = "1"
            # On CI run verbose + unbuffered so a hung test (pytest-timeout aborts
            # the process via os._exit, skipping the summary) is named by the last
            # nodeid logged at test start. Locally keep the compact dotted output
            # (#184).
            $verbosity = if ($env:CI -eq "true") { "-v" } else { "-q" }
            if ($env:CI -eq "true") { $env:PYTHONUNBUFFERED = "1" }
            $e2eArgs = @($route.Targets) + @($verbosity)
            foreach ($b in @($route.Browsers)) {
                $e2eArgs += @("--browser", $b)
            }
            $projLabel = if ($e2eBrowsers) { $e2eBrowsers } else { "Chromium + WebKit/iPhone" }
            Phase "pytest e2e ($e2eTargetLabel, $projLabel, auto-booted)..."
            try {
                & $python -m pytest @e2eArgs
                $e2eExit = $LASTEXITCODE
            }
            finally {
                Remove-Item Env:\LAUNCHER_E2E_AUTOBOOT -ErrorAction SilentlyContinue
                if ($env:CI -eq "true") { Remove-Item Env:\PYTHONUNBUFFERED -ErrorAction SilentlyContinue }
            }
            if ($e2eExit -ne 0) { Fail "Playwright e2e suite failed." }
        }
        finally {
            if ($e2eMutex) {
                if ($e2eMutexAcquired) { $e2eMutex.ReleaseMutex() }
                $e2eMutex.Dispose()
            }
        }
    }
}
finally {
    Pop-Location
}

$sw.Stop()
Log-Progress ("OK: all checks passed in {0:n1}s" -f $sw.Elapsed.TotalSeconds)
Remove-Item Env:\LAUNCHER_VERIFY_PROGRESS_LOG -ErrorAction SilentlyContinue
Write-Host ""
Write-Host ("[OK] Ready to ship -- all checks passed in {0:n1}s." -f $sw.Elapsed.TotalSeconds) -ForegroundColor Green

# A green gate + merge + tray.bat --restart still does not make a
# session-host-path change live -- that process is deliberately excluded
# from the reclaim sweep (project-scaffolding#35) and can run stale code for
# days with nothing else surfacing that (issue #615, demonstrated live on
# #611). Reuse classify_e2e.py's own path categorization (the "session-host"
# / "session-host/launcher" label it already computes for e2e routing) so
# this warning never drifts out of sync with that classifier's path list.
if ($routeReason -match "session-host") {
    Write-Host ""
    Write-Host "[!] SESSION-HOST PATHS TOUCHED -- this is NOT live after tray.bat --restart." -ForegroundColor Yellow
    Write-Host "    Check GET /api/version's session_host.stale_relevant field before reporting" -ForegroundColor Yellow
    Write-Host "    this change as shipped -- if true, report it as merged but not yet live." -ForegroundColor Yellow
    Write-Host "    (Raw session_host.stale is true after ANY merge, not just a session-host" -ForegroundColor Yellow
    Write-Host "    one -- #635; null in either field means unknown, never a confident false.)" -ForegroundColor Yellow
    Write-Host "    See CLAUDE.md's session-host block for the one supported way to restart :8446." -ForegroundColor Yellow
}
Write-GateResult "PASS"
exit 0
