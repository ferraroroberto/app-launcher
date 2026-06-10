#Requires -Version 5.1
<#
    Canonical tray detect + port-reclaim helper.

    CANONICAL, VENDORED VERBATIM from project-scaffolding. Do **not** edit this
    file per-app -- it is byte-identical across every fleet tray so a fix made
    once in the scaffold re-propagates everywhere by re-copying. App-specific
    values (the .venv path, the tray-match regex, the owned ports) are passed in
    as arguments by tray.bat, never hardcoded here -- that is what keeps the file
    identical. Lives alongside the other vendored tray primitive,
    app/tray/single_instance.py. Full reasoning: scaffold docs/windows-tray.md +
    project-scaffolding#29 / #36 / #54.

    Why a committed .ps1 instead of an inline `powershell.exe -Command "..."` in
    the batch (project-scaffolding#54): the detection/reclaim logic needs a CIM
    -Filter string with doubled single quotes nested inside the batch's double
    quotes, all inside a `for /f usebackq` backtick block. Launched
    non-interactively through a nested shell (Git Bash -> `cmd /c "tray.bat
    --restart"`, or a finisher skill's Bash tool), that nested quoting is mangled
    and the inline command returns **empty** -- neither the running tray nor the
    port holder is found, so `--restart` silently degrades to a plain start that
    adopts the stale webapp and *reports success*. Shelling to a `-File` script
    removes every nested quote from the batch line, so detection/reclaim behave
    identically whether tray.bat is run from an interactive console, the Startup
    folder, or a non-interactive agent shell.

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
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('detect', 'reclaim')]
    [string] $Action,

    [Parameter(Mandatory)]
    [string] $VenvDir,

    # detect: regex matching THIS app's tray invocation (e.g. 'launcher\.py\s+tray').
    [string] $TrayMatch,

    # reclaim: comma-separated owned ports (e.g. '8445,8446'). Parsed here rather
    # than bound as [int[]] so a single cmd token survives -File arg parsing.
    [string] $Ports
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

switch ($Action) {
    'detect' {
        if (-not $TrayMatch) { throw "detect requires -TrayMatch" }
        Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
            Where-Object { (Test-UnderVenv $_.CommandLine) -and $_.CommandLine -match $TrayMatch } |
            Select-Object -ExpandProperty ProcessId
    }
    'reclaim' {
        if (-not $Ports) { return }
        foreach ($p in ($Ports -split '\s*,\s*')) {
            if (-not $p) { continue }
            $port = [int] $p
            Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
                $opid = $_.OwningProcess
                $cim = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $opid) -ErrorAction SilentlyContinue
                if ($cim -and (Test-UnderVenv $cim.CommandLine)) {
                    Write-Host ("Reclaiming :{0} from PID {1}" -f $port, $opid)
                    Stop-Process -Id $opid -Force -ErrorAction SilentlyContinue
                }
            }
        }
    }
}
