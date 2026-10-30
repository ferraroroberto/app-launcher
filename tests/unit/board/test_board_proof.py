"""Done lane actions: the per-repo .proof store, the closing-PR link and GET /api/board/proof."""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from src.board import proof_reports
from src.clients import github as github_client

REPORT = "<!doctype html><html><body><h1>walkthrough</h1></body></html>"


@pytest.fixture
def _bypass_gate(monkeypatch):
    """The test client counts as the PC itself, as the outbox route tests do."""
    from app.webapp import middleware
    monkeypatch.setattr(
        middleware, "LOOPBACK_HOSTS",
        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )


def _write_proof(projects, repo, pr, body=REPORT):
    """One PR's report and index row, as ``ui_proof build`` writes them."""
    root = projects / repo / ".proof"
    (root / str(pr)).mkdir(parents=True, exist_ok=True)
    (root / str(pr) / "index.html").write_text(body, encoding="utf-8")
    index_path = root / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    index[str(pr)] = {"title": "t", "branch": "b"}
    index_path.write_text(json.dumps(index), encoding="utf-8")
    return root


@pytest.fixture
def store(tmp_path, webapp_client):
    """The configured projects dir, holding demo-app#21's report in its own .proof."""
    projects = tmp_path / "projects"
    _write_proof(projects, "demo-app", 21)
    _client, app, _overrides = webapp_client
    app.state.webapp_config.projects_dir = str(projects)
    return str(projects)


def _card(number, pr=None, visual_proof=None, repo="demo-app"):
    return {"kind": "issue", "repo": repo, "number": number, "pr": pr, "visual_proof": visual_proof}


def test_a_stored_report_marks_its_issue_available(store):
    (card,) = proof_reports.annotate([_card(5, pr=21)], store)
    assert (card["proof"], card["proof_pr"]) == ("available", 21)
    assert "visual_proof" not in card


def test_a_pr_that_owes_proof_and_has_none_is_missing(store):
    for owed in ("missing", "stale", "failing", "unknown"):
        (card,) = proof_reports.annotate([_card(6, pr=22, visual_proof=owed)], store)
        assert (card["proof"], card["proof_pr"]) == ("missing", 22), owed


def test_an_index_miss_alone_says_nothing_about_a_non_ui_pr(store):
    for pr, line in ((23, None), (23, "fresh"), (23, "n/a"), (None, "missing")):
        (card,) = proof_reports.annotate([_card(7, pr=pr, visual_proof=line)], store)
        assert "proof" not in card, (pr, line)


def test_the_stored_report_wins_over_a_missing_line(store):
    (card,) = proof_reports.annotate([_card(5, pr=21, visual_proof="missing")], store)
    assert card["proof"] == "available"


def test_an_index_row_without_its_file_is_not_available(store):
    (Path(store) / "demo-app" / ".proof" / "21" / "index.html").unlink()
    assert proof_reports.report_path("demo-app", 21, store) is None


def test_no_store_at_all_is_an_empty_index(tmp_path):
    projects = str(tmp_path / "absent")
    assert proof_reports.report_path("demo-app", 21, projects) is None
    assert proof_reports.report_path("demo-app", 21, "") is None
    (card,) = proof_reports.annotate([_card(5, pr=21, visual_proof="missing")], projects)
    assert card["proof"] == "missing"


def test_the_lookup_is_jailed_to_the_repos_proof_folder(store, tmp_path):
    (tmp_path / "secret.html").write_text("x", encoding="utf-8")
    for repo in ("..", "../x", "a/b", "a\b", "", ".", "demo-app/../.."):
        assert proof_reports.report_path(repo, 21, store) is None, repo
    assert proof_reports.report_path("demo-app", True, store) is None
    assert proof_reports.report_path("demo-app", -1, store) is None


def test_a_row_for_another_pr_never_serves_this_ones_page(store):
    root = Path(store) / "demo-app" / ".proof"
    index = json.loads((root / "index.json").read_text(encoding="utf-8"))
    index["30"] = {}
    (root / "index.json").write_text(json.dumps(index), encoding="utf-8")
    assert proof_reports.report_path("demo-app", 30, store) is None   # no 30/index.html


def test_a_page_that_resolves_outside_the_proof_folder_is_refused(store, tmp_path):
    root = Path(store) / "demo-app" / ".proof"
    outside = tmp_path / "elsewhere" / "40"   # named like the PR, so only the jail refuses it
    outside.mkdir(parents=True)
    (outside / "index.html").write_text("secret", encoding="utf-8")
    link = root / "40"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        import _winapi   # a junction needs no privilege; this branch only runs on Windows
        _winapi.CreateJunction(str(outside), str(link))
    index = json.loads((root / "index.json").read_text(encoding="utf-8"))
    index["40"] = {}
    (root / "index.json").write_text(json.dumps(index), encoding="utf-8")
    assert proof_reports.report_path("demo-app", 40, store) is None


def test_an_oversized_report_is_refused(store, monkeypatch):
    monkeypatch.setattr(proof_reports, "MAX_REPORT_BYTES", 5)
    assert proof_reports.report_path("demo-app", 21, store) is None


def test_the_index_is_reread_when_it_changes(store):
    assert proof_reports.report_path("demo-app", 22, store) is None
    _write_proof(Path(store), "demo-app", 22)
    assert proof_reports.report_path("demo-app", 22, store) is not None


def test_each_repo_reads_its_own_store(store):
    _write_proof(Path(store), "other-app", 21, body="<p>other</p>")
    mine = proof_reports.report_path("demo-app", 21, store)
    theirs = proof_reports.report_path("other-app", 21, store)
    assert mine != theirs and theirs.read_text(encoding="utf-8") == "<p>other</p>"


# --- the closing-PR link -----------------------------------------------------

def _pr(number, body, repo="demo-app"):
    return {"repository": {"nameWithOwner": f"o/{repo}"}, "number": number, "body": body,
            "url": f"https://github.com/o/{repo}/pull/{number}"}


def _issue(number, repo="demo-app"):
    return {"repository": {"nameWithOwner": f"o/{repo}"}, "number": number, "title": "t",
            "url": "u", "updatedAt": "2026-07-01T10:00:00Z", "closedAt": "2026-07-01T09:00:00Z",
            "labels": []}


def _gh_answering(monkeypatch, *, issues, prs):
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        rows = prs if "prs" in argv[:3] else issues
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(rows), stderr="")

    monkeypatch.setattr(github_client.subprocess, "run", fake_run)
    return seen


def test_closing_keywords_link_an_issue_to_its_pr(monkeypatch):
    _gh_answering(monkeypatch, issues=[], prs=[
        _pr(21, "Summary\n\nCloses #5\nVISUAL_PROOF=fresh"),
        _pr(22, "Fixes #6 and resolved #7. VISUAL_PROOF=missing (layout_lint not vendored)"),
        _pr(23, "Part of #8, progresses #9"),
        _pr(24, "closes #5"),
        _pr(25, "Closes #5", repo="other-app"),
    ])
    got = github_client.search_merged_on("o", date(2026, 7, 1))
    pull = "https://github.com/o/{}/pull/{}".format
    assert got[("demo-app", 5)] == {"pr": 24, "pr_url": pull("demo-app", 24), "visual_proof": None}  # the later PR wins
    assert got[("demo-app", 6)] == {"pr": 22, "pr_url": pull("demo-app", 22), "visual_proof": "missing"}
    assert got[("demo-app", 7)] == {"pr": 22, "pr_url": pull("demo-app", 22), "visual_proof": "missing"}
    assert got[("other-app", 5)] == {"pr": 25, "pr_url": pull("other-app", 25), "visual_proof": None}
    assert ("demo-app", 8) not in got and ("demo-app", 9) not in got


def test_search_done_on_carries_each_issues_pr(monkeypatch):
    seen = _gh_answering(monkeypatch, issues=[_issue(5), _issue(6)],
                         prs=[_pr(21, "Closes #5\nVISUAL_PROOF=missing")])
    done = github_client.search_done_on("o", date(2026, 7, 1))
    by_number = {c["number"]: c for c in done}
    assert (by_number[5]["pr"], by_number[5]["visual_proof"]) == (21, "missing")
    assert by_number[5]["pr_url"] == "https://github.com/o/demo-app/pull/21"
    assert (by_number[6]["pr"], by_number[6]["visual_proof"], by_number[6]["pr_url"]) == (None, None, "")
    assert len(seen) == 2                                   # one issues search, one PR search


def test_a_failed_pr_search_keeps_done_and_claims_no_proof_state(monkeypatch):
    def fake_run(argv, **kwargs):
        if "prs" in argv[:3]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="rate limited")
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps([_issue(5)]), stderr="")

    monkeypatch.setattr(github_client.subprocess, "run", fake_run)
    (card,) = github_client.search_done_on("o", date(2026, 7, 1))
    assert (card["number"], card["pr"], card["visual_proof"]) == (5, None, None)


# --- the routes --------------------------------------------------------------

def test_the_report_is_served_with_a_scriptless_policy(webapp_client, _bypass_gate, store):
    client, _app, _overrides = webapp_client
    res = client.get("/api/board/proof/demo-app/21")
    assert res.status_code == 200 and res.text == REPORT
    assert res.headers["content-type"].startswith("text/html")
    assert res.headers["cache-control"] == "no-store"
    assert res.headers["x-content-type-options"] == "nosniff"
    csp = res.headers["content-security-policy"]
    assert csp.startswith("sandbox;") and "default-src 'none'" in csp


def test_a_missing_report_is_a_404(webapp_client, _bypass_gate, store):
    client, _app, _overrides = webapp_client
    assert client.get("/api/board/proof/demo-app/99").status_code == 404
    assert client.get("/api/board/proof/..%2F/21").status_code == 404
    assert client.get("/api/board/proof/demo-app/notanumber").status_code == 422


def test_off_the_tailnet_the_report_is_not_reachable(webapp_client, store):
    client, _app, _overrides = webapp_client
    assert client.get("/api/board/proof/demo-app/21").status_code == 403


def test_the_report_route_is_a_passkey_route():
    from app.webapp import middleware
    assert middleware._terminal_guard_level("/api/board/proof/demo-app/21") == "passkey"


def test_api_done_annotates_the_days_cards(webapp_client, store, monkeypatch):
    client, _app, _overrides = webapp_client
    _gh_answering(monkeypatch, issues=[_issue(5), _issue(6), _issue(7)], prs=[
        _pr(21, "Closes #5"), _pr(22, "Closes #6\nVISUAL_PROOF=missing"), _pr(23, "Closes #7")])
    items = client.get("/api/board/done?days_back=2").json()["items"]
    assert {i["number"]: i.get("proof") for i in items} == {5: "available", 6: "missing", 7: None}
    assert {i["number"]: i.get("proof_pr") for i in items}[5] == 21
