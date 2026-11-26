@echo off
chcp 65001 >nul
REM ============================================================================
REM  SETUP - install this app on this PC, or bring its setup up to date
REM ----------------------------------------------------------------------------
REM  What is specific to app-launcher is declared in the [install] table of
REM  .fleet.toml.
REM  Safe to run any number of times; it never overwrites what this PC has
REM  configured. The work is src\installer.py (docs\install.md):
REM    setup.bat            create .venv, install requirements, create missing
REM                         configs, add keys a settings file lacks (after a
REM                         backup), end with an ok/missing/unknown checklist
REM    setup.bat -WhatIf    list every change, make none
REM    setup.bat -Startup   also start the tray at log on
REM  A batch file, not a .ps1, so the PowerShell execution policy (AllSigned
REM  included) never refuses it. Keep this file ASCII-only: cmd.exe reads ahead
REM  in the codepage that was active before chcp 65001 ran.
REM ============================================================================

setlocal
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%" || exit /b 1

REM  The repo's own python once .venv exists, else the system one (the py
REM  launcher first: a bare `python` on PATH can be the Microsoft Store stub).
set "PY_EXE=%SCRIPT_DIR%.venv\Scripts\python.exe"
set "PY_ARGS="
if exist "%PY_EXE%" goto :run
set "PY_EXE="
where py >nul 2>nul && set "PY_EXE=py" && set "PY_ARGS=-3"
if not defined PY_EXE where python >nul 2>nul && set "PY_EXE=python"
if not defined PY_EXE goto :no_python

:run
REM  -I: isolated mode keeps src\ off sys.path, so the app's src package is never
REM  imported before its requirements exist.
"%PY_EXE%" %PY_ARGS% -I "%SCRIPT_DIR%src\installer.py" %*
exit /b %ERRORLEVEL%

:no_python
echo ERROR: no Python found. Install Python 3.11 or newer from python.org, then run setup.bat again.
exit /b 1
