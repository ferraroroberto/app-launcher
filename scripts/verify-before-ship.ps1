# Pre-ship verification gate (Phone-validation 4/5).
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
# port, never adopting the tray's live :8446. Exits non-zero on
# the first failure with the offending output left visible.
#
# Never pipe this script when its exit code matters -- a
# pipeline's status is its last command's, so `... | tail -40` reports 0 for a
# failed gate. Redirect and read the file:
#   pwsh -File scripts/verify-before-ship.ps1 > gate.log 2>&1; echo "exit=$?"
# A caller that cannot avoid a pipe asserts on the final `GATE-RESULT: PASS`
# / `GATE-RESULT: FAIL` line, which a pipe cannot rewrite.

$ErrorActionPreference = "Stop"

# Machine-greppable verdict, emitted as the last line of every path this
# script can still run code on. A pipeline's status is its
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

# Persistent progress log: phase markers land here from this
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

# Append this run to the durable history in the primary checkout. The
# caller holds the e2e gate mutex, which is what serialises two gates ending at
# once in sibling worktrees. A history that could not be written is reported
# loudly but never fails a gate whose tests passed.
function Publish-ProgressHistory {
    try {
        $historyLog = Get-ProgressHistoryPath -RepoRoot $repoRoot
        if (-not $historyLog) { throw "git could not name the primary checkout" }
        $run = [System.IO.File]::ReadAllText($progressLog)
        $old = if (Test-Path $historyLog) { [System.IO.File]::ReadAllText($historyLog) } else { "" }
        [void][System.IO.Directory]::CreateDirectory((Split-Path -Parent $historyLog))
        [System.IO.File]::WriteAllText($historyLog, (Merge-ProgressHistory -History $old -Run $run),
                                       (New-Object System.Text.UTF8Encoding($false)))
        Write-Host "    e2e run recorded in $historyLog" -ForegroundColor DarkGray
    }
    catch {
        Write-Host ("[!] e2e run history NOT recorded: {0}" -f $_.Exception.Message) -ForegroundColor Yellow
    }
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

# Every ratchet stage reads the whole tree, so none is path-routed. Exit 3 means
# the count could not be established, which is its own failure and never a pass;
# one handler owns that rule so a stage added to the table cannot get it wrong.
function Invoke-RatchetStage($stage) {
    Phase "$($stage.Name) ratchet..."
    & $python @($stage.Argv)
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 3) { Fail "$($stage.Name) ratchet NOT CONFIRMED ($($stage.NotConfirmed))." }
    if ($exitCode -ne 0) { Fail "$($stage.Name) ratchet failed (exit $exitCode): $($stage.Failed)" }
}

# ratchet-stages:begin
$ratchetStages = @(
    @{ Name = "comment"; Argv = @("scripts/comment_ratchet.py")
       NotConfirmed = "a file could not be scanned or git was unusable"
       Failed = "a file has more #NNN comment references than .comment-ratchet-baseline.json." }
    # tests/ lines over src/ + app/ lines may not rise above .fleet.toml [test_weight] ratio_ceiling.
    @{ Name = "suite weight"; Argv = @("-m", "scripts.suite_weight_ratchet")
       NotConfirmed = "git was unusable or a file could not be read"
       Failed = "the test-to-code ratio is above .fleet.toml [test_weight] ratio_ceiling, or the table is malformed." }
    # Duplicate blocks (jscpd via npx) and unused imports/variables (ruff).
    @{ Name = "static"; Argv = @("scripts/static_ratchet.py")
       NotConfirmed = "a detector could not run: Node/npx or ruff missing, or output unparseable"
       Failed = "a new duplicate block or unused import/variable, or a baseline problem; see .static-ratchet-baseline.json." }
    # README size and history markers (issue references, dated and changelog headings).
    @{ Name = "docs"; Argv = @("scripts/docs_ratchet.py")
       NotConfirmed = "git was unusable, or a doc is not UTF-8 or has a fence/comment that never closes"
       Failed = "README.md grew past its size budget, or a doc gained an issue reference or dated/changelog heading; see .docs-ratchet-baseline.json." }
    # File size, function length, flat directories, the src -> app layer contract and the root layout.
    @{ Name = "structure"; Argv = @("scripts/structure_ratchet.py")
       NotConfirmed = "git was unusable, a file could not be read or parsed, or the declared shape is unknown"
       Failed = "a new or grown file, function or directory, a src -> app import, or a root folder the shape does not list; see docs/layout.md and .structure-ratchet-baseline.json." }
    # Lint rules over CLAUDE.md, AGENTS.md and every SKILL.md: AGENTS.md decides how CLAUDE.md is read.
    @{ Name = "instruction"; Argv = @("scripts/instruction_ratchet.py")
       NotConfirmed = "git was unusable, or an instruction file is not UTF-8, has a fence that never closes, or a skill description that cannot be read"
       Failed = "an instruction file gained a lint hit or passed its size cap beyond .instruction-ratchet-baseline.json; see the named rule and lines above." }
    # A code change can strand a doc reference without touching the doc.
    @{ Name = "doc refs"; Argv = @("scripts/doc_refs_ratchet.py")
       NotConfirmed = "git was unusable, or a doc is not UTF-8 or has a fence or HTML comment that never closes"
       Failed = "a doc names a repo path, module, link or port that does not exist beyond .doc-refs-ratchet-baseline.json; see the named reference above." }
    # Deleting the last caller in one file strands a definition in another.
    @{ Name = "dead code"; Argv = @("scripts/dead_code_ratchet.py")
       NotConfirmed = "git was unusable, or a tracked .py file cannot be read or does not parse"
       Failed = "a function, class or method nothing refers to is not in .dead-code-ratchet-baseline.json; see the named definition above." }
    # A rule added to .fleet.toml must see every file.
    @{ Name = "conventions"; Argv = @("scripts/conventions_ratchet.py")
       NotConfirmed = "git was unusable, or a tracked .py file cannot be read or does not parse"
       Failed = "a file's count for a convention rule rose above .conventions-ratchet-baseline.json; see the named rule and lines above." }
)
# ratchet-stages:end

if (-not (Test-Path $python)) {
    Fail ".venv missing -- run setup.bat first."
}

Push-Location $repoRoot

. (Join-Path $PSScriptRoot "box_lock_gate.ps1"); Enter-BoxLockGate
try {
    Phase "py_compile (app, src, tests)..."
    & $python -m compileall -q app src tests
    if ($LASTEXITCODE -ne 0) { Fail "byte-compile failed." }

    foreach ($stage in $ratchetStages) { Invoke-RatchetStage $stage }

    # Worker counts for both pytest phases live in e2e-gate-route.ps1;
    # a bad override fails here, before any phase runs.
    . (Join-Path $PSScriptRoot "e2e-gate-route.ps1")
    try {
        $unitPlan = Get-UnitWorkerArgs -Setting $env:UNIT_WORKERS
    }
    catch {
        Fail $_.Exception.Message
    }
    $unitLabel = if ($unitPlan.Workers -gt 1) { "$($unitPlan.Workers) workers" } else { "serial" }
    Phase "pytest (non-e2e, $unitLabel)..."
    & $python -m pytest -q --ignore=tests/e2e @($unitPlan.Args)
    if ($LASTEXITCODE -ne 0) { Fail "non-e2e pytest suite failed." }

    # Diff-proportionate e2e routing. Classify the branch's
    # changed files vs main and run an e2e slice proportionate to the diff:
    # static assets -> Chromium smoke only, real UI/behaviour -> the full
    # dual-projection suite, or only one declared surface's tests when every
    # full-tier path sits in that surface, backend/docs-only -> no browser
    # suite. Fail-safe: any mixed/ambiguous/unrecognized diff runs the full
    # suite. The path->tier rules and surfaces live in .fleet.toml [e2e];
    # scripts/classify_e2e.py is the scaffold's mechanism, and
    # scripts/e2e-gate-route.ps1 turns its verdict into pytest arguments. On CI
    # the browser suite is skipped entirely -- that job runs the
    # clean-machine half, and this gate, here, is the contract for the rest.
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
        Write-Host "    shot baseline skipped too: no screen was recaptured or compared" -ForegroundColor DarkGray
    } else {
        # Parallel workers for the dual-projection tiers: the one
        # setting is $E2EDefaultWorkers in e2e-gate-route.ps1, overridable
        # with E2E_WORKERS. Resolved before the mutex so a bad value fails
        # fast instead of after queueing.
        try {
            $workerPlan = Get-E2EWorkerArgs -Route $route -Setting $env:E2E_WORKERS
        }
        catch {
            Fail $_.Exception.Message
        }
        Phase "e2e routing: $tier ($routeReason)"
        Log-Progress "e2e routing: tier=$tier target=$e2eTargetLabel browsers=$e2eBrowsers workers=$($workerPlan.Workers) reason=$routeReason"

        # Serialize dual-projection e2e legs across concurrent primary/worktree
        # gate runs on this machine. Two overlapping runs -- a
        # normal outcome of the fleet's own claim-or-worktree concurrency
        # model -- each spinning up hundreds of browser contexts is the
        # ephemeral-port burst fleet-config traced to this suite (Tcpip
        # 4231, TIME_WAIT 206->979). A kernel-managed named mutex, not a
        # lockfile: auto-released if a holder crashes, no stale-file cleanup
        # path. Scoped to only this e2e leg -- byte-compile and the non-e2e
        # pytest phase above never wait on it -- and taken by the "full" and
        # "surface" tiers, which both run both projections; "static"
        # (chromium-smoke-only) runs are cheap enough not to need it.
        $e2eMutexName = "Global\AppLauncherFullE2EGate"
        $e2eMutex = $null
        $e2eMutexAcquired = $false
        if ($route.Serialize) {
            $e2eMutex = New-Object System.Threading.Mutex($false, $e2eMutexName)
            # A holder that died without releasing hands the mutex to the next
            # waiter as AbandonedMutexException; Wait-E2EGateMutex turns that
            # into "acquired" so it is logged, not an aborted gate.
            $e2eWait = Wait-E2EGateMutex -Mutex $e2eMutex -Timeout ([TimeSpan]::Zero)
            $e2eMutexAcquired = $e2eWait.Acquired
            if (-not $e2eMutexAcquired) {
                Phase "e2e gate: queued behind another dual-projection gate run (waiting on $e2eMutexName)..."
                Log-Progress "e2e gate: queued -- another verify-before-ship dual-projection e2e run holds $e2eMutexName"
                $e2eWait = Wait-E2EGateMutex -Mutex $e2eMutex -Timeout ([TimeSpan]::FromMinutes(30))
                $e2eMutexAcquired = $e2eWait.Acquired
                if (-not $e2eMutexAcquired) {
                    $e2eMutex.Dispose()
                    Fail ("Timed out after 30 min waiting for another dual-projection e2e gate run to release " +
                          "$e2eMutexName -- check for a stuck/hung gate elsewhere on this machine (issue #685).")
                }
                if (-not $e2eWait.Abandoned) { Log-Progress "e2e gate: acquired $e2eMutexName after queueing" }
            }
            if ($e2eWait.Abandoned) {
                Phase "e2e gate: the previous holder of $e2eMutexName ended without releasing it; taking over (#1682)."
                Log-Progress "e2e gate: abandoned mutex -- previous holder of $e2eMutexName exited without releasing it; acquired it"
            }
        }

        try {
            $env:LAUNCHER_E2E_AUTOBOOT = "1"
            # On CI run verbose + unbuffered so a hung test (pytest-timeout aborts
            # the process via os._exit, skipping the summary) is named by the last
            # nodeid logged at test start. Locally keep the compact dotted output.
            $verbosity = if ($env:CI -eq "true") { "-v" } else { "-q" }
            if ($env:CI -eq "true") { $env:PYTHONUNBUFFERED = "1" }
            $e2eBase = @($route.Targets) + @($verbosity)
            $browserArgs = @()
            foreach ($b in @($route.Browsers)) {
                $browserArgs += @("--browser", $b)
            }
            $e2eArgs = $e2eBase + @($workerPlan.Args) + $browserArgs
            # Taken before the run: a shot written after it was recaptured by it.
            $shotSince = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
            $projLabel = if ($e2eBrowsers) { $e2eBrowsers } else { "Chromium + WebKit/iPhone" }
            $workerLabel = if ($workerPlan.Workers -gt 1) { "$($workerPlan.Workers) workers" } else { "serial" }
            Phase "pytest e2e ($e2eTargetLabel, $projLabel, $workerLabel, auto-booted)..."
            $e2eExit = $null
            $serialExit = $null
            try {
                & $python -m pytest @e2eArgs
                $e2eExit = $LASTEXITCODE
                # The load-sensitive `serial` tests, once the workers are gone.
                # Runs even after a red parallel pass, so one gate
                # run reports both.
                if (@($workerPlan.SerialArgs).Count -gt 0) {
                    Phase "pytest e2e serial pass (tests marked serial, after the workers)..."
                    Log-Progress "e2e serial pass: tests marked serial"
                    & $python -m pytest @($e2eBase + @($workerPlan.SerialArgs) + $browserArgs)
                    $serialExit = $LASTEXITCODE
                }
            }
            finally {
                Remove-Item Env:\LAUNCHER_E2E_AUTOBOOT -ErrorAction SilentlyContinue
                if ($env:CI -eq "true") { Remove-Item Env:\PYTHONUNBUFFERED -ErrorAction SilentlyContinue }
                # Only a run that took the mutex is recorded: the history is
                # written under it, and the static smoke slice is not a timing
                # source. Red and green alike, so /e2e-audit reads both.
                if ($e2eMutexAcquired) {
                    Log-Progress ("e2e browser phase ended: pytest exit {0}, serial pass exit {1}, checkout {2}" -f
                        $e2eExit, $serialExit, (Split-Path -Leaf $repoRoot))
                    Publish-ProgressHistory
                }
            }
            if ($null -ne $serialExit) {
                # Exit 5 is "no tests collected": a narrowed target can hold no
                # serial test, or nothing but. One of the two passes must run.
                if ($e2eExit -eq 5 -and $serialExit -eq 5) { Fail "Playwright e2e: neither pass collected a test." }
                if ($e2eExit -eq 5) { $e2eExit = 0 }
                if ($serialExit -eq 5) { $serialExit = 0 }
                if ($serialExit -ne 0) { Fail "Playwright e2e serial pass failed." }
            }
            if ($e2eExit -ne 0) { Fail "Playwright e2e suite failed." }

            # Screenshot baseline (scaffold docs/playwright-ui-testing.md): the
            # last stage. A slice that ran the whole suite or the capture test just
            # recaptured the shots, so only compare; a narrower slice did not, so the stage
            # captures them itself. Either way the tree is put back. Exit 3 is
            # NOT CONFIRMED (no baseline, nothing recaptured, capture failed):
            # a failure, never a pass.
            $env:LAUNCHER_E2E_AUTOBOOT = "1"
            try {
                if (@($route.Targets | Where-Object { $_ -eq "tests/e2e" -or $_ -eq "tests/e2e/screens/test_screen_shots.py" }).Count -gt 0) {
                    $since = $shotSince.ToString([Globalization.CultureInfo]::InvariantCulture)
                    Phase "shot baseline (compare)..."
                    & $python scripts/shot_baseline.py --check-tree --since $since
                } else {
                    Phase "shot baseline (capture + compare)..."
                    & $python scripts/shot_baseline.py --baseline
                }
                $shotExit = $LASTEXITCODE
            }
            finally {
                Remove-Item Env:\LAUNCHER_E2E_AUTOBOOT -ErrorAction SilentlyContinue
            }
            if ($shotExit -eq 3) { Fail "shot baseline NOT CONFIRMED (no baseline, no shot recaptured, an unreadable image, or the capture failed)." }
            if ($shotExit -ne 0) { Fail "shot baseline failed (exit $shotExit): a screen drifted from tests/e2e/shots. A deliberate UI change re-baselines (CLAUDE.md); anything else is unpinned content." }
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
# from the reclaim sweep and can run stale code for
# days with nothing else surfacing that (demonstrated live once). Reuse
# classify_e2e.py's own path categorization (the "session-host"
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
