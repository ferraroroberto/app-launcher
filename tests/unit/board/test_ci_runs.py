"""Red GitHub CI on the Board and the chief's ci-failed event (#2290).

GitHub is faked at ``github_client.workflow_runs`` / ``workflow_states``, the default branch at
``ci_runs.default_branch``; the mailbox and seen-runs file are ``tmp_path``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import fleet_scope, instance_role
from src.board import ci_runs
from src.chief import ci_failed

OWNER = "example-owner"
# The suite-wide conftest swaps the client's reader for a stub; the parser test needs the real one.
_REAL_WORKFLOW_STATES = ci_runs.github_client.workflow_states


def _run(run_id, conclusion, *, workflow="CI", wid=1, created="2026-11-09T14:00:00Z", status="completed"):
    return {
        "id": run_id, "name": workflow, "workflow_id": wid, "status": status,
        "conclusion": conclusion, "created_at": created, "event": "push",
        "head_sha": "9f8e7d6c5b4a39281706f5e4d3c2b1a099887766",
        "html_url": f"https://github.com/{OWNER}/app/actions/runs/{run_id}",
    }


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """Two repos with a workflow file, one without; runs per repo are mutable."""
    projects = tmp_path / "projects"
    for name in ("alpha", "beta"):
        (projects / name / ".github" / "workflows").mkdir(parents=True)
        (projects / name / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
    (projects / "gamma").mkdir(parents=True)
    monkeypatch.setattr(fleet_scope, "list_repos", lambda _d: {
        "available": True, "reason": "",
        "repos": [{"name": n, "status": fleet_scope.ACTIVE} for n in ("alpha", "beta", "gamma")],
    })
    monkeypatch.setattr(ci_runs, "default_branch", lambda _p: "main")
    runs = {"alpha": [], "beta": []}
    states = {"alpha": {}, "beta": {}}
    calls = []

    def fake_runs(owner, repo, branch):
        calls.append((owner, repo, branch))
        value = runs[repo]
        if isinstance(value, Exception):
            raise value
        return list(value)

    def fake_states(owner, repo):
        value = states[repo]
        if isinstance(value, Exception):
            raise value
        return dict(value)

    monkeypatch.setattr(ci_runs.github_client, "workflow_runs", fake_runs)
    monkeypatch.setattr(ci_runs.github_client, "workflow_states", fake_states)
    return SimpleNamespace(projects=str(projects), runs=runs, states=states, calls=calls)


def _cfg(tmp_path, **over):
    base = dict(
        chief_ci_failures=True,
        chief_inbox_dir=str(tmp_path / "chief-inbox"),
        sessions_state_file=str(tmp_path / "sessions-state.json"),
        session_host_port=54321,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def chief(monkeypatch):
    state = SimpleNamespace(live=True, canonical=True)
    monkeypatch.setattr(ci_failed, "chief_live", lambda _cfg, _kind: state.live)
    monkeypatch.setattr(instance_role, "canonical_instance", lambda: (state.canonical, "test"))
    return state


def _sweep(fleet, cfg=None):
    hook = (lambda results: ci_failed.observe(cfg, results)) if cfg else None
    return ci_runs.refresh(OWNER, "unused", fleet.projects, hook)


def _events(cfg):
    inbox = Path(cfg.chief_inbox_dir)
    if not inbox.is_dir():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(inbox.glob("*.json"))]


def test_one_call_per_repo_with_a_workflow(fleet):
    _sweep(fleet)
    assert sorted(fleet.calls) == [(OWNER, "alpha", "main"), (OWNER, "beta", "main")]


def test_red_card_with_streak_age_and_green_clears(fleet):
    fleet.runs["alpha"] = [
        _run(3, "failure", created="2026-11-09T14:00:00Z"),
        _run(2, "cancelled", created="2026-11-09T13:00:00Z"),
        _run(1, "timed_out", created="2026-11-09T12:00:00Z"),
        _run(0, "success", created="2026-11-09T11:00:00Z"),
    ]
    snap = _sweep(fleet)
    cards = ci_runs.board_cards(snap, now=datetime(2026, 11, 9, 15, tzinfo=timezone.utc))
    assert [(c["repo"], c["state"], c["run_id"], c["age_seconds"]) for c in cards] == [
        ("alpha", "failed", 3, 3 * 3600),
    ]
    assert cards[0]["run_url"].endswith("/runs/3")
    assert ci_runs.section(snap)["available"] is True

    fleet.runs["alpha"] = [_run(4, "success")] + fleet.runs["alpha"]
    assert ci_runs.board_cards(_sweep(fleet)) == []


def test_in_progress_run_keeps_the_last_result(fleet):
    fleet.runs["alpha"] = [_run(5, None, status="in_progress"), _run(4, "failure")]
    assert [c["run_id"] for c in ci_runs.board_cards(_sweep(fleet))] == [4]


def test_unreadable_repo_is_unknown_never_green(fleet, monkeypatch):
    fleet.runs["beta"] = ci_runs.github_client.GhError("HTTP 404")
    cards = ci_runs.board_cards(_sweep(fleet))
    assert [(c["repo"], c["state"], c["error"]) for c in cards] == [("beta", "unknown", "HTTP 404")]

    monkeypatch.setattr(ci_runs, "default_branch", lambda _p: None)
    assert {c["state"] for c in ci_runs.board_cards(_sweep(fleet))} == {"unknown"}


@pytest.mark.parametrize("state", ["disabled_manually", "deleted", "disabled_inactivity"])
def test_red_run_of_an_inactive_workflow_makes_no_card_and_no_event(tmp_path, fleet, chief, state):
    cfg = _cfg(tmp_path)
    _sweep(fleet, cfg)
    fleet.runs["alpha"] = [_run(10, "failure", workflow="ci", wid=7), _run(11, "failure", workflow="lint", wid=8)]
    fleet.states["alpha"] = {7: state, 8: "active"}
    snap = _sweep(fleet, cfg)
    assert [(c["workflow"], c["run_id"]) for c in ci_runs.board_cards(snap)] == [("lint", 11)]
    assert [e["run_id"] for e in _events(cfg)] == [11]


def test_inactive_only_workflow_leaves_the_repo_green(fleet):
    fleet.runs["alpha"] = [_run(10, "failure", wid=7)]
    fleet.states["alpha"] = {7: "disabled_manually"}
    snap = _sweep(fleet)
    assert snap["repos"]["alpha"]["state"] == ci_runs.STATE_GREEN
    assert ci_runs.board_cards(snap) == []


def test_unreadable_or_missing_workflow_state_keeps_the_red_card(fleet):
    fleet.runs["alpha"] = [_run(10, "failure", wid=7)]
    fleet.states["alpha"] = ci_runs.github_client.GhError("HTTP 502")
    assert [c["run_id"] for c in ci_runs.board_cards(_sweep(fleet))] == [10]

    fleet.states["alpha"] = {99: "disabled_manually"}
    assert [c["run_id"] for c in ci_runs.board_cards(_sweep(fleet))] == [10]


def test_workflow_states_read_once_and_only_when_something_is_red(fleet, monkeypatch):
    seen = []
    monkeypatch.setattr(ci_runs.github_client, "workflow_states", lambda o, r: seen.append(r) or {})
    fleet.runs["alpha"] = [_run(10, "failure")]
    fleet.runs["beta"] = [_run(20, "success")]
    _sweep(fleet)
    assert seen == ["alpha"]


def test_workflow_states_parses_the_workflows_list_and_rejects_a_bad_body(monkeypatch):
    body = {"workflows": [{"id": 7, "state": "disabled_manually"}, {"id": 8, "state": "active"}]}
    monkeypatch.setattr(ci_runs.github_client, "_run_gh", lambda args, **_k: json.dumps(body))
    assert _REAL_WORKFLOW_STATES(OWNER, "app") == {7: "disabled_manually", 8: "active"}

    for bad in ("not json", json.dumps({"total_count": 0})):
        monkeypatch.setattr(ci_runs.github_client, "_run_gh", lambda args, _b=bad, **_k: _b)
        with pytest.raises(ci_runs.github_client.GhError):
            _REAL_WORKFLOW_STATES(OWNER, "app")


def test_no_sweep_yet_is_unavailable(fleet):
    assert ci_runs.section(ci_runs.snapshot())["available"] is False


def test_first_sweep_never_backfills_then_new_failure_publishes_once(tmp_path, fleet, chief):
    cfg = _cfg(tmp_path)
    fleet.runs["alpha"] = [_run(10, "failure")]
    _sweep(fleet, cfg)
    assert _events(cfg) == []

    fleet.runs["alpha"] = [_run(11, "failure"), _run(10, "failure")]
    fleet.runs["beta"] = [_run(20, "startup_failure", workflow="Lint", wid=2)]
    _sweep(fleet, cfg)
    events = _events(cfg)
    assert [(e["repo"], e["run_id"]) for e in events] == [("alpha", 11), ("beta", 20)]
    event = next(e for e in events if e["repo"] == "beta")
    assert event["kind"] == "ci-failed" and event["v"] == 2
    assert event["workflow"] == "Lint" and event["conclusion"] == "startup_failure"
    assert event["event"] == "push" and event["failed_jobs"] == []
    assert event["launcher_sid"] is None and event["dispatch_id"] is None
    assert event["excerpt"] is None and event["excerpt_unavailable"]

    _sweep(fleet, cfg)
    assert len(_events(cfg)) == 2


def test_switch_off_publishes_nothing_and_does_not_backfill_on(tmp_path, fleet, chief):
    cfg = _cfg(tmp_path, chief_ci_failures=False)
    _sweep(fleet, cfg)
    fleet.runs["alpha"] = [_run(30, "failure")]
    _sweep(fleet, cfg)
    assert _events(cfg) == []
    cfg.chief_ci_failures = True
    _sweep(fleet, cfg)
    assert _events(cfg) == []


def test_chief_not_live_or_not_canonical_publishes_nothing(tmp_path, fleet, chief):
    cfg = _cfg(tmp_path)
    _sweep(fleet, cfg)
    chief.live = None
    fleet.runs["alpha"] = [_run(40, "failure")]
    _sweep(fleet, cfg)
    chief.live, chief.canonical = True, False
    fleet.runs["beta"] = [_run(41, "failure")]
    _sweep(fleet, cfg)
    assert _events(cfg) == []


def test_unknown_repo_is_not_seeded(tmp_path, fleet, chief):
    """A repo unreadable on the first sweep still publishes its first new red later."""
    cfg = _cfg(tmp_path)
    fleet.runs["beta"] = ci_runs.github_client.GhError("down")
    _sweep(fleet, cfg)
    assert "beta" not in ci_failed.read_seen()
    fleet.runs["beta"] = []
    _sweep(fleet, cfg)
    fleet.runs["beta"] = [_run(50, "failure")]
    _sweep(fleet, cfg)
    assert [e["run_id"] for e in _events(cfg)] == [50]


def test_cooldown_blocks_a_second_start(fleet, monkeypatch):
    started = []
    monkeypatch.setattr(ci_runs.threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: started.append(kw)))
    ci_runs.reset_ci_cache()
    assert ci_runs.start_refresh(OWNER, "x", fleet.projects, now=1000.0) is True
    assert ci_runs.start_refresh(OWNER, "x", fleet.projects, now=1001.0) is False  # in flight
    ci_runs._cache["refreshing"] = False
    assert ci_runs.start_refresh(OWNER, "x", fleet.projects, now=1010.0) is False
    assert len(started) == 1
