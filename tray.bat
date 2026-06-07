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
REM
REM  --restart is orphan-proof: in addition to killing the tray subtree, it
REM  reclaims this app's service ports :8445 (webapp) and :8446 (session-host)
REM  by their owning PID, regardless of process parentage. A webapp/session-host
REM  that got detached from its tray (stale process from an earlier run) would
REM  otherwise survive a subtree kill, block the fresh tray from binding, and
REM  keep serving the old build while the restart reports success. The reclaim
REM  is scoped to processes under THIS repo's .venv, so sister apps' ports and
REM  their cloudflared tunnels are never touched. See project-scaffolding#29.
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
set "TRAY_PIDS="
for /f "usebackq delims=" %%P in (`%PS% -NoProfile -NonInteractive -Command "$v=$env:TRAY_VENV; Get-CimInstance Win32_Process -Filter 'Name = ''pythonw.exe'' OR Name = ''python.exe''' | Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($v, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -and $_.CommandLine -match 'launcher\.py\s+tray' } | Select-Object -ExpandProperty ProcessId"`) do (
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
    %PS% -NoProfile -NonInteractive -Command "$v=$env:RECLAIM_VENV; foreach ($port in 8445,8446) { Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | ForEach-Object { $opid = $_.OwningProcess; $cim = Get-CimInstance Win32_Process -Filter ('ProcessId = {0}' -f $opid) -ErrorAction SilentlyContinue; if ($cim -and $cim.CommandLine -and $cim.CommandLine.IndexOf($v, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) { Write-Host ('Reclaiming :{0} from PID {1}' -f $port, $opid); Stop-Process -Id $opid -Force -ErrorAction SilentlyContinue } } }"
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
