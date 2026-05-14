@echo off
chcp 65001 >nul
REM ============================================================================
REM  WEBAPP + NAMED CLOUDFLARE TUNNEL (persistent URL)
REM ----------------------------------------------------------------------------
REM  Starts the webapp on :8445 and a named Cloudflare tunnel using
REM  webapp/cloudflared.yml so the public URL stays the same every launch.
REM
REM  One-time setup (run from this directory):
REM    cloudflared tunnel login
REM    cloudflared tunnel create launcher
REM    cloudflared tunnel route dns launcher launcher.your-domain.example
REM    copy webapp\cloudflared.sample.yml webapp\cloudflared.yml
REM    REM ...then edit webapp\cloudflared.yml and fill in your UUID + hostname
REM
REM  Press Ctrl+C to stop both.
REM ============================================================================

setlocal
set "SCRIPT_DIR=%~dp0"
set "VENV_PY=%SCRIPT_DIR%.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo [ERROR] .venv missing. Run setup.bat first.
    exit /b 1
)

where cloudflared >nul 2>&1
if errorlevel 1 (
    echo [ERROR] cloudflared not installed.
    echo   winget install Cloudflare.cloudflared
    pause
    exit /b 1
)

if not exist "%SCRIPT_DIR%webapp\cloudflared.yml" (
    echo [ERROR] webapp\cloudflared.yml missing.
    echo   Copy webapp\cloudflared.sample.yml to webapp\cloudflared.yml
    echo   and fill in your tunnel UUID and hostname.
    pause
    exit /b 1
)

cd /d "%SCRIPT_DIR%" || exit /b 1

"%VENV_PY%" scripts\run_named_tunnel.py
exit /b %ERRORLEVEL%
