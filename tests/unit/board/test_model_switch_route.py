"""``POST /api/claude-code/sessions/{sid}/model``: the Chat view's model chip
switching a live session's model through the agent's own ``/model`` command.

The PTY is a recorder that repaints the capture the way the probed terminals
did (``tests/unit/sessions/test_model_switch.py`` holds the screens), and the
Claude transcript is a real file the fake terminal appends to."""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from tests.unit.board.test_plan_picker import PLAN_MODE, _live, _screen, picker_client  # noqa: F401
from tests.unit.sessions.test_model_switch import AFTER, DIALOG


def _answer(model: str) -> str:
    return json.dumps({"type": "assistant", "message": {"model": model, "content": []}}) + "\n"


def _set_model(name: str) -> str:
    content = (f"<local-command-stdout>Set model to `{name}` and saved as your default "
               "for new sessions</local-command-stdout>")
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"


@pytest.fixture
def switch_client(picker_client, monkeypatch, tmp_path):
    """picker_client with short waits, a Claude transcript on Sonnet, and a
    terminal that shows the "Switch model?" dialog when ``state["history"]``
    (a conversation the switch must re-read) and applies at once otherwise."""
    from app.webapp.routers import session_screen as router

    client, session, _show, typed = picker_client
    monkeypatch.setattr(router, "_MODEL_CONFIRM_S", 0.6)
    monkeypatch.setattr(router, "_MODEL_POLL_S", 0.02)
    capture = tmp_path / "s1.transcript"
    transcript = tmp_path / "convo.jsonl"
    transcript.write_text(_answer("claude-sonnet-5-5"), encoding="utf-8")
    state: Dict[str, Any] = {"history": True, "applies": True, "dialog": DIALOG}

    def paint(lines: List[str]) -> None:
        capture.write_text("\r\n".join(lines), encoding="utf-8")

    def apply(name: str) -> None:
        if state["applies"]:
            with transcript.open("a", encoding="utf-8") as fh:
                fh.write(_set_model(name))
        paint(AFTER)

    async def fake_resolve(sid, cfg, steps=None):
        live = session.list_sessions.return_value
        row = next((s for s in live if s["session_id"] == sid), None)
        if row is None:
            return "session_not_found", None, None, "", "", None
        return None, "claude", transcript, "claude", "", row

    async def fake_pty(port, sid, keys):
        typed.append((sid, keys))
        if keys == [("/model opus", True)]:
            paint(state["dialog"]) if state["history"] else apply("Opus 5.5")
        elif keys == [("1", False)]:
            apply("Opus 5.5")
        elif keys == [("/model gpt-6-luna", True)] and state["applies"]:
            session.list_sessions.return_value = [
                {**_live(agent="codex"), "live_title": "proj | GPT-6-Luna"}
            ]

    monkeypatch.setattr(router, "_resolve_source", fake_resolve)
    monkeypatch.setattr(router, "_type_into_pty", fake_pty)
    paint(["❯"])
    return client, session, paint, typed, state


def _post(client, model: Any = "opus"):
    return client.post("/api/claude-code/sessions/s1/model", json={"model": model})


def test_the_switch_route_is_passkey_gated():
    from src.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/claude-code/sessions/abc/model") == "passkey"


def test_a_claude_switch_types_the_command_then_confirms_the_dialog(switch_client):
    client, _, _, typed, _ = switch_client
    r = _post(client)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "outcome": "switched", "model": "Opus",
                        "confirmed_dialog": True, "detail": None}
    assert [keys for _sid, keys in typed] == [[("/model opus", True)], [("1", False)]]


def test_a_claude_switch_with_no_history_is_switched_with_no_dialog(switch_client):
    client, _, _, typed, state = switch_client
    state["history"] = False
    body = _post(client).json()
    assert body["outcome"] == "switched" and body["confirmed_dialog"] is False
    assert [keys for _sid, keys in typed] == [[("/model opus", True)]]


def test_a_switch_the_transcript_never_reports_is_not_confirmed(switch_client):
    client, _, _, _, state = switch_client
    state["applies"] = False
    body = _post(client).json()
    assert body["outcome"] == "unconfirmed" and body["model"] == "Opus"
    assert "didn't report" in body["detail"]


def test_a_dialog_for_another_model_is_left_for_the_terminal(switch_client):
    client, _, _, typed, state = switch_client
    state["dialog"] = [line.replace("Opus 5.5", "Fable 5.2") for line in DIALOG]
    body = _post(client).json()
    assert body["outcome"] == "unconfirmed" and "Fable 5.2" in body["detail"]
    assert [keys for _sid, keys in typed] == [[("/model opus", True)]]


def test_the_model_already_running_types_nothing(switch_client):
    client, _, _, typed, _ = switch_client
    body = _post(client, "sonnet").json()
    assert body["outcome"] == "switched" and body["model"] == "Sonnet"
    assert typed == []


@pytest.mark.parametrize("screen,why", [
    (lambda: _screen(PLAN_MODE), "plan"),
    (lambda: DIALOG, "model"),
])
def test_nothing_is_typed_while_the_terminal_waits_on_something_else(switch_client, screen, why):
    client, _, paint, typed, _ = switch_client
    paint(screen())
    r = _post(client)
    assert r.status_code == 409 and why in r.json()["detail"]
    assert typed == []


def test_a_codex_switch_is_confirmed_by_the_window_title(switch_client):
    client, session, _, typed, _ = switch_client
    session.list_sessions.return_value = [{**_live(agent="codex"), "live_title": "proj | GPT-5.6-Terra"}]
    body = _post(client, "gpt-6-luna").json()
    assert body == {"ok": True, "outcome": "switched", "model": "Luna",
                    "confirmed_dialog": False, "detail": None}
    assert [keys for _sid, keys in typed] == [[("/model gpt-6-luna", True)]]


def test_a_codex_title_that_never_changes_is_not_confirmed(switch_client):
    client, session, _, _, state = switch_client
    state["applies"] = False
    session.list_sessions.return_value = [{**_live(agent="codex"), "live_title": "proj | GPT-5.6-Terra"}]
    assert _post(client, "gpt-6-luna").json()["outcome"] == "unconfirmed"


@pytest.mark.parametrize("live,model,status,detail", [
    (_live(agent="pi"), "opus", 409, "can't switch model"),
    (_live(kind="remote"), "opus", 409, "detached"),
    (_live(), "gpt-6-luna", 400, "model must be"),
    (_live(), None, 400, "model must be"),
])
def test_a_switch_that_cannot_be_driven_types_nothing(switch_client, live, model, status, detail):
    client, session, _, typed, _ = switch_client
    session.list_sessions.return_value = [live]
    r = _post(client, model)
    assert r.status_code == status and detail in r.json()["detail"]
    assert typed == []


def test_a_gone_session_and_an_unreachable_host_are_told_apart(switch_client):
    from src.clients.session import SessionHostError

    client, session, _, typed, _ = switch_client
    session.list_sessions.return_value = []
    assert _post(client).status_code == 409
    session.list_sessions.side_effect = SessionHostError("timed out")
    assert _post(client).status_code == 503
    assert typed == []


def test_the_switch_is_audited_without_the_conversation(switch_client, monkeypatch):
    from src import audit

    client, _, _, _, _ = switch_client
    logged: List[Any] = []
    monkeypatch.setattr(audit, "session_log", lambda sid, event, **kw: logged.append((sid, event, kw)))
    _post(client)
    assert logged == [("s1", "model_switch", {"agent": "claude", "model": "opus",
                                              "confirmed_dialog": True, "reason": "switched"})]



def test_nothing_is_typed_while_claude_is_mid_turn(switch_client):
    """A /model typed mid-turn queues behind the turn, and its dialog would
    then wait on the terminal with nobody to answer it."""
    client, session, _, typed, _ = switch_client
    session.list_sessions.return_value = [{**_live(), "live_title": "\u2810 Fixing the login"}]
    r = _post(client)
    assert r.status_code == 409 and "working" in r.json()["detail"]
    assert typed == []
