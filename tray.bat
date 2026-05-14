@echo off
chcp 65001 >nul
REM ============================================================================
REM  LAUNCHER TRAY - tray icon that owns the webapp lifecycle
REM ----------------------------------------------------------------------------
REM  Launch this on login (Startup folder) for always-on phone-first launcher.
REM ============================================================================

setlocal
set "SCRIPT_DIR=%~dp0"
set "VENV_PYW=%SCRIPT_DIR%.venv\Scripts\pythonw.exe"
set "VENV_PY=%SCRIPT_DIR%.venv\Scripts\python.exe"

cd /d "%SCRIPT_DIR%" || exit /b 1

REM Prefer pythonw.exe so no console window stays open.
if exist "%VENV_PYW%" (
    start "" "%VENV_PYW%" launcher.py tray
) else if exist "%VENV_PY%" (
    start "" "%VENV_PY%" launcher.py tray
) else (
    start "" pythonw launcher.py tray
)
exit /b 0
