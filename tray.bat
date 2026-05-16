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
REM Window title differentiates this tray from sister apps' trays so
REM `taskkill /FI "WINDOWTITLE eq AppLauncher Tray"` can target it
REM selectively. The same trick is in voice-transcriber and photo-ocr.
if exist "%VENV_PYW%" (
    start "AppLauncher Tray" "%VENV_PYW%" launcher.py tray
) else if exist "%VENV_PY%" (
    start "AppLauncher Tray" "%VENV_PY%" launcher.py tray
) else (
    start "AppLauncher Tray" pythonw launcher.py tray
)
exit /b 0
