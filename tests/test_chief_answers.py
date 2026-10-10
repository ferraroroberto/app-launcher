"""The Board's answered marks on the chief's plan, shared by every device (#1487).

The answer sheet used to mark what it sent in the answering browser's
localStorage, so another device showed the same questions as open. The marks
now live with the webapp: ``POST /api/board/chief/answered`` records them and
``GET /api/board/chief-plan`` returns them on the plan version they were made
on. These tests drive two clients against one webapp, the reset when the
chief rewrites the plan, and the rule that the app never writes the plan file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import chief_answers


def _write_plan(path: Path, updated_at: str) -> str:
    text = json.dumps({
        "version": 1, "updated_at": updated_at, "lanes": [],
        "queue": [{"repo": "app-launcher", "ref": "#1487", "title": "marks", "status": "building"}],
        "waiting_on_roberto": [
            {"id": "q-plans", "text": "Approve the plans?"},
            {"text": "Remember the last tab?"},
        ],
    })
    path.write_text(text, encoding="utf-8")
    return text


@pytest.fixture
def _bypass_gate(monkeypatch):
    """Treat the TestClient host as loopback so the route logic runs (the
    passkey gate itself is pinned by test_answered_route_is_passkey_gated)."""
    from app.webapp import middleware
    monkeypatch.setattr(
        middleware, "LOOPBACK_HOSTS",
        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )


def test_answered_route_is_passkey_gated(webapp_client):
    from app.webapp.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/board/chief/answered") == "passkey"
    client, _, _ = webapp_client
    resp = client.post("/api/board/chief/answered", json={"updated_at": "", "ids": ["0"]})
    assert resp.status_code == 403
    assert not chief_answers.CHIEF_ANSWERED_FILE.exists()


def test_two_clients_share_the_marks_and_a_new_plan_clears_them(webapp_client, _bypass_gate):
    client_a, app, _ = webapp_client
    client_b = TestClient(app)
    plan_file = Path(app.state.webapp_config.chief_plan_file)
    plan_text = _write_plan(plan_file, "2026-10-10T09:00:00Z")

    assert client_b.get("/api/board/chief-plan").json()["answered"] == []

    # Device A answers the first question.
    resp = client_a.post("/api/board/chief/answered", json={
        "updated_at": "2026-10-10T09:00:00Z", "ids": ["q-plans"],
    })
    assert resp.status_code == 200
    assert resp.json() == {
        "recorded": True, "updated_at": "2026-10-10T09:00:00Z", "answered": ["q-plans"],
    }
    # Device B sees it on its next poll, and adds the second; both now agree.
    assert client_b.get("/api/board/chief-plan").json()["answered"] == ["q-plans"]
    resp = client_b.post("/api/board/chief/answered", json={
        "updated_at": "2026-10-10T09:00:00Z", "ids": ["1", "q-plans"],
    })
    assert resp.json()["answered"] == ["q-plans", "1"]
    assert client_a.get("/api/board/chief-plan").json()["answered"] == ["q-plans", "1"]
    # The plan file is the chief's: recording marks never touched it.
    assert plan_file.read_text(encoding="utf-8") == plan_text

    # The chief rewrites the plan: no device shows the old marks any more.
    _write_plan(plan_file, "2026-10-10T09:05:00Z")
    for client in (client_a, client_b):
        assert client.get("/api/board/chief-plan").json()["answered"] == []
    # A page still on the old plan can't put them back.
    resp = client_a.post("/api/board/chief/answered", json={
        "updated_at": "2026-10-10T09:00:00Z", "ids": ["1"],
    })
    assert resp.json() == {
        "recorded": False, "updated_at": "2026-10-10T09:05:00Z", "answered": [],
    }
    assert client_b.get("/api/board/chief-plan").json()["answered"] == []
    # On the new plan, marks start from nothing rather than merging the old.
    resp = client_b.post("/api/board/chief/answered", json={
        "updated_at": "2026-10-10T09:05:00Z", "ids": ["1"],
    })
    assert resp.json()["answered"] == ["1"]


def test_no_plan_records_nothing(webapp_client, _bypass_gate):
    client, _, _ = webapp_client
    resp = client.post("/api/board/chief/answered", json={"updated_at": "", "ids": ["0"]})
    assert resp.json() == {"recorded": False, "updated_at": "", "answered": []}
    assert not chief_answers.CHIEF_ANSWERED_FILE.exists()


@pytest.mark.parametrize("body", [
    {"ids": ["0"]},
    {"updated_at": 5, "ids": ["0"]},
    {"updated_at": "x", "ids": "0"},
    {"updated_at": "x", "ids": [0]},
])
def test_malformed_body_is_refused(webapp_client, _bypass_gate, body):
    client, _, _ = webapp_client
    assert client.post("/api/board/chief/answered", json=body).status_code == 400


def test_unreadable_or_foreign_marks_read_as_none(tmp_path: Path):
    path = tmp_path / "answered.json"
    assert chief_answers.read_answered("v1", path) == []
    path.write_text("not json", encoding="utf-8")
    assert chief_answers.read_answered("v1", path) == []
    path.write_text(json.dumps({"updated_at": "v1", "ids": "q"}), encoding="utf-8")
    assert chief_answers.read_answered("v1", path) == []
    path.write_text(json.dumps({"updated_at": "v1", "ids": ["q"]}), encoding="utf-8")
    assert chief_answers.read_answered("v1", path) == ["q"]
    assert chief_answers.read_answered("v2", path) == []


def test_clean_ids_drops_blanks_and_duplicates_and_caps():
    assert chief_answers.clean_ids([" a ", "", "a", "b"]) == ["a", "b"]
    assert chief_answers.clean_ids(["x" * 500])[0] == "x" * chief_answers.MAX_ID_LEN
    many = [str(i) for i in range(chief_answers.MAX_IDS + 10)]
    assert len(chief_answers.clean_ids(many)) == chief_answers.MAX_IDS
    assert chief_answers.clean_ids(None) is None
    assert chief_answers.clean_ids(["a", 1]) is None
