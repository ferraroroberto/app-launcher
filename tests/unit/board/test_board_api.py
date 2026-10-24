"""Board API: GET /api/board shape, the chief plan reader, the quota endpoint and refresh."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import httpx

from app.webapp.routers import board as board_router
from src import board, chief_plan, github_client, quota_usage

from tests.unit.board._support import _FakeGh, _claim_contract, _iso, _live


def test_api_board_distinguishes_unfetched_from_genuinely_empty(
    webapp_client, monkeypatch
):
    """#910: a never-fetched cache and a fetched-but-empty one both give an
    empty Backlog list, so the payload must say which it is. A real zero is
    ``available: True`` — it must not look like a failure either."""
    client, _app, _overrides = webapp_client

    unfetched = client.get("/api/board").json()
    assert unfetched["columns"]["backlog"] == []
    assert unfetched["github"]["available"] is False

    def _nothing_open(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    monkeypatch.setattr(github_client.subprocess, "run", _nothing_open)
    client.post("/api/board/github/refresh")

    empty = client.get("/api/board").json()
    assert empty["columns"]["backlog"] == []
    assert empty["columns"]["done"] == []
    assert empty["github"]["available"] is True
    assert empty["github"]["error"] is None


# ---------------------------------------------------------------- API shape


def test_api_board_shape_with_everything_absent(webapp_client):
    client, _app, _overrides = webapp_client
    body = client.get("/api/board").json()
    assert set(body["columns"]) == {"backlog", "claude_turn", "your_turn", "other", "done"}
    assert body["github"] == {"available": False, "fetched_at": None, "error": None}
    # A session list read and found empty is a real zero (#915).
    assert body["live_sessions"] == {"available": True, "error": None}
    assert body["sessions_state"]["available"] is False
    assert body["active_issues"]["available"] is False
    # The quota rows live on /api/rate-limits alone (#1433).
    assert "quota_lines" not in body
    assert body["columns"]["backlog"] == []
    assert body["generated_at"]


# ------------------------------------------------ chief's plan (#1279)

_PLAN_V1 = {
    "version": 1,
    "updated_at": "2026-09-26T14:40:00Z",
    "future_field": {"ignored": True},
    "lanes": [{"repo": "app-launcher", "session": "s-1", "item": "#1273", "status": "gate", "extra": 1,
               "model": "opus"}],
    "queue": [
        {"repo": "app-launcher", "ref": "#1273", "title": "Chat by default", "status": "gate", "note": "",
         "model": "sonnet"},
        "not a row",
        {"repo": "automation", "ref": "#135", "title": "parking burst\ttrial", "status": "someday",
         "note": ["not", "text"], "model": 5},
        {"repo": "automation", "ref": "#136", "title": "no model", "status": "queued"},
    ],
    "waiting_on_roberto": [{"text": "Remember the last tab?", "ref": "app-launcher#1131"}],
}


def test_chief_plan_reader_is_tolerant(tmp_path: Path):
    """Format v1 read by contract: unknown fields dropped, a non-row skipped,
    an unknown status passed through for the neutral chip; missing or an
    empty queue is empty; anything untrustworthy is unreadable, never raised."""
    f = tmp_path / "chief-plan.json"
    assert chief_plan.read_chief_plan(f) == {"state": "empty"}

    f.write_text(json.dumps(_PLAN_V1), encoding="utf-8")
    plan = chief_plan.read_chief_plan(f)
    assert plan == {
        "state": "ok",
        "updated_at": "2026-09-26T14:40:00Z",
        "lanes": [{"repo": "app-launcher", "session": "s-1", "item": "#1273", "status": "gate",
                   "model": "opus", "ref_url": ""}],
        # `model` (#1352): a string reads through; a wrong type (5) or a
        # missing value reads as blank.
        "queue": [
            {"repo": "app-launcher", "ref": "#1273", "title": "Chat by default", "status": "gate", "note": "",
             "model": "sonnet", "ref_url": ""},
            {"repo": "automation", "ref": "#135", "title": "parking burst trial", "status": "someday",
             "note": "", "model": "", "ref_url": ""},
            {"repo": "automation", "ref": "#136", "title": "no model", "status": "queued",
             "note": "", "model": "", "ref_url": ""},
        ],
        # An old text-only item reads as a free-text question (#1295).
        "waiting_on_roberto": [{
            "text": "Remember the last tab?", "ref": "app-launcher#1131", "repo": "", "detail": "",
            "recommendation": "", "id": "", "question": "Remember the last tab?", "options": [],
            "multi": False, "ref_url": "",
        }],
    }

    f.write_text(json.dumps({"version": 1, "queue": [], "lanes": _PLAN_V1["lanes"]}), encoding="utf-8")
    assert chief_plan.read_chief_plan(f) == {"state": "empty"}
    for bad in ("{", "", "[]", json.dumps({**_PLAN_V1, "version": 2}), json.dumps({"queue": _PLAN_V1["queue"]})):
        f.write_text(bad, encoding="utf-8")
        assert chief_plan.read_chief_plan(f) == {"state": "unreadable"}, bad
    f.write_bytes(b"\xff\xfe not utf-8")
    assert chief_plan.read_chief_plan(f) == {"state": "unreadable"}


def test_chief_plan_questions_read_the_additive_fields_field_by_field(tmp_path: Path):
    """#1295: a waiting item's answer-sheet fields are read one by one — a
    bad type drops that field alone, never the item or the card; options are
    capped at four and need a label; question falls back to the text and a
    missing id stays blank; a ``repo#N`` ref links on the given owner."""
    f = tmp_path / "chief-plan.json"
    f.write_text(json.dumps({**_PLAN_V1, "waiting_on_roberto": [
        {
            "id": "q-plans", "text": "Approve the plans", "ref": "fleet-config#959", "repo": "fleet-config",
            "question": "Approve the four plans?", "detail": "Each one\nships alone.",
            "recommendation": "Yes, all four", "multi": False, "future": 1,
            "options": [
                {"label": "Yes, all four", "description": "Ship them in order", "recommended": True},
                {"label": "Only the first"},
                {"label": ""}, "not an option", {"description": "no label"},
                {"label": "Hold them", "recommended": "yes"},
                {"label": "Ask me later"}, {"label": "Fifth one dropped"},
            ],
        },
        "not an item",
        {"text": "Which days?", "multi": "true", "options": {"label": "not a list"}, "id": ["bad"],
         "question": 7, "detail": None},
        {"text": "Pick any", "multi": True, "options": [{"label": "A"}, {"label": "B"}], "ref": "#12",
         "repo": "life-os"},
    ]}), encoding="utf-8")
    plan = chief_plan.read_chief_plan(f, "octo")
    # Queue rows link the same way (#1297): a `#N` ref beside its repo.
    assert [q["ref_url"] for q in plan["queue"]] == [
        "https://github.com/octo/app-launcher/issues/1273",
        "https://github.com/octo/automation/issues/135",
        "https://github.com/octo/automation/issues/136",
    ]
    # A lane links from its `item` the same way (#1578).
    assert [lane["ref_url"] for lane in plan["lanes"]] == [
        "https://github.com/octo/app-launcher/issues/1273",
    ]
    first, second, third = plan["waiting_on_roberto"]
    assert first == {
        "text": "Approve the plans", "ref": "fleet-config#959", "repo": "fleet-config",
        "detail": "Each one ships alone.", "recommendation": "Yes, all four", "id": "q-plans",
        "question": "Approve the four plans?", "multi": False,
        "ref_url": "https://github.com/octo/fleet-config/issues/959",
        "options": [
            {"label": "Yes, all four", "description": "Ship them in order", "recommended": True},
            {"label": "Only the first", "description": "", "recommended": False},
            {"label": "Hold them", "description": "", "recommended": False},
            {"label": "Ask me later", "description": "", "recommended": False},
        ],
    }
    assert second == {
        "text": "Which days?", "ref": "", "repo": "", "detail": "", "recommendation": "",
        "id": "", "question": "7", "options": [], "multi": False, "ref_url": "",
    }
    assert third["id"] == "" and third["multi"] is True
    assert third["ref_url"] == "https://github.com/octo/life-os/issues/12"
    assert [o["label"] for o in third["options"]] == ["A", "B"]


def test_api_board_chief_plan_reads_the_configured_file_behind_board_auth(webapp_client):
    client, app, _overrides = webapp_client
    plan_file = Path(app.state.webapp_config.chief_plan_file)
    # The fixture points at a temp file, never the real hook state.
    assert "hooks" not in plan_file.parts
    assert client.get("/api/board/chief-plan").json() == {"state": "empty"}
    plan_file.write_text(json.dumps(_PLAN_V1), encoding="utf-8")
    body = client.get("/api/board/chief-plan").json()
    assert body["state"] == "ok" and [q["ref"] for q in body["queue"]] == ["#1273", "#135", "#136"]
    owner = app.state.webapp_config.github_owner
    assert body["waiting_on_roberto"][0]["ref_url"] == f"https://github.com/{owner}/app-launcher/issues/1131"

    app.state.webapp_config.auth_token = "secret-token"
    for path in ("/api/board", "/api/board/chief-plan"):
        assert client.get(path).status_code == 401, path
    ok = client.get("/api/board/chief-plan", headers={"Authorization": "Bearer secret-token"})
    assert ok.status_code == 200 and ok.json()["state"] == "ok"


def test_api_board_marks_active_backlog_issue(
    webapp_client, monkeypatch
):
    client, app, _overrides = webapp_client
    fake = _FakeGh()
    monkeypatch.setattr(github_client.subprocess, "run", fake)
    github_client.refresh("ferraroroberto")

    active_file = Path(app.state.webapp_config.sessions_state_file).with_name(
        "active-issues.json"
    )
    active_file.write_text(json.dumps({
        "app-launcher#164": {
            "repo": "app-launcher", "number": 164,
            "branch": "feat/164-board", "started_at": _iso(datetime.now(timezone.utc)),
        },
    }), encoding="utf-8")

    body = client.get("/api/board").json()
    assert body["active_issues"]["available"] is True
    assert body["active_issues"]["count"] == 1
    assert body["columns"]["backlog"][0]["in_progress"] is True


def test_api_board_exposes_claim_state_per_backlog_card(
    webapp_client, monkeypatch, tmp_path
):
    """#948: the owner's live-session verdict reaches every backlog card, and
    an unreadable session list never turns a claim into a dead one."""
    from src.session_client import SessionHostError

    client, app, overrides = webapp_client
    fake = _FakeGh()
    monkeypatch.setattr(github_client.subprocess, "run", fake)
    github_client.refresh("ferraroroberto")
    app.state.webapp_config.fleet_config_dir = str(_claim_contract(tmp_path))

    active_file = Path(app.state.webapp_config.sessions_state_file).with_name(
        "active-issues.json"
    )

    def _claim(owner):
        active_file.write_text(json.dumps({"app-launcher#164": {
            "repo": "app-launcher", "number": 164, "branch": "feat/164-board",
            "started_at": _iso(datetime.now(timezone.utc)),
            **({"owner_session_id": owner} if owner else {}),
        }}), encoding="utf-8")

    def _card():
        return client.get("/api/board").json()["columns"]["backlog"][0]

    overrides["session"].list_sessions.return_value = [
        {"session_id": "owner-1", "alive": True, "kind": "pty", "project_dir": "E:/x"},
    ]
    _claim("owner-1")
    assert (_card()["claim_state"], _card()["in_progress"]) == ("live", True)

    _claim("owner-gone")
    assert (_card()["claim_state"], _card()["in_progress"]) == ("dead", False)

    _claim(None)
    assert (_card()["claim_state"], _card()["in_progress"]) == ("unknown", True)

    _claim("owner-gone")
    overrides["session"].list_sessions.side_effect = SessionHostError("down")
    assert (_card()["claim_state"], _card()["in_progress"]) == ("unknown", True)


def test_quota_reading_is_served_by_one_endpoint(webapp_client):
    """#1433: both tabs' usage meters read /api/rate-limits; the Board payload
    carries no quota, so the two tabs can never show different readings."""
    client, app, _overrides = webapp_client
    rate_limits_file = Path(app.state.webapp_config.rate_limits_file)
    rate_limits_file.write_text(json.dumps({
        "five_hour": {"used_percentage": 42, "resets_at": 1751640000},
        "seven_day": {"used_percentage": 88, "resets_at": 1751900000},
        "captured_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=1)),
    }), encoding="utf-8")

    assert "quota_lines" not in client.get("/api/board").json()
    claude = client.get("/api/rate-limits").json()["quota_lines"][0]
    assert claude["label"] == "Claude Code"
    assert claude["state"] == "available"
    assert claude["five_hour"]["used_percentage"] == 42
    assert claude["weekly"]["used_percentage"] == 88


def test_api_rate_limits_standalone_endpoint_absent(webapp_client):
    client, _app, _overrides = webapp_client
    lines = client.get("/api/rate-limits").json()["quota_lines"]
    assert [line["harness"] for line in lines] == ["claude", "codex"]
    assert [line["label"] for line in lines] == ["Claude Code", "Codex"]
    for line in lines:
        assert line["state"] == "unknown"
        assert line["five_hour"] is None and line["weekly"] is None


def test_api_rate_limits_standalone_endpoint_present(webapp_client):
    client, app, _overrides = webapp_client
    rate_limits_file = Path(app.state.webapp_config.rate_limits_file)
    rate_limits_file.write_text(json.dumps({
        "five_hour": {"used_percentage": 10, "resets_at": 1751640000},
        "captured_at": _iso(datetime.now(timezone.utc)),
    }), encoding="utf-8")

    claude = client.get("/api/rate-limits").json()["quota_lines"][0]
    assert claude["state"] == "available"
    assert claude["five_hour"] == {
        "used_percentage": 10, "resets_at": 1751640000, "duration_minutes": 300,
    }
    assert claude["weekly"] is None


def test_api_rate_limits_never_follows_the_selected_model(webapp_client):
    """#860: both agents always, whatever the caller has selected."""
    client, _app, _overrides = webapp_client
    for selection in ("claude:sonnet", "codex:gpt-5.6-luna", "pi:opus"):
        lines = client.get(
            "/api/rate-limits?quota_selection=" + selection
        ).json()["quota_lines"]
        assert [line["harness"] for line in lines] == ["claude", "codex"]


def test_coding_and_board_codex_polls_coalesce_one_native_refresh(
    webapp_client, monkeypatch
):
    """Repeated quota polls start one native-process run, not one per poll."""
    _client, app, _overrides = webapp_client
    monkeypatch.setattr(board_router, "_quota_refresh_gate", quota_usage.RefreshGate())
    refresh = Mock(return_value="complete")
    monkeypatch.setattr(quota_usage, "refresh_codex", refresh)

    async def poll_three_times() -> None:
        # One loop for all polls: TestClient tears its per-request loop down,
        # cancelling a refresh task that has not reached its thread yet.
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            for _ in range(3):
                lines = (await http.get("/api/rate-limits")).json()["quota_lines"]
                assert [line["harness"] for line in lines] == ["claude", "codex"]
                assert lines[1]["state"] == "unknown"
            await (board_router._quota_refresh_task or asyncio.sleep(0))
    asyncio.run(poll_three_times())
    assert refresh.call_count == 1


def test_api_board_merges_live_sessions_and_state(webapp_client):
    client, app, overrides = webapp_client
    overrides["session"].list_sessions.return_value = [
        _live("live-1", "E:/automation/photo-ocr", 20),
    ]
    state_file = Path(app.state.webapp_config.sessions_state_file)
    state_file.write_text(json.dumps({
        "t-uuid": {"project": "photo-ocr", "status": "needs-you",
                   "cwd": "E:/automation/photo-ocr",
                   "updated_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=3))},
    }), encoding="utf-8")

    body = client.get("/api/board").json()
    assert body["sessions_state"]["available"] is True
    your_turn = body["columns"]["your_turn"]
    assert [c["session_id"] for c in your_turn] == ["live-1"]
    # #608: no transcript to check, so the needs-you split lands on the
    # safe generic value — still routed to Your turn either way.
    assert your_turn[0]["status"] == "awaiting-input"
    assert body["columns"]["claude_turn"] == []


def test_api_board_chief_never_lands_in_your_turn(webapp_client):
    """The standing fleet chief's needs-you card routes to Claude's turn, not
    Your turn (#575) — its Stop-hook status is its normal resting state
    between dispatches, not a transient alert like a worker session's."""
    client, app, overrides = webapp_client
    overrides["session"].list_sessions.return_value = [
        _live("chief-1", "E:/automation/fleet-config", 20, label="chief"),
    ]
    state_file = Path(app.state.webapp_config.sessions_state_file)
    state_file.write_text(json.dumps({
        "t-uuid": {"project": "fleet-config", "status": "needs-you",
                   "cwd": "E:/automation/fleet-config",
                   "updated_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=3))},
    }), encoding="utf-8")

    body = client.get("/api/board").json()
    assert body["columns"]["your_turn"] == []
    claude_turn = body["columns"]["claude_turn"]
    assert [c["session_id"] for c in claude_turn] == ["chief-1"]
    # #608: no transcript to check, so the needs-you split lands on the
    # safe generic value — still routed to Claude's turn regardless (the
    # chief carve-out), and the client relabels it "standing by" for display.
    assert claude_turn[0]["status"] == "awaiting-input"


def test_api_board_survives_session_host_down(webapp_client, caplog):
    """The Board keeps rendering with the session-host down (#164), but its
    empty live columns are unknown, not empty (#915): ``live_sessions`` says
    so, the failure logs one warning rather than one per poll, and a read
    that genuinely comes back empty is ``available`` again."""
    client, _app, overrides = webapp_client
    from src.session_client import SessionHostError
    overrides["session"].list_sessions.side_effect = SessionHostError("down")
    with caplog.at_level(logging.DEBUG, logger="app.webapp.routers.board_spawn"):
        body = client.get("/api/board").json()
        client.get("/api/board")  # the next poll must not re-warn
    assert body["columns"]["claude_turn"] == []
    assert body["columns"]["your_turn"] == []
    assert body["live_sessions"]["available"] is False
    assert "down" in body["live_sessions"]["error"]
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "live sessions unknown" in warnings[0].getMessage()

    overrides["session"].list_sessions.side_effect = None
    overrides["session"].list_sessions.return_value = []
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.webapp.routers.board_spawn"):
        body = client.get("/api/board").json()
    assert body["live_sessions"] == {"available": True, "error": None}
    assert body["columns"]["your_turn"] == []
    assert any("readable again" in r.getMessage() for r in caplog.records)


def test_api_refresh_endpoint_fills_cache(webapp_client, monkeypatch):
    client, _app, _overrides = webapp_client
    fake = _FakeGh()
    monkeypatch.setattr(github_client.subprocess, "run", fake)

    github = client.post("/api/board/github/refresh").json()
    assert github["error"] is None
    assert github["fetched_at"]
    # The owner from config reaches the gh command line.
    assert any("testowner" in " ".join(argv) for argv in fake.calls)

    body = client.get("/api/board").json()
    assert [c["number"] for c in body["columns"]["backlog"]] == [164]
    assert body["columns"]["your_turn"] == []
    assert [c["kind"] for c in body["columns"]["other"]] == ["pr"]


def test_session_board_columns_routes_like_the_board(monkeypatch, tmp_path):
    """#1434: the Coding tab's per-session placement is build_board's own
    routing: the needs-you family lands in Your turn, the chief never does,
    and a state-only card (no session id) is left out."""
    from src import active_issue_claims

    cards = [
        {"session_id": "s-stall", "status": "stalled", "label": "", "age_seconds": 900},
        {"session_id": "s-wait", "status": "awaiting-decision", "label": "", "age_seconds": 60},
        {"session_id": "s-work", "status": "working", "label": "", "age_seconds": 540},
        {"session_id": "s-chief", "status": "awaiting-input", "label": "chief"},
        {"session_id": None, "status": "stalled", "label": ""},
    ]
    monkeypatch.setattr(board, "merge_sessions", lambda *a, **k: cards)
    monkeypatch.setattr(active_issue_claims, "classify_claims", lambda *a, **k: {})
    placed = board.session_board_columns([], {}, {}, fleet_config_dir=tmp_path)
    assert placed == {
        "s-stall": {"column": "your_turn", "status": "stalled", "age_seconds": 900},
        "s-wait": {"column": "your_turn", "status": "awaiting-decision", "age_seconds": 60},
        "s-work": {"column": "claude_turn", "status": "working", "age_seconds": 540},
        # No age anchor on the card: carried as None (unknown), never 0.
        "s-chief": {"column": "claude_turn", "status": "awaiting-input", "age_seconds": None},
    }
