@echo off
chcp 65001 >nul
REM ============================================================================
REM  SETUP - install app-launcher on this PC, or bring its setup up to date
REM ----------------------------------------------------------------------------
REM  Safe to run any number of times; it never overwrites what this PC has
REM  configured. The work is scripts\install.py (docs\install.md):
REM    setup.bat            create .venv, install requirements, create missing
REM                         configs, add keys a config lacks (after a backup)
REM    setup.bat -WhatIf    list every change, make none
REM    setup.bat -Startup   also start the tray at log on
REM  A batch file, not a .ps1, so an AllSigned execution policy never refuses it.
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
"%PY_EXE%" %PY_ARGS% "%SCRIPT_DIR%scripts\install.py" %*
exit /b %ERRORLEVEL%

:no_python
echo ERROR: no Python found. Install Python 3 from python.org, then run setup.bat again.
exit /b 1
