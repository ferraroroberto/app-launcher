"""Owner checks on the Board: the list cache, the marks, the answers and the route.

fleet-config's owner_check CLI is faked at ``owner_checks._run_cli``, so no
test reaches GitHub. The contract under test: a read that did not land is
unknown (never an empty "none"), a failed refresh keeps the older items, and
each answer does exactly its one write.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.board import owner_checks

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)

ITEMS = [
    {"repo": "demo", "number": 7, "ref": "demo#7", "title": "Pair a speaker", "url": "https://x/7",
     "state": "OPEN", "kind": "before-start", "block": True, "reason": "a real device",
     "try": "pair it", "proof": [], "since": "2026-10-30"},
    {"repo": "demo", "number": 9, "ref": "demo#9", "title": "New tab", "url": "https://x/9",
     "state": "CLOSED", "kind": "validate-after", "block": False, "reason": "", "try": "",
     "proof": [], "since": ""},
]


def _fake_cli(responses, calls):
    def run(_fleet, args, _timeout):
        calls.append(list(args))
        reply = responses[args[0]]
        if isinstance(reply, Exception):
            raise reply
        return reply
    return run


@pytest.fixture
def cli(monkeypatch):
    calls: list = []
    responses: dict = {"list": (0, json.dumps({"items": ITEMS, "unknown": []}))}
    monkeypatch.setattr(owner_checks, "_run_cli", _fake_cli(responses, calls))
    return responses, calls


def test_never_fetched_is_unknown_not_none():
    snap = owner_checks.snapshot(now=NOW)
    assert snap["available"] is False
    assert snap["items"] == []


def test_refresh_fills_the_cache_and_a_missing_block_stays_listed(cli):
    snap = owner_checks.refresh("fleet")
    assert snap["available"] is True and snap["error"] is None
    assert [it["ref"] for it in snap["items"]] == ["demo#7", "demo#9"]
    no_block = snap["items"][1]
    assert no_block["block"] is False and no_block["reason"] == ""


def test_a_failed_refresh_keeps_the_older_items(cli):
    responses, _ = cli
    owner_checks.refresh("fleet")
    responses["list"] = owner_checks.OwnerCheckError("gh down")
    snap = owner_checks.refresh("fleet")
    assert snap["error"] == "gh down"
    assert len(snap["items"]) == 2


def test_an_unreadable_fleet_is_an_error_not_an_empty_list(cli):
    responses, _ = cli
    owner_checks.refresh("fleet")
    responses["list"] = (3, json.dumps({"items": [], "unknown": [{"repo": "*", "reason": "no projects.toml"}]}))
    snap = owner_checks.refresh("fleet")
    assert snap["error"] == "no projects.toml"
    assert len(snap["items"]) == 2


def test_unreadable_repos_ride_along_as_unknown(cli):
    responses, _ = cli
    responses["list"] = (3, json.dumps({"items": ITEMS[:1], "unknown": [{"repo": "other", "reason": "403"}]}))
    snap = owner_checks.refresh("fleet")
    assert snap["unknown"] == [{"repo": "other", "reason": "403"}]
    assert snap["error"] is None


def test_refresh_gate_runs_one_at_a_time_with_a_cooldown(monkeypatch):
    started = []
    monkeypatch.setattr(owner_checks.threading, "Thread", lambda **kw: type(
        "T", (), {"start": lambda self: started.append(kw["args"])})())
    assert owner_checks.start_refresh("fleet", now=100.0) is True
    assert owner_checks.start_refresh("fleet", now=101.0) is False  # in flight
    owner_checks._cache["refreshing"] = False
    assert owner_checks.start_refresh("fleet", now=110.0) is False  # cooldown
    assert owner_checks.start_refresh("fleet", now=100.0 + owner_checks.REFRESH_COOLDOWN_SECONDS) is True
    assert len(started) == 2


@pytest.mark.parametrize("raw, message", [
    ({"ref": "demo", "kind": "validate-after", "answer": "validated"}, "repo"),
    ({"ref": "demo#9", "kind": "nope", "answer": "validated"}, "kind"),
    ({"ref": "demo#7", "kind": "before-start", "answer": "validated"}, "cannot"),
    ({"ref": "demo#9", "kind": "validate-after", "answer": "problem", "text": "  "}, "description"),
])
def test_malformed_answers_are_refused(raw, message):
    with pytest.raises(ValueError, match=message):
        owner_checks.validate_answer(raw)


def test_later_snoozes_without_touching_github(cli):
    _, calls = cli
    owner_checks.refresh("fleet")
    ans = owner_checks.validate_answer({"ref": "demo#7", "kind": "before-start", "answer": "later"})
    owner_checks.answer("fleet", ans, comment=lambda *a: pytest.fail("no comment"), now=NOW)
    assert calls == [["list", "--json"]]
    item = owner_checks.snapshot(now=NOW)["items"][0]
    assert item["mark"] == "later"
    # The snooze ends and the item waits again.
    later = NOW + timedelta(hours=owner_checks.SNOOZE_HOURS + 1)
    assert owner_checks.snapshot(now=later)["items"][0]["mark"] is None


def test_problem_posts_a_comment_and_keeps_the_label(cli):
    _, calls = cli
    owner_checks.refresh("fleet")
    posted = []
    ans = owner_checks.validate_answer(
        {"ref": "demo#9", "kind": "validate-after", "answer": "problem", "text": "the tab\nis blank"})
    owner_checks.answer("fleet", ans, comment=lambda *a: posted.append(a), now=NOW)
    assert posted == [("demo", 9, owner_checks.problem_comment("the tab is blank", "2026-11-01"))]
    assert all(c[0] != "clear" for c in calls)
    item = next(it for it in owner_checks.snapshot(now=NOW)["items"] if it["ref"] == "demo#9")
    assert item["mark"] == "problem"


def test_validated_clears_through_owner_check_and_hides_the_lagging_item(cli):
    responses, calls = cli
    responses["clear"] = (0, "CLEARED=demo#9 removed=needs-validation\n")
    owner_checks.refresh("fleet")
    ans = owner_checks.validate_answer({"ref": "demo#9", "kind": "validate-after", "answer": "validated"})
    owner_checks.answer("fleet", ans, comment=lambda *a: pytest.fail("no comment"), now=NOW)
    assert ["clear", "demo#9", "--answer", "validated by owner"] in calls
    # GitHub's label index lags: the next list still returns it, the Board doesn't.
    owner_checks.refresh("fleet")
    assert [it["ref"] for it in owner_checks.snapshot(now=NOW)["items"]] == ["demo#7"]


def test_a_clear_that_did_not_land_raises(cli):
    responses, _ = cli
    responses["clear"] = (3, "CLEARED=unknown reason=gh: 502\n")
    owner_checks.refresh("fleet")
    ans = owner_checks.validate_answer({"ref": "demo#7", "kind": "before-start", "answer": "done"})
    with pytest.raises(owner_checks.OwnerCheckError, match="did not land"):
        owner_checks.answer("fleet", ans, comment=lambda *a: None, now=NOW)
    assert [it["ref"] for it in owner_checks.snapshot(now=NOW)["items"]] == ["demo#7", "demo#9"]


def test_corrupt_marks_read_as_none(tmp_path):
    path = tmp_path / "marks.json"
    path.write_text("{not json", encoding="utf-8")
    assert owner_checks.read_marks(path) == {}


def test_no_fleet_config_is_its_own_error():
    with pytest.raises(owner_checks.OwnerCheckError, match="not set"):
        owner_checks._cli_argv("")


# ------------------------------------------------------------------ the route

@pytest.fixture
def _bypass_gate(monkeypatch):
    from app.webapp import middleware
    monkeypatch.setattr(
        middleware.trust, "LOOPBACK_HOSTS",
        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )


def test_answer_route_is_passkey_gated(webapp_client):
    from app.webapp.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/board/owner-checks/answer") == "passkey"
    client, _, _ = webapp_client
    resp = client.post("/api/board/owner-checks/answer",
                       json={"answers": [{"ref": "demo#7", "kind": "before-start", "answer": "later"}]})
    assert resp.status_code == 403


def test_answer_route_reports_each_answer(webapp_client, _bypass_gate, cli, monkeypatch):
    responses, _ = cli
    responses["clear"] = (3, "CLEARED=unknown reason=gh: 502\n")
    owner_checks.refresh("fleet")
    client, _, _ = webapp_client
    resp = client.post("/api/board/owner-checks/answer", json={"answers": [
        {"ref": "demo#7", "kind": "before-start", "answer": "done"},
        {"ref": "demo#9", "kind": "validate-after", "answer": "later"},
    ]})
    assert resp.status_code == 200
    body = resp.json()
    assert [(r["ref"], r["ok"]) for r in body["results"]] == [("demo#7", False), ("demo#9", True)]
    marks = {it["ref"]: it["mark"] for it in body["owner_checks"]["items"]}
    assert marks == {"demo#7": None, "demo#9": "later"}


def test_answer_route_refuses_a_malformed_batch_before_any_write(webapp_client, _bypass_gate, cli):
    _, calls = cli
    client, _, _ = webapp_client
    resp = client.post("/api/board/owner-checks/answer", json={"answers": [
        {"ref": "demo#9", "kind": "validate-after", "answer": "later"},
        {"ref": "demo#7", "kind": "before-start", "answer": "validated"},
    ]})
    assert resp.status_code == 400
    assert calls == []
    assert not owner_checks.MARKS_FILE.exists()


def test_board_payload_carries_the_owner_section(webapp_client):
    client, _, _ = webapp_client
    section = client.get("/api/board").json()["owner_checks"]
    assert section["available"] is False
    assert section["items"] == []
