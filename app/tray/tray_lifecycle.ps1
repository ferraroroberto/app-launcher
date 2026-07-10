#Requires -Version 5.1
<#
    Canonical tray lifecycle helper.

    CANONICAL, VENDORED VERBATIM from project-scaffolding. Do **not** edit this
    file per-app -- it is byte-identical across every fleet tray so a fix made
    once in the scaffold re-propagates everywhere by re-copying. App-specific
    values (the .venv path, the tray-match regex, the owned ports, the tray
    launch command, and optional version URL) are passed in as arguments by
    tray.bat, never hardcoded here -- that is what keeps the file identical.
    Lives alongside the other vendored tray primitive, app/tray/single_instance.py.
    Full reasoning: scaffold docs/windows-tray.md + project-scaffolding#29 /
    #36 / #54.

    Why a committed .ps1 instead of cmd-side lifecycle logic
    (project-scaffolding#54): the old batch shape first embedded CIM/port logic
    in `powershell.exe -Command "..."`, then moved that logic to `-File` but
    still captured detect output through `for /f usebackq`. Launched
    non-interactively through a nested shell (Git Bash -> `cmd /c "tray.bat
    --restart"`, or a finisher skill's Bash tool), that cmd capture can return
    empty even when this helper works standalone. The result is the same silent
    stale-build failure: no tray kill, no port reclaim, a plain start that adopts
    the old webapp, and exit 0. The `launch` action below owns the whole detect
    -> kill -> reclaim -> start -> verify sequence inside one PowerShell
    process, so tray.bat does not parse helper output at all.

    Keep this file ASCII-only: a stray non-ASCII char breaks Windows PowerShell
    5.1 parsing (scaffold docs/windows-tray.md, "Platform gotcha").

    Usage (from tray.bat):
      detect  -> emits the matching tray PIDs, one per line (empty if none):
        powershell.exe -NoProfile -NonInteractive -File tray_lifecycle.ps1 `
          detect -VenvDir "<repo>\.venv" -TrayMatch "launcher\.py\s+tray"
      reclaim -> kills the owning PID of each listed port whose CommandLine is
                 under this repo's .venv (orphan-proof), printing each reclaim:
        powershell.exe -NoProfile -NonInteractive -File tray_lifecycle.ps1 `
          reclaim -VenvDir "<repo>\.venv" -Ports "8445,8446"
      launch  -> idempotent start, or restart with port reclaim and git_sha
                 verification:
        powershell.exe -NoProfile -NonInteractive -File tray_lifecycle.ps1 `
          launch -AppName "my-app" -ScriptDir "<repo>" -VenvDir "<repo>\.venv" `
          -TrayMatch "launcher\.py\s+tray" -Ports "8445" `
          -TrayLaunch "launcher.py tray" -Restart
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('detect', 'reclaim', 'launch')]
    [string] $Action,

    [Parameter(Mandatory)]
    [string] $VenvDir,

    # detect: regex matching THIS app's tray invocation (e.g. 'launcher\.py\s+tray').
    [string] $TrayMatch,

    # reclaim: comma-separated owned ports (e.g. '8445,8446'). Parsed here rather
    # than bound as [int[]] so a single cmd token survives -File arg parsing.
    [string] $Ports,

    # launch: display name used in user-facing messages.
    [string] $AppName,

    # launch: repository root / tray working directory.
    [string] $ScriptDir,

    # launch: arguments passed to python/pythonw to start the tray.
    [string] $TrayLaunch,

    # launch: restart instead of idempotent start.
    [switch] $Restart,

    # launch: optional explicit version endpoint. Blank infers first owned port.
    [string] $VersionUrl,

    # launch: bounded stale-serve detection.
    [int] $VerifyTimeoutSeconds = 30
)

# Scope every match by the holder's CommandLine containing this repo's .venv path
# (ordinal, case-insensitive) -- NEVER the process image path. On Python 3.14
# Windows venvs a venv-launched pythonw.exe re-execs the base interpreter, so the
# image path reports the shared base python while only the CommandLine still
# carries the .venv path; an image-path guard never matches the real process and
# the operation silently no-ops. (scaffold docs/windows-tray.md)
function Test-UnderVenv {
    param([string] $CommandLine)
    return $CommandLine -and
        $CommandLine.IndexOf($VenvDir, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
}

function Get-TrayProcessIds {
    if (-not $TrayMatch) { throw "detect/launch requires -TrayMatch" }
    return @(
        Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
            Where-Object { (Test-UnderVenv $_.CommandLine) -and $_.CommandLine -match $TrayMatch } |
            Select-Object -ExpandProperty ProcessId
    )
}

function Get-OwnedPorts {
    $result = @()
    if (-not $Ports) { return $result }
    foreach ($p in ($Ports -split '\s*,\s*')) {
        if (-not $p) { continue }
        $result += [int] $p
    }
    return $result
}

function Invoke-ReclaimPorts {
    foreach ($port in (Get-OwnedPorts)) {
        Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
            $ownerProcessId = $_.OwningProcess
            $cim = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $ownerProcessId) -ErrorAction SilentlyContinue
            if ($cim -and (Test-UnderVenv $cim.CommandLine)) {
                Write-Host ("Reclaiming :{0} from PID {1}" -f $port, $ownerProcessId)
                Stop-Process -Id $ownerProcessId -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

function Stop-TrayProcesses {
    param([int[]] $ProcessIds)
    foreach ($trayProcessId in $ProcessIds) {
        & taskkill /T /F /PID $trayProcessId > $null 2>&1
    }
}

function Get-PythonLauncher {
    $venvScripts = Join-Path $VenvDir "Scripts"
    $venvPythonw = Join-Path $venvScripts "pythonw.exe"
    $venvPython = Join-Path $venvScripts "python.exe"
    if (Test-Path $venvPythonw) { return $venvPythonw }
    if (Test-Path $venvPython) { return $venvPython }
    return "pythonw"
}

function Start-TrayProcess {
    if (-not $ScriptDir) { throw "launch requires -ScriptDir" }
    if (-not $TrayLaunch) { throw "launch requires -TrayLaunch" }
    $python = Get-PythonLauncher
    Write-Host ("Starting {0} tray..." -f $AppName)
    Start-Process -FilePath $python -ArgumentList $TrayLaunch -WorkingDirectory $ScriptDir -WindowStyle Hidden
}

function Get-GitHead {
    if (-not $ScriptDir) { throw "version verification requires -ScriptDir" }
    $head = (& git -C $ScriptDir rev-parse HEAD 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $head) {
        throw "restart verification requires git and a valid repository HEAD"
    }
    return ([string] $head).Trim()
}

function Resolve-VersionUrl {
    if ($VersionUrl) { return $VersionUrl }
    $ownedPorts = @(Get-OwnedPorts)
    if ($ownedPorts.Count -eq 0) {
        throw "restart verification requires -VersionUrl or at least one owned port"
    }
    return ("http://127.0.0.1:{0}/api/version" -f $ownedPorts[0])
}

function Test-GitShaMatches {
    param(
        [string] $ServedSha,
        [string] $HeadSha
    )
    if (-not $ServedSha) { return $false }
    return $HeadSha.StartsWith($ServedSha, [System.StringComparison]::OrdinalIgnoreCase) -or
        $ServedSha.StartsWith($HeadSha, [System.StringComparison]::OrdinalIgnoreCase)
}

function Wait-VersionMatchesHead {
    $url = Resolve-VersionUrl
    $head = Get-GitHead
    $deadline = (Get-Date).AddSeconds($VerifyTimeoutSeconds)
    $lastError = $null
    $lastSha = $null

    do {
        try {
            $response = Invoke-RestMethod -Uri $url -Method Get -TimeoutSec 3
            $servedSha = $response.git_sha
            if (-not $servedSha) { $servedSha = $response.gitSha }
            $lastSha = [string] $servedSha
            if (Test-GitShaMatches -ServedSha $lastSha -HeadSha $head) {
                $assetHash = $response.asset_hash
                if (-not $assetHash) { $assetHash = $response.assetHash }
                if ($assetHash) {
                    Write-Host ("Verified {0} serves git_sha {1} (asset_hash {2})." -f $url, $lastSha, $assetHash)
                } else {
                    Write-Host ("Verified {0} serves git_sha {1}." -f $url, $lastSha)
                }
                return
            }
            $lastError = "served git_sha '$lastSha', expected HEAD '$head'"
        } catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)

    throw ("restart verification failed for {0}: {1}" -f $url, $lastError)
}

switch ($Action) {
    'detect' {
        Get-TrayProcessIds
    }
    'reclaim' {
        Invoke-ReclaimPorts
    }
    'launch' {
        if (-not $AppName) { throw "launch requires -AppName" }
        $trayPids = @(Get-TrayProcessIds)
        if ($trayPids.Count -gt 0 -and -not $Restart) {
            Write-Host ("{0} tray is already running (PID: {1})." -f $AppName, ($trayPids -join " "))
            Write-Host 'Run "tray.bat --restart" to stop it and start fresh.'
            exit 0
        }

        if ($Restart) {
            if ($trayPids.Count -gt 0) {
                Write-Host ("Stopping previous {0} tray (PID: {1})..." -f $AppName, ($trayPids -join " "))
                Stop-TrayProcesses -ProcessIds $trayPids
            }
            Invoke-ReclaimPorts
            Start-Sleep -Seconds 2
        }

        Start-TrayProcess
        if ($Restart) {
            Wait-VersionMatchesHead
        }
    }
}
