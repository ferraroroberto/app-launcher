"""src/inspiration/ideas.py and GET /api/inspiration/ideas: the Board Ideas card's read.

The state root is a stand-in skill's ``state_root`` pinned under ``tmp_path``
(the same stand-in the source-list tests load), so no test reads the owner's
real ideas. Every idea here is made up.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.inspiration import ideas as board_ideas
from tests.unit.board.test_owner_checks import _run_node_pin
from tests.unit.core.test_inspiration_sources import _checkout

IDEA = {"id": "i-low", "title": "A made-up idea", "link": "https://example.test/a",
        "source": {"name": "Letter", "kind": "newsletter"}, "author": "", "published": "",
        "excerpt": "short", "digest": "2026-11-09", "score": 6, "model": "m",
        "why_fits": "w", "experiment": "e", "suggested_repo": "demo", "prompt": "Do it.\n\n## Source"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """(fleet-config dir, state root) with the root pinned under tmp_path."""
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setenv("INSPIRATION_SCAN_ROOT", str(root))
    return str(_checkout(tmp_path / "fleet-config")), root


def _write(root, ideas, ledger=None):
    (root / "ideas.json").write_text(json.dumps({"version": 1, "digest": "2026-11-10", "ideas": ideas}),
                                     encoding="utf-8")
    if ledger is not None:
        (root / "ideas-ledger.jsonl").write_text("".join(json.dumps(r) + "\n" for r in ledger), encoding="utf-8")


def test_no_scan_yet_is_missing_not_an_empty_ok(env):
    fc, _root = env
    assert board_ideas.read_board_ideas(fc)["state"] == "missing"


def test_undecided_ideas_best_first_and_the_ledger_newest_first(env):
    fc, root = env
    high = dict(IDEA, id="i-high", score=9)
    filed = dict(IDEA, id="i-filed", score=8)
    _write(root, [IDEA, high, filed], [
        {"id": "i-filed", "state": "applied", "at": "2026-11-10T08:00:00Z", "issue": "demo#12", "title": "t"},
        {"id": "i-gone", "state": "filtered", "at": "2026-11-10T09:00:00Z", "note": "not now"},
    ])
    out = board_ideas.read_board_ideas(fc, "octo")
    assert out["state"] == "ok" and out["digest"] == "2026-11-10"
    assert [i["id"] for i in out["ideas"]] == ["i-high", "i-low"]
    assert [r["id"] for r in out["ledger"]] == ["i-gone", "i-filed"]
    assert out["ledger"][1]["issue_url"] == "https://github.com/octo/demo/issues/12"
    assert out["ledger"][0]["issue_url"] == "" and out["ledger"][0]["note"] == "not now"


@pytest.mark.parametrize("ideas_text, ledger_text, reason", [
    ("{not json", None, "ideas.json is not valid JSON."),
    ('{"ideas": 3}', None, "ideas.json has no ideas list."),
    ('{"ideas": []}', '{"id": "x", "state": "applied"}\nnot json\n', "ideas-ledger.jsonl line 2 is not JSON."),
    ('{"ideas": []}', '{"id": "x", "state": "maybe"}\n', "ideas-ledger.jsonl line 1 is not a ledger row."),
])
def test_an_unreadable_file_is_unknown_with_its_reason_never_empty(env, ideas_text, ledger_text, reason):
    fc, root = env
    (root / "ideas.json").write_text(ideas_text, encoding="utf-8")
    if ledger_text is not None:
        (root / "ideas-ledger.jsonl").write_text(ledger_text, encoding="utf-8")
    out = board_ideas.read_board_ideas(fc)
    assert (out["state"], out["reason"], out["ideas"], out["ledger"]) == ("unknown", reason, [], [])


def test_route_serves_the_read_and_an_absent_skill_is_missing(webapp_client, env, tmp_path):
    client, app, _ = webapp_client
    fc, root = env
    _write(root, [IDEA])
    app.state.webapp_config.fleet_config_dir = fc
    body = client.get("/api/inspiration/ideas").json()
    assert body["state"] == "ok" and body["ideas"][0]["prompt"] == IDEA["prompt"]
    app.state.webapp_config.fleet_config_dir = str(tmp_path / "no-checkout")
    assert client.get("/api/inspiration/ideas").json()["state"] == "missing"


def test_a_skill_that_will_not_load_is_unknown(env):
    fc, _root = env
    (Path(fc) / ".claude" / "skills" / "inspiration-scan" / "sources.py").write_text("raise RuntimeError\n")
    out = board_ideas.read_board_ideas(fc)
    assert (out["state"], out["reason"]) == ("unknown", "The inspiration-scan skill could not be loaded.")


def test_idea_message_node_pin():
    assert "OK idea message" in _run_node_pin("idea_message.test.mjs")
