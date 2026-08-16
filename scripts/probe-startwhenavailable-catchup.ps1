<#
.SYNOPSIS
  Empirically answer issue #757's open question 2: does StartWhenAvailable
  catch up a trigger that Task Scheduler skipped because nobody was logged on?

.DESCRIPTION
  #746 set StartWhenAvailable=True on every \AppLauncher\ task. Windows
  documents that flag for missed starts "generally", but it is unverified
  whether a trigger skipped for LogonType=InteractiveToken with no session
  counts as a missed start at all. If it does, #757's exposure is much
  narrower than it looks; if it does not, an S4U principal is the only fix.

  That fact cannot be observed from inside a logged-on session, and this host
  has had exactly one continuous session since 2026-08-13 03:31:59, so no
  natural experiment exists in its event log either. This script arranges one.

  It registers a THROWAWAY task (default name: _zz-757-catchup-probe, at the
  root task path, deliberately outside \AppLauncher\ so it can never be
  mistaken for a real job) with:
    - the DEFAULT InteractiveToken principal, i.e. the exact #757 defect
    - StartWhenAvailable = $true, i.e. the exact #746 mitigation
    - a one-shot trigger N minutes in the future
    - an action that appends one ISO timestamp to a log file

  It never touches a real job or an \AppLauncher\ task, and it needs no
  elevation: InteractiveToken is the principal a non-elevated caller can
  already register (that is the whole defect).

.PARAMETER Arm
  Register the probe task with a trigger -InMinutes from now.

.PARAMETER InMinutes
  Minutes from now for the one-shot trigger. Default 10 - long enough to
  sign out deliberately, short enough not to forget.

.PARAMETER Check
  Read the probe's log plus the Winlogon logon record and print the verdict.

.PARAMETER Cleanup
  Unregister the probe task and delete its log + state file.

.EXAMPLE
  # 1. Arm it, then SIGN OUT (Start > user > Sign out). Locking is NOT enough:
  #    a locked session is still a session and the task will fire normally.
  pwsh -File scripts/probe-startwhenavailable-catchup.ps1 -Arm -InMinutes 10

  # 2. Sign back in after the trigger time has passed, then:
  pwsh -File scripts/probe-startwhenavailable-catchup.ps1 -Check

  # 3. Always finish with:
  pwsh -File scripts/probe-startwhenavailable-catchup.ps1 -Cleanup
#>
[CmdletBinding()]
param(
    [switch]$Arm,
    [int]$InMinutes = 10,
    [switch]$Check,
    [switch]$Cleanup,
    [string]$TaskName = '_zz-757-catchup-probe'
)

$ErrorActionPreference = 'Stop'

# Deliberately outside \AppLauncher\: nothing in this repo enumerates the
# root task path, so the probe can never be read as a job by the Jobs tab,
# the coverage scan, or delete_schtasks' blind-delete fallback.
$TaskPath = '\'
$LogPath = Join-Path $env:TEMP 'app-launcher-757-catchup-probe.log'
$StatePath = Join-Path $env:TEMP 'app-launcher-757-catchup-probe.json'
$PowerShellExe = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'

function Get-ProbeTask {
    try {
        return Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction Stop
    } catch {
        return $null
    }
}

function Invoke-Arm {
    if (Get-ProbeTask) {
        Write-Host "Probe task already registered - re-arming (replacing it)."
    }
    if (Test-Path $LogPath) { Remove-Item $LogPath -Force }

    $fireAt = (Get-Date).AddMinutes($InMinutes)
    # Single-quoted inner command so nothing interpolates at registration
    # time; the child resolves Get-Date when it actually runs.
    $inner = "Add-Content -LiteralPath '$LogPath' -Value (Get-Date -Format o)"
    $action = New-ScheduledTaskAction -Execute $PowerShellExe `
        -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -Command `"$inner`""
    $trigger = New-ScheduledTaskTrigger -Once -At $fireAt
    # StartWhenAvailable is the flag under test. The battery flags mirror
    # what _apply_power_policy sets fleet-wide (#746) so the probe differs
    # from a real job in exactly one way: it is throwaway.
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable

    Register-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName `
        -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

    $task = Get-ProbeTask
    $logonType = $task.Principal.LogonType
    @{
        armed_at   = (Get-Date).ToString('o')
        fire_at    = $fireAt.ToString('o')
        logon_type = [string]$logonType
        task       = "$TaskPath$TaskName"
        log        = $LogPath
    } | ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding UTF8

    Write-Host ""
    Write-Host "Armed: $TaskPath$TaskName"
    Write-Host "  LogonType        : $logonType   (must be Interactive for a valid probe)"
    Write-Host "  StartWhenAvailable: $($task.Settings.StartWhenAvailable)"
    Write-Host "  Fires at         : $($fireAt.ToString('yyyy-MM-dd HH:mm:ss'))"
    Write-Host "  Log              : $LogPath"
    Write-Host ""
    if ("$logonType" -ne 'Interactive') {
        Write-Warning "LogonType is '$logonType', not 'Interactive' - this probe tests nothing. Investigate before signing out."
    }
    Write-Host "NEXT: SIGN OUT now (Start > your user > Sign out)."
    Write-Host "      Locking the screen is NOT enough - a locked session is still a session."
    Write-Host "      Sign back in after $($fireAt.ToString('HH:mm')), then run this script with -Check."
}

function Invoke-Check {
    if (-not (Test-Path $StatePath)) {
        Write-Host "No probe state at $StatePath - was it armed on this machine?"
        return
    }
    $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    $fireAt = [datetime]::Parse($state.fire_at)

    $runs = @()
    if (Test-Path $LogPath) {
        $runs = Get-Content -LiteralPath $LogPath |
            Where-Object { $_.Trim() } |
            ForEach-Object { [datetime]::Parse($_.Trim()) }
    }

    # The sign-in that ended the logged-out window. Winlogon 7001 = logon.
    $logon = $null
    try {
        $logon = (Get-WinEvent -FilterHashtable @{
            LogName      = 'System'
            ProviderName = 'Microsoft-Windows-Winlogon'
            Id           = 7001
            StartTime    = [datetime]::Parse($state.armed_at)
        } -ErrorAction Stop | Sort-Object TimeCreated | Select-Object -First 1).TimeCreated
    } catch {
        $logon = $null
    }

    Write-Host ""
    Write-Host "Probe task    : $($state.task)"
    Write-Host "LogonType     : $($state.logon_type)"
    Write-Host "Trigger due   : $($fireAt.ToString('yyyy-MM-dd HH:mm:ss'))"
    Write-Host "First logon   : $(if ($logon) { $logon.ToString('yyyy-MM-dd HH:mm:ss') } else { '<none since arming>' })"
    Write-Host "Runs recorded : $($runs.Count)"
    foreach ($r in $runs) { Write-Host "  ran at $($r.ToString('yyyy-MM-dd HH:mm:ss'))" }
    Write-Host ""

    if (-not $logon) {
        Write-Host "VERDICT: INVALID - no sign-in recorded since the probe was armed."
        Write-Host "         The session never ended, so the trigger was never skipped for"
        Write-Host "         'no session'. Sign out fully before the trigger and retry."
        return
    }
    if ($runs.Count -eq 0) {
        if ((Get-Date) -gt $logon.AddMinutes(15)) {
            Write-Host "VERDICT: NO CATCH-UP. The trigger came due with no session, the task"
            Write-Host "         never ran, and it still had not run 15+ minutes after sign-in."
            Write-Host "         StartWhenAvailable does NOT rescue a no-session skip -> the S4U"
            Write-Host "         principal is the only fix for #757."
        } else {
            Write-Host "VERDICT: TOO EARLY. No run yet, but sign-in was less than 15 minutes ago."
            Write-Host "         Windows delays catch-up starts by up to ~10 minutes. Re-run -Check."
        }
        return
    }
    $first = $runs[0]
    if ($first -lt $logon) {
        Write-Host "VERDICT: INVALID - the task ran at $($first.ToString('HH:mm:ss')), before the"
        Write-Host "         sign-in at $($logon.ToString('HH:mm:ss')). A session was still up when"
        Write-Host "         the trigger came due, so nothing was skipped. Retry."
        return
    }
    $delay = [int]($first - $logon).TotalMinutes
    Write-Host "VERDICT: CATCH-UP CONFIRMED. The trigger was skipped while logged out and the"
    Write-Host "         task ran $delay minute(s) after sign-in. StartWhenAvailable DOES rescue"
    Write-Host "         a no-session skip - record this on #757; it narrows the exposure to the"
    Write-Host "         window between the skipped slot and the next sign-in."
}

function Invoke-Cleanup {
    if (Get-ProbeTask) {
        Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Confirm:$false
        Write-Host "Unregistered $TaskPath$TaskName"
    } else {
        Write-Host "No probe task to remove."
    }
    foreach ($p in @($LogPath, $StatePath)) {
        if (Test-Path $p) { Remove-Item $p -Force; Write-Host "Removed $p" }
    }
}

if ($Arm)     { Invoke-Arm;     exit 0 }
if ($Check)   { Invoke-Check;   exit 0 }
if ($Cleanup) { Invoke-Cleanup; exit 0 }

Write-Host "Pass one of -Arm, -Check or -Cleanup. See: Get-Help $PSCommandPath -Detailed"
exit 1
