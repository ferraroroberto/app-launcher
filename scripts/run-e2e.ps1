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
$env:LAUNCHER_E2E_LIVE = "1"

& $python -m pytest -m smoke -v tests/e2e @args
exit $LASTEXITCODE
