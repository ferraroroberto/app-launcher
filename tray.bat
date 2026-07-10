@echo off
chcp 65001 >nul
REM ============================================================================
REM  AppLauncher TRAY - tray icon that owns a long-lived service lifecycle
REM ----------------------------------------------------------------------------
REM  Rebuilt from project-scaffolding's tray.bat.template (project-scaffolding#54
REM  / #144, app-launcher#423). Everything below the four ADAPT values is the
REM  orphan-proof reclaim-then-start machinery and is copied verbatim, so this
REM  file matches every sister tray. Full reasoning: scaffold
REM  docs/windows-tray.md + project-scaffolding#29.
REM
REM  Launch this on login (Startup folder) for always-on phone-first launcher.
REM
REM  Idempotent:
REM    tray.bat              -> no-op if an AppLauncher tray is already running
REM    tray.bat --restart    -> stop the running tray (and its service tree) and
REM                             start a fresh one
REM
REM  Detection matches the tray process by command line + this project's .venv
REM  path via CIM, then kills BY PID with /T. We never blanket-kill pythonw, so
REM  sister-app trays (PhotoOCR, VoiceTranscriber, local-llm-hub, ...) and any
REM  other unrelated python processes are untouched.
REM
REM  The full detect -> kill -> reclaim -> start -> verify lifecycle lives in
REM  app\tray\tray_lifecycle.ps1 (a committed helper shelled to with -File), NOT
REM  in cmd-side `for /f` output capture or inline `powershell -Command "..."`.
REM  Both cmd shapes have failed under non-interactive nested callers (Git Bash
REM  -> `cmd /c "tray.bat --restart"`, or a finisher skill's Bash tool): detect
REM  output came back empty, nothing was killed, and --restart silently degraded
REM  to a plain start that adopted the stale webapp and reported success.
REM  Delegating once to PowerShell makes behavior identical from any caller and
REM  lets stale git_sha verification fail loudly (project-scaffolding#54).
REM
REM  --restart is orphan-proof: besides killing the tray subtree, it reclaims
REM  this app's owned service ports by their owning PID, regardless of process
REM  parentage. A service child that got detached from its tray (a stale process
REM  from an earlier run) would otherwise survive a subtree kill, block the fresh
REM  tray from binding, and keep serving the old build while the restart reports
REM  success. The reclaim is scoped to processes whose CommandLine is under THIS
REM  repo's .venv (NOT the process image path): a venv-launched pythonw re-execs
REM  the base interpreter, so .Path reports the shared base python while only the
REM  CommandLine still carries the .venv path. Matching the image path would miss
REM  the real service; the CommandLine scope keeps the sweep on THIS repo only.
REM
REM  Mutex-shared ports (a port another app may legitimately own) must NOT go in
REM  the OWNED_PORTS reclaim list -- reclaiming one would kill the sibling.
REM
REM  IMPORTANT: the :8446 session-host is linked-but-independent -- it hosts the
REM  user's live Coding / PTY sessions, is spawned DETACHED so a `taskkill /T`
REM  subtree kill cannot reach it, and is re-adopted by the fresh tray on start.
REM  It is deliberately NOT in OWNED_PORTS below (reclaiming it would kill those
REM  sessions). Only the webapp port :8445, which this tray definitively owns
REM  and cycles, is reclaimed.
REM ============================================================================

setlocal EnableDelayedExpansion
set "SCRIPT_DIR=%~dp0"

cd /d "%SCRIPT_DIR%" || exit /b 1

REM === ADAPT (1/4): short app name, used in messages + the start window title ===
set "APP_NAME=AppLauncher"
REM === ADAPT (2/4): the args python is started with to launch the tray,
REM     e.g. "launcher.py tray"  or  "-m tray" ===
set "TRAY_LAUNCH=launcher.py tray"

set "WANT_RESTART="
if /i "%~1"=="--restart" set "WANT_RESTART=1"
if /i "%~1"=="-r"        set "WANT_RESTART=1"

REM === ADAPT (3/4): in the -TrayMatch below, replace with a regex matching
REM     THIS app's tray invocation, e.g. launcher\.py\s+tray  or  -m\s+tray
set "PS=C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
set "TRAY_VENV=%SCRIPT_DIR%.venv"
set "TRAY_PS=%SCRIPT_DIR%app\tray\tray_lifecycle.ps1"
if not exist "%TRAY_PS%" (
    echo ERROR: missing tray helper "%TRAY_PS%" -- vendor app\tray\tray_lifecycle.ps1 from the scaffold.
    exit /b 1
)

REM === ADAPT (4/4): this tray's exclusively-owned ports as a comma list.
REM     Exclude any mutex-shared port -- see the :8446 note above. ===
set "OWNED_PORTS=8445"
REM Optional override. Leave blank to verify http://127.0.0.1:<first-owned-port>/api/version.
set "VERSION_URL="

set "RESTART_ARG="
if defined WANT_RESTART set "RESTART_ARG=-Restart"

%PS% -NoProfile -NonInteractive -File "%TRAY_PS%" launch -AppName "%APP_NAME%" -ScriptDir "%SCRIPT_DIR%" -VenvDir "%TRAY_VENV%" -TrayMatch "launcher\.py\s+tray" -Ports "%OWNED_PORTS%" -TrayLaunch "%TRAY_LAUNCH%" -VersionUrl "%VERSION_URL%" !RESTART_ARG!
exit /b %ERRORLEVEL%
