"""Dismissing a red CI card on the Board.

The store (``src.board.ci_dismissed``), its effect on ``ci_runs.board_cards``
and the ``POST /api/board/ci/dismiss`` route. Snapshots are built by hand: the
sweep itself is covered by test_ci_runs.py.
"""

from __future__ import annotations

import json

from src.board import ci_dismissed, ci_runs


def _snap(*red: tuple[str, str, int]) -> dict:
    repos: dict = {}
    for repo, workflow, run_id in red:
        repos.setdefault(repo, {"state": ci_runs.STATE_RED, "reason": "", "red": []})
        repos[repo]["red"].append({
            "workflow": workflow, "run_id": run_id, "run_url": f"https://example.test/{run_id}",
            "conclusion": "failure", "red_since": "2026-11-09T12:00:00Z",
        })
    return {"fetched_at": "2026-11-09T12:00:00Z", "repos": repos, "error": None, "refreshing": False}


def test_dismiss_round_trips_and_is_idempotent(tmp_path):
    path = tmp_path / "dismissed.json"
    assert ci_dismissed.read_dismissed(path) == {}
    ci_dismissed.dismiss("alpha", "ci", 10, path)
    ci_dismissed.dismiss("alpha", "ci", 10, path)
    ci_dismissed.dismiss("alpha", "lint", 11, path)
    ci_dismissed.dismiss("beta", "ci", 12, path)
    assert ci_dismissed.read_dismissed(path) == {
        "alpha": {"ci": [10], "lint": [11]},
        "beta": {"ci": [12]},
    }


def test_a_workflow_keeps_only_its_newest_dismissals(tmp_path):
    path = tmp_path / "dismissed.json"
    total = ci_dismissed.MAX_PER_WORKFLOW + 3
    for run_id in range(total):
        ci_dismissed.dismiss("alpha", "ci", run_id, path)
    kept = ci_dismissed.read_dismissed(path)["alpha"]["ci"]
    assert len(kept) == ci_dismissed.MAX_PER_WORKFLOW
    assert kept[-1] == total - 1


def test_a_corrupt_file_dismisses_nothing(tmp_path):
    path = tmp_path / "dismissed.json"
    path.write_text("{not json", encoding="utf-8")
    assert ci_dismissed.read_dismissed(path) == {}
    path.write_text(json.dumps({
        "alpha": ["ci"], "beta": {"ci": "10"}, "gamma": {"ci": [True, None, "x"]},
    }), encoding="utf-8")
    assert ci_dismissed.read_dismissed(path) == {}


def test_a_dismissed_run_makes_no_card_and_a_newer_red_run_does():
    snap = _snap(("alpha", "ci", 10), ("alpha", "lint", 11))
    assert [c["run_id"] for c in ci_runs.board_cards(snap)] == [10, 11]

    ci_dismissed.dismiss("alpha", "ci", 10)
    assert [c["run_id"] for c in ci_runs.board_cards(snap)] == [11]

    newer = _snap(("alpha", "ci", 13), ("alpha", "lint", 11))
    assert [(c["workflow"], c["run_id"]) for c in ci_runs.board_cards(newer)] == [
        ("ci", 13), ("lint", 11),
    ]


def test_dismissal_is_per_repo_and_workflow():
    ci_dismissed.dismiss("alpha", "ci", 10)
    snap = _snap(("alpha", "ci", 10), ("beta", "ci", 10), ("alpha", "lint", 10))
    assert [(c["repo"], c["workflow"]) for c in ci_runs.board_cards(snap)] == [
        ("alpha", "lint"), ("beta", "ci"),
    ]


def test_an_unreadable_repo_card_cannot_be_dismissed_away():
    ci_dismissed.dismiss("alpha", "ci", 10)
    snap = {"fetched_at": "x", "repos": {"alpha": {"state": ci_runs.STATE_UNKNOWN, "reason": "HTTP 404", "red": []}}}
    assert [c["state"] for c in ci_runs.board_cards(snap)] == ["unknown"]


def test_route_records_the_run_and_the_card_goes(webapp_client):
    client, _app, _overrides = webapp_client
    ci_runs._cache.update(_snap(("alpha", "ci", 10)))
    body = {"repo": "alpha", "workflow": "ci", "run_id": 10}

    resp = client.post("/api/board/ci/dismiss", json=body)

    assert resp.status_code == 200
    assert resp.json() == {"dismissed": True, **body}
    assert ci_dismissed.read_dismissed() == {"alpha": {"ci": [10]}}
    assert ci_runs.board_cards(ci_runs.snapshot()) == []


def test_route_refuses_what_is_not_a_current_red_run(webapp_client):
    client, _app, _overrides = webapp_client
    ci_runs._cache.update(_snap(("alpha", "ci", 10)))

    for body in (
        {"repo": "alpha", "workflow": "ci", "run_id": 9},      # a run since replaced
        {"repo": "alpha", "workflow": "lint", "run_id": 10},   # no such workflow
        {"repo": "nope", "workflow": "ci", "run_id": 10},      # no such repo
    ):
        assert client.post("/api/board/ci/dismiss", json=body).status_code == 409, body
    for body in (
        {"repo": "alpha", "workflow": "ci"},
        {"repo": "alpha", "workflow": "ci", "run_id": "10"},
        {"repo": "alpha", "workflow": "ci", "run_id": True},
        {"repo": 1, "workflow": "ci", "run_id": 10},
    ):
        assert client.post("/api/board/ci/dismiss", json=body).status_code == 400, body
    assert ci_dismissed.read_dismissed() == {}
