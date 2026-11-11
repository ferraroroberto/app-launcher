"""The e2e gate's webapp never sweeps the owner's real repos for CI.

A GitHub refresh starts a background CI sweep over the fleet-config checkout's
repo list, read from the projects folder. The gate's hermetic config left
``fleet_config_dir`` at its default, the sibling real ``fleet-config``, so a sweep
that landed between a test's refresh and its Board read put the real repos'
names on the disposable Board. The sweep is held here at its first repo, so the
landing is deterministic instead of a matter of timing.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

from src import webapp_config
from src.board import ci_runs
from src.clients import github as github_client
from tests.e2e.fixtures import autoboot

_REAL_REPO = "real-owner-repo"
_FLEET_CONTRACT = f"""
class FleetStatusError(ValueError):
    pass


def list_status():
    return [{{"name": "{_REAL_REPO}", "status": "active"}}]


def set_status(repo, value):
    return "unchanged"
"""


def _real_machine(root: Path) -> Path:
    """A projects folder holding the checkout, a real fleet-config and one real CI repo."""
    checkout = root / "app-launcher"
    checkout.mkdir(parents=True)
    contract = root / "fleet-config" / "skills" / "_lib"
    contract.mkdir(parents=True)
    (contract / "fleet_status.py").write_text(_FLEET_CONTRACT, encoding="utf-8")
    workflows = root / _REAL_REPO / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("on: push\n", encoding="utf-8")
    return checkout


def _hold_the_sweep_then_land_it(client, monkeypatch) -> None:
    """Refresh, keep the sweep waiting at its first repo, then let it land."""
    reached, release = threading.Event(), threading.Event()

    def held_default_branch(_path):
        reached.set()
        assert release.wait(timeout=10), "the sweep was never released"
        return "main"

    monkeypatch.setattr(ci_runs, "default_branch", held_default_branch)
    monkeypatch.setattr(ci_runs, "_last_refresh_started", None)
    monkeypatch.setattr(
        github_client.subprocess, "run",
        lambda argv, **_kw: subprocess.CompletedProcess(argv, 0, stdout="[]", stderr=""),
    )
    assert client.post("/api/board/github/refresh").status_code == 200
    sweep = next((t for t in threading.enumerate() if t.name == "ci-runs-refresh"), None)
    if sweep is not None:
        assert reached.wait(timeout=10), "the sweep never reached a repo"
    release.set()
    if sweep is not None:
        sweep.join(timeout=10)
        assert not sweep.is_alive()


def test_a_sweep_landing_after_the_refresh_names_no_real_repo(
    webapp_client, monkeypatch, tmp_path
):
    client, app, _overrides = webapp_client
    client.headers["Authorization"] = "Bearer tok-gate-12345678"
    checkout = _real_machine(tmp_path / "projects")
    monkeypatch.setattr(webapp_config, "PROJECT_ROOT", checkout)
    gate_cfg = autoboot.gate_webapp_config("tok-gate-12345678", tmp_path / "state", checkout)
    cfg_file = tmp_path / "gate-webapp-config.json"
    cfg_file.write_text(json.dumps(gate_cfg), encoding="utf-8")
    app.state.webapp_config = webapp_config.load_webapp_config(cfg_file)

    _hold_the_sweep_then_land_it(client, monkeypatch)

    board = client.get("/api/board").json()
    cards = [c for col in board["columns"].values() for c in col]
    assert _REAL_REPO not in json.dumps(cards), cards
