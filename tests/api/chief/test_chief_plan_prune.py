"""src/chief/plan_prune.py and POST /api/board/chief-plan/prune.

The launcher loads fleet-config's ``skills/_lib/chief_plan.py`` by path. These
tests give it a scratch checkout whose writer is a small stand-in over a plan
file under ``tmp_path``, so no test can reach the real chief-plan.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.chief import plan_prune as chief_plan_prune

_WRITER = '''
import json


def prune_plan_file(path, statuses=("merged",)):
    doc = json.loads(path.read_text())
    kept = [r for r in doc["queue"] if r["status"] not in statuses]
    pruned = len(doc["queue"]) - len(kept)
    doc["queue"] = kept
    path.write_text(json.dumps(doc))
    return pruned
'''

_REFUSING = '''
def prune_plan_file(path, statuses=("merged",)):
    raise ValueError("chief-plan.json is not valid JSON; `plan clear` resets it")
'''

_PLAN = {
    "version": 1,
    "updated_at": "2026-10-25T10:00:00Z",
    "lanes": [{"item": "#7", "repo": "a", "status": "building"}],
    "queue": [
        {"repo": "a", "ref": "#1", "title": "done one", "status": "merged"},
        {"repo": "a", "ref": "#2", "title": "in flight", "status": "building"},
        {"repo": "b", "ref": "#3", "title": "done two", "status": "merged"},
        {"repo": "b", "ref": "#4", "title": "later", "status": "queued"},
        {"repo": "b", "ref": "#5", "title": "set aside", "status": "parked"},
    ],
    "waiting_on_roberto": [{"id": "q1", "text": "Approve?"}],
}


def _checkout(root: Path, writer: str | None = _WRITER) -> Path:
    lib = root / "skills" / "_lib"
    lib.mkdir(parents=True)
    if writer is not None:
        (lib / "chief_plan.py").write_text(writer, encoding="utf-8")
    return root


def _plan_file(tmp_path: Path) -> Path:
    f = tmp_path / "chief-plan.json"
    f.write_text(json.dumps(_PLAN), encoding="utf-8")
    return f


@pytest.fixture
def prune(webapp_client, tmp_path, monkeypatch):
    """(client, plan file) with the app's fleet-config and plan file pinned to scratch paths."""
    from app.webapp import middleware
    # The passkey gate is pinned by test_prune_route_is_passkey_gated; here the
    # TestClient host counts as loopback so the route logic runs.
    monkeypatch.setattr(
        middleware, "LOOPBACK_HOSTS",
        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )
    client, app, _ = webapp_client
    cfg = app.state.webapp_config
    cfg.fleet_config_dir = str(_checkout(tmp_path / "fleet-config"))
    plan = _plan_file(tmp_path)
    cfg.chief_plan_file = str(plan)
    return client, plan


class TestLogic:
    def test_prunes_through_the_writer_and_returns_the_count(self, tmp_path):
        root = _checkout(tmp_path / "fc")
        plan = _plan_file(tmp_path)
        assert chief_plan_prune.prune_merged(str(root), str(plan)) == 2
        kept = json.loads(plan.read_text())
        assert [r["ref"] for r in kept["queue"]] == ["#2", "#4", "#5"]

    def test_writer_refusal_carries_its_message(self, tmp_path):
        root = _checkout(tmp_path / "fc", writer=_REFUSING)
        plan = _plan_file(tmp_path)
        with pytest.raises(chief_plan_prune.PlanPruneRefused, match="plan clear"):
            chief_plan_prune.prune_merged(str(root), str(plan))
        assert json.loads(plan.read_text()) == _PLAN

    @pytest.mark.parametrize(
        "make",
        [
            pytest.param(lambda r: "", id="folder-unset"),
            pytest.param(lambda r: str(_checkout(r / "none", writer=None)), id="no-module"),
            pytest.param(lambda r: str(_checkout(r / "old", writer="def update(p, f): pass")), id="old-module"),
            pytest.param(lambda r: str(_checkout(r / "bad", writer="raise RuntimeError('boom')")), id="import-raises"),
        ],
    )
    def test_unusable_contract_is_unavailable_and_leaves_the_file(self, tmp_path, make):
        plan = _plan_file(tmp_path)
        with pytest.raises(chief_plan_prune.PlanPruneUnavailable):
            chief_plan_prune.prune_merged(make(tmp_path), str(plan))
        assert json.loads(plan.read_text()) == _PLAN


class TestApi:
    def test_prune_route_is_passkey_gated(self, webapp_client):
        from app.webapp.middleware import _terminal_guard_level
        assert _terminal_guard_level("/api/board/chief-plan/prune") == "passkey"
        client, _, _ = webapp_client
        assert client.post("/api/board/chief-plan/prune").status_code == 403

    def test_clears_only_merged_rows(self, prune):
        client, plan = prune
        resp = client.post("/api/board/chief-plan/prune")
        assert resp.status_code == 200
        assert resp.json() == {"pruned": 2}
        kept = json.loads(plan.read_text())
        assert [r["ref"] for r in kept["queue"]] == ["#2", "#4", "#5"]
        assert kept["lanes"] == _PLAN["lanes"]
        assert kept["waiting_on_roberto"] == _PLAN["waiting_on_roberto"]

    def test_nothing_merged_prunes_zero(self, prune):
        client, _ = prune
        assert client.post("/api/board/chief-plan/prune").json() == {"pruned": 2}
        assert client.post("/api/board/chief-plan/prune").json() == {"pruned": 0}

    def test_old_fleet_config_is_503_and_the_file_is_untouched(self, prune, tmp_path):
        client, plan = prune
        cfg = client.app.state.webapp_config
        cfg.fleet_config_dir = str(_checkout(tmp_path / "old", writer="def update(p, f): pass"))
        resp = client.post("/api/board/chief-plan/prune")
        assert resp.status_code == 503
        assert "too old" in resp.json()["detail"]
        assert json.loads(plan.read_text()) == _PLAN

    def test_unparseable_plan_is_409_with_the_writers_message(self, prune, tmp_path):
        client, plan = prune
        cfg = client.app.state.webapp_config
        cfg.fleet_config_dir = str(_checkout(tmp_path / "refusing", writer=_REFUSING))
        resp = client.post("/api/board/chief-plan/prune")
        assert resp.status_code == 409
        assert "plan clear" in resp.json()["detail"]
        assert json.loads(plan.read_text()) == _PLAN
