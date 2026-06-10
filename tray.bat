@echo off
chcp 65001 >nul
REM ============================================================================
REM  LAUNCHER TRAY - tray icon that owns the webapp lifecycle
REM ----------------------------------------------------------------------------
REM  Launch this on login (Startup folder) for always-on phone-first launcher.
REM
REM  Idempotent:
REM    tray.bat              -> no-op if an AppLauncher tray is already running
REM    tray.bat --restart    -> stop the running tray + its owned-and-cycled
REM                             children (webapp :8445, cloudflared) and start a
REM                             fresh one. The :8446 session-host is linked-but-
REM                             independent: it is spawned DETACHED, survives the
REM                             restart, and the fresh tray re-adopts it, so open
REM                             Coding / PTY sessions are NOT killed.
REM
REM  Detection matches the tray process by command line + this project's .venv
REM  path via CIM, then kills BY PID with /T. We never blanket-kill pythonw,
REM  so sister-app trays (PhotoOCR, VoiceTranscriber, local-llm-hub, ...) and
REM  any other unrelated python processes are untouched.
REM
REM  --restart is orphan-proof: in addition to killing the tray subtree, it
REM  reclaims this app's OWNED-AND-CYCLED port :8445 (webapp) by its owning PID,
REM  regardless of process parentage -- a stale webapp detached from an earlier
REM  run would otherwise block the fresh tray from binding and keep serving the
REM  old build while the restart reports success. The reclaim is scoped to
REM  processes under THIS repo's .venv, so sister apps are never touched.
REM  :8446 (the session-host) is deliberately NOT reclaimed: it is linked-but-
REM  independent (it hosts the user's PTY / Coding sessions), spawned detached so
REM  taskkill /T cannot reach it, and re-adopted by the fresh tray on start.
REM  See project-scaffolding#29 (reclaim) and #35 (detach + re-adopt).
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
set "TRAY_VENV=%SCRIPT_DIR%.venv"
set "TRAY_PS=%SCRIPT_DIR%app\tray\tray_lifecycle.ps1"
if not exist "%TRAY_PS%" (
    echo ERROR: missing tray helper "%TRAY_PS%" -- vendor app\tray\tray_lifecycle.ps1 from the scaffold.
    exit /b 1
)
set "TRAY_PIDS="
for /f "usebackq delims=" %%P in (`%PS% -NoProfile -NonInteractive -File "%TRAY_PS%" detect -VenvDir "%TRAY_VENV%" -TrayMatch "launcher\.py\s+tray"`) do (
    if defined TRAY_PIDS (set "TRAY_PIDS=!TRAY_PIDS! %%P") else (set "TRAY_PIDS=%%P")
)

if defined TRAY_PIDS if not defined WANT_RESTART (
    echo AppLauncher tray is already running ^(PID: !TRAY_PIDS!^).
    echo Run "tray.bat --restart" to stop it and start fresh.
    exit /b 0
)

if defined WANT_RESTART (
    if defined TRAY_PIDS (
        echo Stopping previous AppLauncher tray ^(PID: !TRAY_PIDS!^)...
        for %%P in (!TRAY_PIDS!) do (
            taskkill /T /F /PID %%P >nul 2>&1
        )
    )
    REM Orphan-proof: reclaim this app's service ports from ANY holder whose
    REM command line is under this repo's .venv, even one detached from the tray
    REM subtree above. We match on CommandLine (not the process image path):
    REM a venv-launched pythonw re-execs the base interpreter, so .Path reports
    REM the shared base python while CommandLine still carries the .venv path.
    REM Matching the image path would miss the real webapp/session-host; the
    REM CommandLine scope keeps the sweep on THIS repo's children only.
    set "RECLAIM_VENV=%SCRIPT_DIR%.venv"
    %PS% -NoProfile -NonInteractive -File "%TRAY_PS%" reclaim -VenvDir "%RECLAIM_VENV%" -Ports "8445"
    REM Give Windows a moment to release :8445 before rebinding.
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
