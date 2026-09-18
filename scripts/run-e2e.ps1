# Run the Playwright smoke suite against a live tray on https://127.0.0.1:8445.
# Tray must be started separately (tray.bat); this script does not boot it.
#
# Usage:
#   .\scripts\run-e2e.ps1            # run the smoke suite
#   .\scripts\run-e2e.ps1 --headed   # forward any extra args to pytest
#
# If the tray isn't up, conftest.py skips the suite with a clear message.

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "[X] .venv missing -- run setup.bat first." -ForegroundColor Red
    exit 1
}

# Explicit live-tray opt-in (issue #386): this script IS the deliberate
# dev-loop entry point for e2e-against-the-live-tray, so it sets the flag a
# bare `pytest tests/e2e` refuses to run without.
#
# Scoped to this one run (issue #1007). $env: writes go to the *calling*
# console's process environment, so leaving the flag set meant every later
# bare `pytest tests/e2e` in that same window silently drove the live :8445
# tray the phone is using -- precisely the accident #386's guard exists to
# stop. Same set-then-finally shape as verify-before-ship.ps1 uses for
# LAUNCHER_E2E_AUTOBOOT.
$exitCode = 1
try {
    $env:LAUNCHER_E2E_LIVE = "1"
    & $python -m pytest -m smoke -v tests/e2e @args
    $exitCode = $LASTEXITCODE
}
finally {
    Remove-Item Env:\LAUNCHER_E2E_LIVE -ErrorAction SilentlyContinue
}
exit $exitCode
