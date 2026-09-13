# Routing decision for verify-before-ship.ps1's e2e phase (issues #568, #955).
#
# Turns scripts/classify_e2e.py's E2E_* lines into what the gate runs: the
# tier, the pytest target arguments, the browsers, and whether the run must
# hold the machine-wide dual-projection mutex. Dot-sourced by the gate and kept
# free of side effects so tests/test_verify_gate_route.py can drive it with
# fake classifier output.
#
# Fail-safe: anything the gate can't use -- no verdict, an unknown tier, a
# repeated E2E_* key, a static/full/surface verdict with no target -- runs the
# whole suite on both projections. Uncertainty never narrows.
#
# ASCII only: Windows PowerShell 5.1 reads a BOM-less script as ANSI.

function Get-E2ERoute {
    param(
        [AllowEmptyCollection()][AllowNull()][string[]]$ClassifierOutput,
        [bool]$IsCI = $false
    )

    $failSafe = {
        param($reason)
        [pscustomobject]@{
            Tier      = "full"
            Targets   = @("tests/e2e")
            Browsers  = @()
            Reason    = $reason
            Serialize = $true
        }
    }

    if ($IsCI) {
        return & $failSafe "CI always runs the full dual-projection suite"
    }

    $kv = @{}
    $repeated = @()
    foreach ($line in @($ClassifierOutput)) {
        if ($line -match '^(E2E_[A-Z_]+)=(.*)$') {
            if ($kv.ContainsKey($matches[1])) { $repeated += $matches[1] }
            $kv[$matches[1]] = $matches[2]
        }
    }
    if ($repeated) {
        return & $failSafe ("classifier repeated {0} -- defaulting to full suite (fail-safe)" -f ($repeated -join ","))
    }

    $tier = [string]$kv["E2E_TIER"]
    # A surface verdict (#955) names several targets, space-separated -- each
    # must reach pytest as its own argument ("a.py b.py" is one missing path).
    $targets = @(([string]$kv["E2E_PYTEST_TARGET"]) -split '\s+' | Where-Object { $_ })
    $browsers = @(([string]$kv["E2E_BROWSERS"]) -split ',' | Where-Object { $_ })
    $reason = [string]$kv["E2E_REASON"]

    if ($tier -eq "skip") {
        return [pscustomobject]@{
            Tier = "skip"; Targets = @(); Browsers = @(); Reason = $reason; Serialize = $false
        }
    }
    if (@("static", "full", "surface") -notcontains $tier) {
        return & $failSafe ("classifier gave no usable verdict (tier '{0}') -- defaulting to full suite (fail-safe)" -f $tier)
    }
    if ($targets.Count -eq 0) {
        return & $failSafe ("classifier gave tier '{0}' with no pytest target -- defaulting to full suite (fail-safe)" -f $tier)
    }

    # full and surface are both dual-projection runs, so both queue behind the
    # machine-wide mutex (#685); a Chromium-only static smoke run doesn't.
    return [pscustomobject]@{
        Tier      = $tier
        Targets   = $targets
        Browsers  = $browsers
        Reason    = $reason
        Serialize = ($tier -ne "static")
    }
}
