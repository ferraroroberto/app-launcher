# Routing decision for verify-before-ship.ps1's e2e phase (issues #568, #955).
#
# Turns scripts/classify_e2e.py's E2E_* lines into what the gate runs: the
# tier, the pytest target arguments, the browsers, and whether the run must
# hold the machine-wide dual-projection mutex -- plus, from Get-E2EWorkerArgs
# at the bottom, how many parallel workers it runs on. Dot-sourced by the gate
# and kept free of side effects so tests/test_verify_gate_route.py can drive it
# with fake classifier output.
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

    # CI runs the clean-machine half of this gate and nothing else (#1041).
    # The browser suite there was a second run of a gate that had already
    # passed locally before the PR was opened -- the same script, the same
    # tests -- and it needed three separate timeout widenings
    # (E2E_LOG_POLL_DEADLINE_MS, E2E_STOP_OVERLAY_HIDE_MS,
    # E2E_REAL_AGENT_ECHO_MS) purely to survive the hosted runner, which is
    # the suite fighting the environment rather than testing the code. What
    # only CI can do -- a fresh .venv from requirements.txt on a machine that
    # has never seen this repo, run against committed files alone -- lives
    # entirely in byte-compile plus the non-e2e suite, and that is what it
    # now runs. The local gate stays the contract for the browser suite.
    #
    # Deliberately decided here, not in the workflow: this is a routing
    # decision, it is the only place that already knows it is on CI, and
    # tests/test_verify_gate_route.py pins it.
    if ($IsCI) {
        return [pscustomobject]@{
            Tier      = "skip"
            Targets   = @()
            Browsers  = @()
            Reason    = "CI runs the clean-machine half only -- browser suite is the local gate's job (#1041)"
            Serialize = $false
        }
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

# Browser-suite parallelism (#1231, step 1 of #1220). THE one setting for how
# many pytest-xdist workers a dual-projection run (full / surface) uses:
# $E2EDefaultWorkers, overridable per run with the E2E_WORKERS env var. 0 or 1
# runs serially, exactly as before #1231. Parallelism changes when tests run,
# never which: every node still runs on both projections.
#
# The static tier (Chromium smoke, ~15 s) stays serial -- a worker boots its
# own webapp + session-host (~4 s each), which would cost more than it saves.
# --dist loadgroup spreads tests one at a time, except that a module marked
# xdist_group (test_terminal_reconnect.py: the real-agent and reconnect pins)
# keeps its tests on one worker so they never overlap each other. All the
# workers run inside the gate's one machine-wide dual-projection mutex (#685),
# which serialises gates across checkouts, not the workers within one.
$E2EDefaultWorkers = 4

function Get-E2EWorkerArgs {
    param(
        [Parameter(Mandatory = $true)]$Route,
        [AllowEmptyString()][AllowNull()][string]$Setting
    )

    $workers = $E2EDefaultWorkers
    if ($Setting) {
        $parsed = 0
        if (-not [int]::TryParse($Setting.Trim(), [ref]$parsed) -or $parsed -lt 0) {
            throw ("E2E_WORKERS must be a whole number of workers (0 or 1 = serial), got '{0}'" -f $Setting)
        }
        $workers = $parsed
    }
    if (-not $Route.Serialize -or $workers -le 1) {
        return [pscustomobject]@{ Workers = 1; Args = @() }
    }
    return [pscustomobject]@{
        Workers = $workers
        Args    = @("-n", [string]$workers, "--dist", "loadgroup")
    }
}
