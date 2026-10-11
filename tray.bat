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
REM  the ONE shared, machine-local tray_lifecycle.ps1 owned by fleet-config
REM  (project-scaffolding#153) -- exposed at %USERPROFILE%\.claude\tray\ by
REM  fleet-config's install.ps1 junction, shelled to with -File, NOT in
REM  cmd-side `for /f` output capture or inline `powershell -Command "..."`.
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
REM  Clear an APP_NAME inherited from the caller (#963): the usual restart route
REM  is an agent inside a launcher-hosted session that already carries the old
REM  leaked value, and the tray chain hands its whole environment down. Cleared
REM  inside setlocal, so the calling console keeps its own copy.
set "APP_NAME="
set "SCRIPT_DIR=%~dp0"
REM  `%~dp0` always ends in a trailing backslash, which is what the path joins
REM  below want -- but NOT what a quoted argument can carry. Windows argv parsing
REM  treats an odd run of backslashes before a closing quote as escaping that
REM  quote, so `-ScriptDir "%SCRIPT_DIR%"` swallows the rest of the command line
REM  and every later switch (-TrayMatch, -Ports, ...) arrives EMPTY -- detect
REM  matches nothing, reclaim reclaims nothing, and --restart silently degrades
REM  to the adopt-the-stale-build start this template exists to prevent
REM  (project-scaffolding#145). Pass the de-slashed copy as the argument.
set "SCRIPT_DIR_ARG=%SCRIPT_DIR:~0,-1%"

cd /d "%SCRIPT_DIR%" || exit /b 1

REM === ADAPT (1/4): short app name, used in messages + the start window title ===
REM  Namespaced, never a bare APP_NAME: setlocal hides a variable from the
REM  calling console but NOT from children, so the whole tray -> webapp ->
REM  session-host chain inherits it, and so does every session and app it
REM  spawns -- where APP_NAME is another project's own config key (#963).
set "TRAY_APP_NAME=AppLauncher"
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
REM  ONE shared, machine-local copy owned by fleet-config (project-scaffolding#153)
REM  -- junctioned by fleet-config's install.ps1, never vendored per-app.
set "TRAY_PS=%USERPROFILE%/.claude/tray/tray_lifecycle.ps1"
REM  --restart without the helper stays a hard error: the orphan-proof restart
REM  is fleet-config's job, and degrading it to a plain start would adopt the
REM  stale build, see project-scaffolding#54. A plain start has no such risk, so
REM  it jumps to the standalone start at the bottom. Two flat ifs, not a nested
REM  block: an `exit /b 1` nested inside a block that also holds a `goto` comes
REM  back as exit code 0 under cmd.exe (project-scaffolding#345).
if not exist "%TRAY_PS%" if not defined WANT_RESTART goto :standalone_start
if not exist "%TRAY_PS%" (
    echo ERROR: missing shared tray helper "%TRAY_PS%"
    echo        --restart needs it: install fleet-config and run its install.ps1, then retry.
    echo        See ferraroroberto/fleet-config's README for details.
    exit /b 1
)

REM === ADAPT (4/4): this tray's exclusively-owned ports as a comma list.
REM     Exclude any mutex-shared port -- see the :8446 note above. ===
set "OWNED_PORTS=8445"
REM Optional override. Leave blank to verify http://127.0.0.1:<first-owned-port>/api/version.
set "VERSION_URL="

set "RESTART_ARG="
if defined WANT_RESTART set "RESTART_ARG=-Restart"

%PS% -NoProfile -NonInteractive -File "%TRAY_PS%" launch -AppName "%TRAY_APP_NAME%" -ScriptDir "%SCRIPT_DIR_ARG%" -VenvDir "%TRAY_VENV%" -TrayMatch "launcher\.py\s+tray" -Ports "%OWNED_PORTS%" -TrayLaunch "%TRAY_LAUNCH%" -VersionUrl "%VERSION_URL%" !RESTART_ARG!
exit /b %ERRORLEVEL%

REM  Standalone start (project-scaffolding#345): the shared helper is missing,
REM  e.g. this app was moved to a PC that has none of the other fleet repos, and
REM  a plain start was asked for. Launch the tray straight from this repo's .venv
REM  and exit 0. No detect/adopt here: the tray's own single-instance guard
REM  (app/tray/single_instance.py) stops a duplicate, and --restart is refused
REM  above, so nothing in this path can mask a stale build.
:standalone_start
set "TRAY_PYW=%TRAY_VENV%\Scripts\pythonw.exe"
if not exist "%TRAY_PYW%" (
    echo ERROR: missing "%TRAY_PYW%" - create this app's .venv first.
    exit /b 1
)
echo NOTE: shared tray helper not found, starting the %TRAY_APP_NAME% tray standalone - --restart needs fleet-config.
REM  Start-Process (as the shared helper does), not cmd `start`: `start` lets the
REM  tray inherit this script's stdout pipe, so a caller capturing our output (an
REM  agent shell, a finisher skill) would block until the tray exits. Values go
REM  through the environment, so no quoting nests inside the -Command string.
%PS% -NoProfile -NonInteractive -Command "Start-Process -FilePath $env:TRAY_PYW -ArgumentList $env:TRAY_LAUNCH -WorkingDirectory $env:SCRIPT_DIR_ARG -WindowStyle Hidden -ErrorAction Stop"
exit /b %ERRORLEVEL%
