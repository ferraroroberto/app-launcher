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

& $python -m pytest -m smoke -v tests/e2e @args
exit $LASTEXITCODE
