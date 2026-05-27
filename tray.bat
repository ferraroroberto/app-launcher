@echo off
chcp 65001 >nul
REM ============================================================================
REM  LAUNCHER TRAY - tray icon that owns the webapp lifecycle
REM ----------------------------------------------------------------------------
REM  Launch this on login (Startup folder) for always-on phone-first launcher.
REM
REM  Idempotent:
REM    tray.bat              -> no-op if an AppLauncher tray is already running
REM    tray.bat --restart    -> stop the running tray (and its tree: webapp on
REM                             :8445, session-host on :8446, cloudflared) and
REM                             start a fresh one
REM
REM  Detection matches the tray process by command line + this project's .venv
REM  path via CIM, then kills BY PID with /T. We never blanket-kill pythonw,
REM  so sister-app trays (PhotoOCR, VoiceTranscriber, local-llm-hub, ...) and
REM  any other unrelated python processes are untouched.
REM ============================================================================

setlocal EnableDelayedExpansion
set "SCRIPT_DIR=%~dp0"
set "VENV_DIR=%SCRIPT_DIR%.venv\Scripts"
set "VENV_PYW=%VENV_DIR%\pythonw.exe"
set "VENV_PY=%VENV_DIR%\python.exe"

cd /d "%SCRIPT_DIR%" || exit /b 1

set "WANT_RESTART="
if /i "%~1"=="--restart" set "WANT_RESTART=1"
if /i "%~1"=="-r"        set "WANT_RESTART=1"

set "PS=C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
set "TRAY_VENV=%VENV_DIR%"
set "TRAY_PIDS="
for /f "usebackq delims=" %%P in (`%PS% -NoProfile -NonInteractive -Command "$v=$env:TRAY_VENV; Get-CimInstance Win32_Process -Filter 'Name = ''pythonw.exe'' OR Name = ''python.exe''' | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($v, [System.StringComparison]::OrdinalIgnoreCase) -and $_.CommandLine -match 'launcher\.py\s+tray' } | Select-Object -ExpandProperty ProcessId"`) do (
    if defined TRAY_PIDS (set "TRAY_PIDS=!TRAY_PIDS! %%P") else (set "TRAY_PIDS=%%P")
)

if defined TRAY_PIDS (
    if not defined WANT_RESTART (
        echo AppLauncher tray is already running ^(PID: !TRAY_PIDS!^).
        echo Run "tray.bat --restart" to stop it and start fresh.
        exit /b 0
    )
    echo Stopping previous AppLauncher tray ^(PID: !TRAY_PIDS!^)...
    for %%P in (!TRAY_PIDS!) do (
        taskkill /T /F /PID %%P >nul 2>&1
    )
    REM Give Windows a moment to release :8445 / :8446 before rebinding.
    ping 127.0.0.1 -n 3 >nul
)

REM Prefer pythonw.exe so no console window stays open.
if exist "%VENV_PYW%" (
    start "AppLauncher Tray" "%VENV_PYW%" launcher.py tray
) else if exist "%VENV_PY%" (
    start "AppLauncher Tray" "%VENV_PY%" launcher.py tray
) else (
    start "AppLauncher Tray" pythonw launcher.py tray
)
exit /b 0
