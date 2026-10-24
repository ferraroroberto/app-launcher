"""The chief's open-question count, one rule on both sides (#1502).

The Board tab's badge counts the chief's unanswered questions from every tab,
so the webapp counts them on the sessions poll (``chief_questions``) while the
Board counts them from its own plan payload. Which questions are answered
depends on one key rule (the chief's ``id``, else the position), written once
in Python (``src.chief_answers.item_key``) and once in JS
(``chief-keys.js``'s ``itemKey``). Both are pinned to the same fixture here;
the JS half runs under plain Node and skips cleanly without it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src import chief_answers
from src.subprocess_flags import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "chief_question_keys.json"
_JS_TEST = _REPO_ROOT / "tests" / "js" / "chief_keys.test.mjs"


def _cases():
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_python_keys_and_counts_the_shared_fixture(case):
    keys = [chief_answers.item_key(item, i) for i, item in enumerate(case["waiting"])]
    assert keys == case["keys"]
    plan = {"state": "ok", "waiting_on_roberto": case["waiting"], "answered": case["answered"]}
    assert chief_answers.unanswered_count(plan) == case["unanswered"]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_js_keys_and_counts_the_same_fixture():
    result = subprocess.run(
        ["node", str(_JS_TEST)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        "chief-keys.js disagrees with the shared fixture:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout


def test_an_unreadable_plan_is_unknown_and_an_empty_one_is_zero():
    assert chief_answers.unanswered_count({"state": "unreadable"}) is None
    assert chief_answers.unanswered_count({"state": "unavailable"}) is None
    assert chief_answers.unanswered_count({"state": "empty"}) == 0


def _write_plan(path: Path, waiting) -> None:
    path.write_text(json.dumps({
        "version": 1, "updated_at": "2026-10-11T08:00:00Z", "lanes": [],
        "queue": [{"repo": "demo-repo", "ref": "#7", "title": "a thing", "status": "building"}],
        "waiting_on_roberto": waiting,
    }), encoding="utf-8")


def test_sessions_poll_counts_open_questions_fleet_wide(webapp_client):
    client, app, overrides = webapp_client
    overrides["session"].list_sessions.return_value = []
    plan_file = Path(app.state.webapp_config.chief_plan_file)
    _write_plan(plan_file, [
        {"id": "q-one", "text": "One?", "ref": "demo-repo#1"},
        {"text": "Two?", "ref": "other-repo#2"},
        {"text": "Three?"},
    ])
    assert client.get("/api/claude-code/sessions").json()["chief_questions"] == 3

    # Answered on any device: no longer counted.
    chief_answers.record_answered("2026-10-11T08:00:00Z", ["q-one", "2"])
    assert client.get("/api/claude-code/sessions").json()["chief_questions"] == 1


def test_sessions_poll_says_unknown_for_an_unreadable_plan(webapp_client):
    client, app, overrides = webapp_client
    overrides["session"].list_sessions.return_value = []
    Path(app.state.webapp_config.chief_plan_file).write_text("{not json", encoding="utf-8")
    assert client.get("/api/claude-code/sessions").json()["chief_questions"] is None
