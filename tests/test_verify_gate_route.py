"""verify-before-ship.ps1's e2e routing decision (issues #685, #955).

`scripts/e2e-gate-route.ps1` turns the classifier's E2E_* lines into the pytest
arguments the gate runs and whether that run takes the machine-wide
dual-projection mutex. Driven here through real Windows PowerShell 5.1 with
fake classifier output, plus one end-to-end case on the real classifier. Every
case goes through a single PowerShell process so the suite pays one startup.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ROUTE_PS1 = REPO_ROOT / "scripts" / "e2e-gate-route.ps1"
GATE_PS1 = REPO_ROOT / "scripts" / "verify-before-ship.ps1"
POWERSHELL = (pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows"))
              / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the gate is a Windows PowerShell script")

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_FULL = ("full", ["tests/e2e"], [], True)

_CASES: dict[str, dict] = {
    "surface": {"lines": [
        "E2E_TIER=surface",
        "E2E_BROWSERS=",
        "E2E_PYTEST_TARGET=tests/e2e/test_board_tab.py tests/e2e/test_smoke.py",
        "E2E_REASON=surface board: app/webapp/static/board.js",
        "E2E_SURFACE=board",
    ]},
    "full": {"lines": ["E2E_TIER=full", "E2E_BROWSERS=", "E2E_PYTEST_TARGET=tests/e2e", "E2E_REASON=webapp: x"]},
    "static": {"lines": ["E2E_TIER=static", "E2E_BROWSERS=chromium",
                         "E2E_PYTEST_TARGET=tests/e2e/test_smoke.py", "E2E_REASON=static-asset: x.png"]},
    "skip": {"lines": ["E2E_TIER=skip", "E2E_BROWSERS=", "E2E_PYTEST_TARGET=", "E2E_REASON=docs: README.md"]},
    # A full verdict, on CI: the CI branch must override it downward (#1041).
    "ci": {"lines": ["E2E_TIER=full", "E2E_BROWSERS=", "E2E_PYTEST_TARGET=tests/e2e",
                     "E2E_REASON=webapp: x"], "ci": True},
    # Unusable classifier output -> whole suite.
    "no-output": {"lines": []},
    "no-verdict": {"lines": ["Traceback (most recent call last):", "tomllib.TOMLDecodeError"]},
    "unknown-tier": {"lines": ["E2E_TIER=bogus", "E2E_PYTEST_TARGET=tests/e2e/test_smoke.py"]},
    "no-target": {"lines": ["E2E_TIER=surface", "E2E_PYTEST_TARGET=", "E2E_SURFACE=board"]},
    "repeated-key": {"lines": ["E2E_TIER=surface", "E2E_PYTEST_TARGET=tests/e2e/test_smoke.py", "E2E_TIER=skip"]},
}

# Worker plans (#1231): (route case, E2E_WORKERS value) -> Get-E2EWorkerArgs.
_WORKER_CASES: dict[str, tuple[str, str]] = {
    "full-default": ("full", ""),
    "surface-default": ("surface", ""),
    "full-six": ("full", "6"),
    "full-one": ("full", "1"),
    "full-zero": ("full", "0"),
    "static-default": ("static", ""),
    "skip-default": ("skip", ""),
    "full-word": ("full", "four"),
    "full-negative": ("full", "-2"),
}

_HARNESS = r"""
$ErrorActionPreference = 'Stop'
. $env:E2E_ROUTE_PS1
$out = @{}
foreach ($case in ($env:E2E_FAKE_CASES | ConvertFrom-Json)) {
    $r = Get-E2ERoute -ClassifierOutput @($case.lines | Where-Object { $_ -ne $null }) -IsCI ([bool]$case.ci)
    $out[$case.name] = [pscustomobject]@{
        Tier = $r.Tier; Targets = @($r.Targets); Browsers = @($r.Browsers)
        Reason = $r.Reason; Serialize = $r.Serialize
    }
}
$workers = @{}
foreach ($w in ($env:E2E_WORKER_CASES | ConvertFrom-Json)) {
    $r = Get-E2ERoute -ClassifierOutput @($w.lines) -IsCI $false
    try {
        $plan = Get-E2EWorkerArgs -Route $r -Setting $w.setting
        $workers[$w.name] = [pscustomobject]@{ Workers = $plan.Workers; Args = (@($plan.Args) -join ' '); Error = '' }
    } catch {
        $workers[$w.name] = [pscustomobject]@{ Workers = 0; Args = ''; Error = $_.Exception.Message }
    }
}
$out['__workers__'] = $workers
$out | ConvertTo-Json -Depth 4 -Compress
"""


@pytest.fixture(scope="module")
def routes() -> dict[str, dict]:
    """Every case's route, from one PowerShell run. Includes "real": the real
    classifier's verdict on a Board-only diff."""
    classify = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "classify_e2e.py"), "app/webapp/static/board.js"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, timeout=60, check=True,
    )
    cases = [{"name": name, "lines": c["lines"], "ci": c.get("ci", False)} for name, c in _CASES.items()]
    cases.append({"name": "real", "lines": classify.stdout.splitlines(), "ci": False})
    # The harness ships as -EncodedCommand and reads cases from the environment,
    # so nothing needs Windows command-line or PowerShell quoting.
    out = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", base64.b64encode(_HARNESS.encode("utf-16-le")).decode("ascii")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=dict(os.environ, E2E_ROUTE_PS1=str(ROUTE_PS1), E2E_FAKE_CASES=json.dumps(cases),
                 E2E_WORKER_CASES=json.dumps([
                     {"name": name, "lines": _CASES[route]["lines"], "setting": setting}
                     for name, (route, setting) in _WORKER_CASES.items()])),
        stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, timeout=120,
    )
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout)
    _WORKER_PLANS.update(result.pop("__workers__"))
    # ConvertTo-Json can flatten a one-element array in Windows PowerShell 5.1.
    for route in result.values():
        for key in ("Targets", "Browsers"):
            if route[key] is None:
                route[key] = []
            elif not isinstance(route[key], list):
                route[key] = [route[key]]
    return result


# Filled by the `routes` fixture from the same PowerShell run.
_WORKER_PLANS: dict[str, dict] = {}


def _shape(route: dict) -> tuple:
    return route["Tier"], route["Targets"], route["Browsers"], route["Serialize"]


def test_surface_targets_split_into_separate_arguments_and_serialize(routes: dict) -> None:
    assert _shape(routes["surface"]) == (
        "surface", ["tests/e2e/test_board_tab.py", "tests/e2e/test_smoke.py"], [], True)


def test_full_tier_serializes(routes: dict) -> None:
    assert _shape(routes["full"]) == _FULL


def test_static_tier_runs_chromium_smoke_without_the_mutex(routes: dict) -> None:
    assert _shape(routes["static"]) == ("static", ["tests/e2e/test_smoke.py"], ["chromium"], False)


def test_skip_tier_runs_nothing(routes: dict) -> None:
    assert _shape(routes["skip"]) == ("skip", [], [], False)


def test_ci_skips_the_browser_suite_whatever_the_diff_says(routes: dict) -> None:
    """CI runs the clean-machine half only (#1041).

    Driven with a *full* classifier verdict, so this pins the override: on CI
    the browser suite is skipped however loudly the diff asks for it. The
    local gate is the contract for that suite.
    """
    assert _shape(routes["ci"]) == ("skip", [], [], False)
    assert "clean-machine" in routes["ci"]["Reason"]


@pytest.mark.parametrize("case", ["no-output", "no-verdict", "unknown-tier", "no-target", "repeated-key"])
def test_unusable_classifier_output_runs_the_whole_suite(routes: dict, case: str) -> None:
    assert _shape(routes[case]) == _FULL
    assert "fail-safe" in routes[case]["Reason"]


def test_real_classifier_surface_verdict_reaches_pytest_as_separate_targets(routes: dict) -> None:
    """End to end: the real classifier on a Board-only diff, through the helper."""
    real = routes["real"]
    assert real["Tier"] == "surface"
    assert len(real["Targets"]) > 1
    assert all(" " not in t and (REPO_ROOT / t).is_file() for t in real["Targets"])
    assert real["Serialize"] is True


def test_gate_runs_the_helper_verdict() -> None:
    """The gate must dot-source the helper and pass its targets, mutex flag
    and worker arguments through."""
    gate = GATE_PS1.read_text(encoding="utf-8")
    assert "e2e-gate-route.ps1" in gate
    assert "Get-E2ERoute" in gate
    assert "$route.Serialize" in gate
    assert "@($route.Targets)" in gate
    assert "Get-E2EWorkerArgs -Route $route -Setting $env:E2E_WORKERS" in gate
    assert "@($workerPlan.Args)" in gate


def test_dual_projection_tiers_run_on_four_workers_by_default(routes: dict) -> None:
    """#1231: full and surface runs spread over 4 xdist workers, loadgroup so
    the real-agent module's xdist_group stays on one worker."""
    for case in ("full-default", "surface-default"):
        plan = _WORKER_PLANS[case]
        assert (plan["Workers"], plan["Args"], plan["Error"]) == (
            4, "-n 4 --dist loadgroup", ""), (case, plan)


def test_e2e_workers_overrides_the_count_and_one_or_zero_is_serial(routes: dict) -> None:
    assert _WORKER_PLANS["full-six"]["Args"] == "-n 6 --dist loadgroup"
    for case in ("full-one", "full-zero"):
        assert (_WORKER_PLANS[case]["Workers"], _WORKER_PLANS[case]["Args"]) == (1, ""), case


def test_single_projection_and_skipped_tiers_stay_serial(routes: dict) -> None:
    """The static Chromium smoke is too short to repay a worker's own boot."""
    for case in ("static-default", "skip-default"):
        assert (_WORKER_PLANS[case]["Workers"], _WORKER_PLANS[case]["Args"]) == (1, ""), case


def test_a_bad_worker_count_fails_loudly_instead_of_guessing(routes: dict) -> None:
    for case in ("full-word", "full-negative"):
        assert "E2E_WORKERS must be a whole number" in _WORKER_PLANS[case]["Error"], case


def test_helper_is_ascii_for_windows_powershell_5_1() -> None:
    ROUTE_PS1.read_bytes().decode("ascii")
