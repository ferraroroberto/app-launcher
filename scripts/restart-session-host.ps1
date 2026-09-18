# Manually restart the app-launcher session-host on :8446 (issue #615).
#
# tray.bat --restart deliberately EXCLUDES :8446 to protect live PTY
# sessions (project-scaffolding#35) -- so a change under src/session_host.py
# or app/session_host/ is not live until this process restarts, and nothing
# else restarts it automatically. This script is the one supported,
# documented way to do that: an explicit, operator-initiated action, never
# a side effect of a normal ship.
#
# THIS KILLS EVERY LIVE CODING-TAB / CHIEF SESSION ON THIS MACHINE. There is
# no drain, no idle check -- every PTY dies immediately, including any
# standing fleet chief. Only run this at a clean boundary (no live sessions
# mid-work), never unattended, never as part of building or testing a
# session-host change itself.
#
# Usage:
#   pwsh -File scripts/restart-session-host.ps1 -Confirm
#   (bare, with no -Confirm, prints the warning and exits 1 -- does nothing)

param(
    [switch]$Confirm
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$trayPs = Join-Path $env:USERPROFILE ".claude\tray\tray_lifecycle.ps1"
$trayBat = Join-Path $repoRoot "tray.bat"
$psExe = "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

if (-not $Confirm) {
    Write-Host "This restarts the app-launcher session-host (:8446)." -ForegroundColor Yellow
    Write-Host "It KILLS EVERY LIVE PTY SESSION ON THIS MACHINE, including any standing chief." -ForegroundColor Red
    Write-Host "There is no drain and no idle check. Only proceed at a clean boundary." -ForegroundColor Red
    Write-Host ""
    Write-Host "Re-run with -Confirm to proceed:" -ForegroundColor Yellow
    Write-Host "  pwsh -File scripts/restart-session-host.ps1 -Confirm"
    exit 1
}

if (-not (Test-Path $trayPs)) {
    Write-Host "Missing shared tray helper: $trayPs" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $trayBat)) {
    Write-Host "Missing tray.bat at repo root: $trayBat" -ForegroundColor Red
    exit 1
}

Write-Host "Reclaiming :8446 (this kills every live PTY now)..." -ForegroundColor Cyan
& $psExe -NoProfile -NonInteractive -File $trayPs reclaim `
    -VenvDir (Join-Path $repoRoot ".venv") -Ports 8446
if ($LASTEXITCODE -ne 0) {
    Write-Host "tray_lifecycle.ps1 reclaim exited $LASTEXITCODE -- session-host may still be running." -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "Restarting the tray (also spawns a fresh session-host since none is left to adopt)..." -ForegroundColor Cyan
& $trayBat --restart

# Bounded poll of the same /api/version freshness check #615 added, so this
# script proves the restart actually worked instead of trusting silence.
#
# Gate on stale_relevant, NOT raw stale (issue #1007). docs/restart-and-
# liveness.md is explicit that `stale` is true after any merge anywhere in
# the repo, so a correct restart used to print a false "STILL STALE"
# failure here. `stale_relevant` scopes it to whether a declared
# session-host path was actually touched -- which is the only thing this
# script can claim to have fixed.
#
# Both fields read null (never a confident false) when a SHA or the diff
# itself cannot be resolved, so null is reported as its own "unknown"
# outcome and is NOT treated as success.
function Format-Tri($value) {
    if ($value -eq $true) { return "true" }
    if ($value -eq $false) { return "false" }
    return "unknown"
}

$deadline = (Get-Date).AddSeconds(30)
$confirmed = $false
$lastSeen = ""
while ((Get-Date) -lt $deadline) {
    try {
        $body = Invoke-RestMethod -Uri "https://127.0.0.1:8445/api/version" -SkipCertificateCheck -TimeoutSec 3
        $host_ = $body.session_host
        if ($host_.reachable -eq $true) {
            $lastSeen = ("git_sha=$($host_.git_sha) started_at=$($host_.started_at) " +
                         "stale=$(Format-Tri $host_.stale) " +
                         "stale_relevant=$(Format-Tri $host_.stale_relevant)")
            if ($host_.stale_relevant -eq $false) {
                Write-Host "session-host: $lastSeen (live)" -ForegroundColor Green
                $confirmed = $true
                break
            }
            # true  -> the fresh host still loads code older than a declared
            #          session-host path change; keep polling, it may still
            #          be coming up.
            # null  -> freshness could not be resolved at all; also not a pass.
        }
    } catch {
        # webapp or session-host not up yet -- keep polling until the deadline.
    }
    Start-Sleep -Seconds 1
}
if (-not $confirmed) {
    if ($lastSeen) {
        Write-Host "session-host: $lastSeen" -ForegroundColor Yellow
        Write-Host ("Restart NOT confirmed within 30s: stale_relevant never reached false. " +
                    "The session-host is reachable but is not provably running the merged " +
                    "session-host code -- check it by hand.") -ForegroundColor Red
    } else {
        Write-Host "Could not reach the session-host within 30s -- check it by hand." -ForegroundColor Red
    }
    exit 1
}
