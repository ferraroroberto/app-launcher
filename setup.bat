@echo off
chcp 65001 >nul
REM ============================================================================
REM  SETUP - install this app on this PC, or bring its setup up to date
REM ----------------------------------------------------------------------------
REM  Copy to setup.bat at the repo root; it needs no edit. What is specific to
REM  the app is declared in the [install] table of .fleet.toml.
REM  Safe to run any number of times; it never overwrites what this PC has
REM  configured. The work is src\installer.py (docs\install.md):
REM    setup.bat            create .venv, install requirements, create missing
REM                         configs, add keys a settings file lacks (after a
REM                         backup), end with required and optional checklists
REM    setup.bat -WhatIf    list every change, make none
REM    setup.bat -Startup   also start the app at log on
REM    setup.bat -MigrateFrom <old folder> [-StopOld] [-Json]
REM                         in a fresh clone: copy this PC's declared files from
REM                         an existing install, install, start, verify
REM    setup.bat -InPlace [-WhatIf] [-Json]
REM                         replace this install at the same path: stop it,
REM                         rename it to <folder>.bak-YYYYMMDD-N, clone fresh
REM                         here, migrate from the backup
REM  A batch file, not a .ps1, so the PowerShell execution policy (AllSigned
REM  included) never refuses it. Keep this file ASCII-only: cmd.exe reads ahead
REM  in the codepage that was active before chcp 65001 ran.
REM ============================================================================

setlocal
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%" || exit /b 1

set "IN_PLACE="
for %%A in (%*) do if /i "%%~A"=="-InPlace" set "IN_PLACE=1"

REM  The repo's own python once .venv exists, else the system one (the py
REM  launcher first: a bare `python` on PATH can be the Microsoft Store stub).
REM  -InPlace always takes the system one: it renames the folder .venv is in.
set "PY_EXE=%SCRIPT_DIR%.venv\Scripts\python.exe"
set "PY_ARGS="
if not defined IN_PLACE if exist "%PY_EXE%" goto :run
set "PY_EXE="
where py >nul 2>nul && set "PY_EXE=py" && set "PY_ARGS=-3"
if not defined PY_EXE where python >nul 2>nul && set "PY_EXE=python"
if not defined PY_EXE goto :no_python
if defined IN_PLACE goto :in_place

:run
REM  -I: isolated mode keeps src\ off sys.path, so the app's src package is never
REM  imported before its requirements exist.
"%PY_EXE%" %PY_ARGS% -I "%SCRIPT_DIR%src\installer.py" %*
exit /b %ERRORLEVEL%

:in_place
REM  Windows will not rename a folder a process lives in, so the run moves out:
REM  a temporary copy of the migration modules, the system python, and a current
REM  folder outside the tree. The run, the removal of the stage folder and the
REM  exit share one line because once the folder is renamed, the next line
REM  cmd.exe would read is the new clone's. The current folder is %TEMP%, not
REM  the stage, so nothing holds the stage open when it is removed.
set "STAGE=%TEMP%\setup-inplace-%RANDOM%%RANDOM%"
mkdir "%STAGE%" || exit /b 1
copy /y "%SCRIPT_DIR%src\installer_migrate.py" "%STAGE%\" >nul || goto :stage_failed
copy /y "%SCRIPT_DIR%src\installer_declaration.py" "%STAGE%\" >nul || goto :stage_failed
copy /y "%SCRIPT_DIR%src\installer_inplace.py" "%STAGE%\" >nul || goto :stage_failed
cd /d "%TEMP%"
"%PY_EXE%" %PY_ARGS% -I "%STAGE%\installer_inplace.py" -Folder "%SCRIPT_DIR%." %* & call set "RC=%%ERRORLEVEL%%" & rmdir /s /q "%STAGE%" & call exit /b %%RC%%

:stage_failed
rmdir /s /q "%STAGE%"
exit /b 1

:no_python
echo ERROR: no Python found. Install Python 3.11 or newer from python.org, then run setup.bat again.
exit /b 1
