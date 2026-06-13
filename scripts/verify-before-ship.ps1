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
# A tray on :8445 may be running or not — autoboot picks a free port for its
# own webapp and adopts the tray's session-host on :8446 if one is up. Exits
# non-zero on the first failure with the offending output left visible.

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$sw = [System.Diagnostics.Stopwatch]::StartNew()

function Fail($message) {
    Write-Host ""
    Write-Host "[X] $message" -ForegroundColor Red
    Write-Host ("Failed after {0:n1}s." -f $sw.Elapsed.TotalSeconds) -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $python)) {
    Fail ".venv missing -- run setup.bat first."
}

Push-Location $repoRoot
try {
    Write-Host "==> py_compile (app, src, tests)..." -ForegroundColor Cyan
    & $python -m compileall -q app src tests
    if ($LASTEXITCODE -ne 0) { Fail "byte-compile failed." }

    Write-Host "==> pytest (non-e2e)..." -ForegroundColor Cyan
    & $python -m pytest -q --ignore=tests/e2e
    if ($LASTEXITCODE -ne 0) { Fail "non-e2e pytest suite failed." }

    Write-Host "==> pytest e2e (Chromium + WebKit/iPhone, auto-booted)..." -ForegroundColor Cyan
    $env:LAUNCHER_E2E_AUTOBOOT = "1"
    # On CI run verbose + unbuffered so a hung test (pytest-timeout aborts the
    # process via os._exit, skipping the summary) is named by the last nodeid
    # logged at test start. Locally keep the compact dotted output (#184).
    if ($env:CI -eq "true") {
        $env:PYTHONUNBUFFERED = "1"
        $e2eArgs = @("tests/e2e", "-v")
    } else {
        $e2eArgs = @("tests/e2e", "-q")
    }
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
    Pop-Location
}

$sw.Stop()
Write-Host ""
Write-Host ("[OK] Ready to ship -- all checks passed in {0:n1}s." -f $sw.Elapsed.TotalSeconds) -ForegroundColor Green
exit 0
