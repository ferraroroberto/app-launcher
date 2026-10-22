"""src/fleet_scope.py and GET/PUT /api/fleet/scope.

The launcher loads fleet-config's ``skills/_lib/fleet_status.py`` by path. These
tests give it a scratch checkout whose contract is a small stand-in over a JSON
file under ``tmp_path``, so no test can reach the real ``hooks/projects.toml``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from src import fleet_scope

_CONTRACT = '''
import json
from pathlib import Path

_STATE = Path(__file__).resolve().parents[2] / "hooks" / "state.json"


class FleetStatusError(ValueError):
    pass


def list_status():
    if not _STATE.exists():
        raise FleetStatusError("state file is missing")
    data = json.loads(_STATE.read_text())
    return [{"name": n, "status": data[n]} for n in sorted(data)]


def set_status(repo, value):
    if not _STATE.exists():
        raise FleetStatusError("state file is missing")
    data = json.loads(_STATE.read_text())
    if repo == "locked":
        raise FleetStatusError("array is not editable")
    if data[repo] == value:
        return "unchanged"
    data[repo] = value
    _STATE.write_text(json.dumps(data))
    return "changed"
'''


def _checkout(root: Path, contract: str | None = _CONTRACT, state: dict | None = None) -> Path:
    lib = root / "skills" / "_lib"
    lib.mkdir(parents=True)
    if contract is not None:
        (lib / "fleet_status.py").write_text(contract, encoding="utf-8")
    if state is not None:
        (root / "hooks").mkdir()
        (root / "hooks" / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return root


@pytest.fixture
def scope(webapp_client, tmp_path):
    """(client, state file) with the app's fleet-config folder pinned to a scratch checkout."""
    client, app, _ = webapp_client
    root = _checkout(
        tmp_path / "fleet-config",
        state={"alpha": "active", "beta": "dormant", "locked": "active"},
    )
    app.state.webapp_config.fleet_config_dir = str(root)
    return client, root / "hooks" / "state.json"


class TestLogic:
    def test_lists_repos_with_status(self, tmp_path):
        root = _checkout(tmp_path / "fc", state={"b": "dormant", "a": "active"})
        got = fleet_scope.list_repos(str(root))
        assert got["available"] is True
        assert got["repos"] == [
            {"name": "a", "status": "active"},
            {"name": "b", "status": "dormant"},
        ]

    def test_set_status_reports_changed_then_unchanged(self, tmp_path):
        root = _checkout(tmp_path / "fc", state={"a": "active"})
        assert fleet_scope.set_status(str(root), "a", "dormant") == "changed"
        assert fleet_scope.set_status(str(root), "a", "dormant") == "unchanged"
        assert fleet_scope.list_repos(str(root))["repos"][0]["status"] == "dormant"

    def test_writer_refusal_carries_its_message(self, tmp_path):
        root = _checkout(tmp_path / "fc", state={"locked": "active"})
        with pytest.raises(fleet_scope.FleetScopeError, match="not editable"):
            fleet_scope.set_status(str(root), "locked", "dormant")

    @pytest.mark.parametrize(
        "make",
        [
            pytest.param(lambda r: "", id="folder-unset"),
            pytest.param(lambda r: str(_checkout(r / "none", contract=None)), id="no-module"),
            pytest.param(lambda r: str(_checkout(r / "old", contract="def list_status(): return []")), id="old-module"),
            pytest.param(lambda r: str(_checkout(r / "bad", contract="raise RuntimeError('boom')")), id="import-raises"),
            pytest.param(lambda r: str(_checkout(r / "nofile", state=None)), id="writer-refuses-read"),
        ],
    )
    def test_unusable_contract_is_unavailable_never_empty_or_active(self, tmp_path, make):
        folder = make(tmp_path)
        got = fleet_scope.list_repos(folder)
        assert got["available"] is False
        assert got["reason"]
        assert got["repos"] == []
        with pytest.raises(fleet_scope.FleetScopeError):
            fleet_scope.set_status(folder, "a", "dormant")

    def test_failure_warns_once_across_repeated_reads(self, tmp_path, caplog):
        fleet_scope._logged_failures.clear()
        root = str(_checkout(tmp_path / "bad", contract="raise RuntimeError('boom')"))
        with caplog.at_level(logging.WARNING, logger="src.fleet_scope"):
            for _ in range(3):
                fleet_scope.list_repos(root)
        assert len([r for r in caplog.records if "fleet scope" in r.getMessage()]) == 1


class TestApi:
    def test_get_lists_every_repo(self, scope):
        client, _ = scope
        body = client.get("/api/fleet/scope").json()
        assert body["available"] is True
        assert {r["name"]: r["status"] for r in body["repos"]} == {
            "alpha": "active", "beta": "dormant", "locked": "active",
        }

    def test_get_without_checkout_says_unavailable(self, webapp_client, tmp_path):
        client, app, _ = webapp_client
        app.state.webapp_config.fleet_config_dir = str(tmp_path / "absent")
        body = client.get("/api/fleet/scope").json()
        assert body["available"] is False and body["reason"] and body["repos"] == []

    def test_put_flips_and_repeat_is_unchanged(self, scope):
        client, state = scope
        first = client.put("/api/fleet/scope/alpha", json={"status": "dormant"})
        assert first.status_code == 200
        assert first.json()["outcome"] == "changed"
        assert json.loads(state.read_text())["alpha"] == "dormant"
        second = client.put("/api/fleet/scope/alpha", json={"status": "dormant"}).json()
        assert second["outcome"] == "unchanged"
        assert {r["name"]: r["status"] for r in second["repos"]}["alpha"] == "dormant"

    def test_put_unknown_repo_is_404_and_invalid_status_is_422(self, scope):
        client, state = scope
        before = state.read_text()
        unknown = client.put("/api/fleet/scope/ghost", json={"status": "dormant"})
        invalid = client.put("/api/fleet/scope/alpha", json={"status": "paused"})
        missing = client.put("/api/fleet/scope/alpha", json={})
        assert unknown.status_code == 404 and "ghost" in unknown.json()["detail"]
        assert invalid.status_code == 422 and missing.status_code == 422
        assert unknown.json()["detail"] != invalid.json()["detail"]
        assert state.read_text() == before

    def test_put_writer_refusal_is_409_with_its_message(self, scope):
        client, _ = scope
        resp = client.put("/api/fleet/scope/locked", json={"status": "dormant"})
        assert resp.status_code == 409
        assert "not editable" in resp.json()["detail"]

    def test_put_without_checkout_is_503(self, webapp_client, tmp_path):
        client, app, _ = webapp_client
        app.state.webapp_config.fleet_config_dir = str(tmp_path / "absent")
        resp = client.put("/api/fleet/scope/alpha", json={"status": "dormant"})
        assert resp.status_code == 503
