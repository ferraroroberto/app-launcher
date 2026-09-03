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
# A tray on :8445 may be running or not — autoboot always picks a free port
# for its own webapp and spawns its own disposable session-host on a free
# port, never adopting the tray's live :8446 (issue #260). Exits non-zero on
# the first failure with the offending output left visible.

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$sw = [System.Diagnostics.Stopwatch]::StartNew()

# Persistent progress log (issue #534): phase markers land here from this
# script, per-test START/DONE lines from the pytest hook in tests/conftest.py
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

    # Diff-proportionate e2e routing (issue #568). Classify the branch's
    # changed files vs main and run an e2e slice proportionate to the diff:
    # static assets -> Chromium smoke only, real UI/behaviour -> the full
    # dual-projection suite, backend/docs-only -> no browser suite. Fail-safe:
    # any mixed/ambiguous/unrecognized diff runs the full suite. The path->tier
    # rules live in scripts/classify_e2e.py (one reviewable place). On CI the
    # full suite always runs -- the local gate is where routing is proven first.
    $tier = "full"; $e2eTarget = "tests/e2e"; $e2eBrowsers = ""; $routeReason = ""
    if ($env:CI -eq "true") {
        $routeReason = "CI always runs the full dual-projection suite"
    } else {
        $classifyOut = & $python (Join-Path $repoRoot "scripts\classify_e2e.py")
        $kv = @{}
        foreach ($line in $classifyOut) {
            if ($line -match '^(E2E_[A-Z_]+)=(.*)$') { $kv[$matches[1]] = $matches[2] }
        }
        if ($kv.ContainsKey("E2E_TIER") -and $kv["E2E_TIER"]) {
            $tier = $kv["E2E_TIER"]
            $e2eTarget = $kv["E2E_PYTEST_TARGET"]
            $e2eBrowsers = $kv["E2E_BROWSERS"]
            $routeReason = $kv["E2E_REASON"]
        } else {
            $routeReason = "classifier gave no verdict -- defaulting to full suite (fail-safe)"
        }
    }

    if ($tier -eq "skip") {
        Phase "e2e routing: SKIP browser suite (backend/docs-only diff)"
        Write-Host "    reason: $routeReason" -ForegroundColor DarkGray
        Log-Progress "e2e routing: tier=skip reason=$routeReason"
    } else {
        Phase "e2e routing: $tier ($routeReason)"
        Log-Progress "e2e routing: tier=$tier target=$e2eTarget browsers=$e2eBrowsers reason=$routeReason"

        # Serialize full-tier (dual-projection) e2e legs across concurrent
        # primary/worktree gate runs on this machine (issue #685). Two
        # overlapping full-tier runs -- a normal outcome of the fleet's own
        # claim-or-worktree concurrency model -- each spinning up ~400x2
        # browser contexts is the ephemeral-port burst fleet-config#498 traced
        # to this suite (Tcpip 4231, TIME_WAIT 206->979). A kernel-managed
        # named mutex, not a lockfile: auto-released if a holder crashes, no
        # stale-file cleanup path. Scoped to only this e2e leg -- byte-compile
        # and the non-e2e pytest phase above never wait on it -- and only to
        # the "full" tier; "static" (chromium-smoke-only) runs are cheap
        # enough not to need it.
        $e2eMutexName = "Global\AppLauncherFullE2EGate"
        $e2eMutex = $null
        $e2eMutexAcquired = $false
        if ($tier -eq "full") {
            $e2eMutex = New-Object System.Threading.Mutex($false, $e2eMutexName)
            $e2eMutexAcquired = $e2eMutex.WaitOne(0)
            if (-not $e2eMutexAcquired) {
                Phase "e2e gate: queued behind another full gate run (waiting on $e2eMutexName)..."
                Log-Progress "e2e gate: queued -- another verify-before-ship full e2e run holds $e2eMutexName"
                $e2eMutexAcquired = $e2eMutex.WaitOne([TimeSpan]::FromMinutes(30))
                if (-not $e2eMutexAcquired) {
                    $e2eMutex.Dispose()
                    Fail ("Timed out after 30 min waiting for another full e2e gate run to release " +
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
            $e2eArgs = @($e2eTarget, $verbosity)
            foreach ($b in ($e2eBrowsers -split ',' | Where-Object { $_ })) {
                $e2eArgs += @("--browser", $b)
            }
            $projLabel = if ($e2eBrowsers) { $e2eBrowsers } else { "Chromium + WebKit/iPhone" }
            Phase "pytest e2e ($e2eTarget, $projLabel, auto-booted)..."
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
    Write-Host "    See CLAUDE.md's restart section for the one supported way to restart :8446." -ForegroundColor Yellow
}
exit 0
