@echo off
chcp 65001 >nul
REM ============================================================================
REM  WEBAPP - standalone FastAPI launcher (HTTPS on :8445 when cert present)
REM ----------------------------------------------------------------------------
REM  Daily use: launch tray.bat instead — it adopt-or-spawns the webapp for
REM  you. This bat is for headless boxes, dev iteration, or when you want
REM  the webapp without the tray icon.
REM ============================================================================

setlocal
set "SCRIPT_DIR=%~dp0"
set "VENV_PY=%SCRIPT_DIR%.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo [ERROR] .venv missing. Run setup.bat first.
    exit /b 1
)

cd /d "%SCRIPT_DIR%" || exit /b 1

set "CERT_DIR=%SCRIPT_DIR%webapp\certificates"
set "CERT=%CERT_DIR%\cert.pem"
set "KEY=%CERT_DIR%\key.pem"

if not exist "%CERT%" (
    echo [INFO] No HTTPS cert found, running HTTP-only on :8445.
    echo        Run scripts\gen_ssl_cert.py to enable HTTPS.
    "%VENV_PY%" -m uvicorn app.webapp.server:app --host 0.0.0.0 --port 8445
) else (
    echo [INFO] HTTPS via %CERT%
    "%VENV_PY%" -m uvicorn app.webapp.server:app --host 0.0.0.0 --port 8445 --ssl-keyfile "%KEY%" --ssl-certfile "%CERT%"
)

exit /b %ERRORLEVEL%
