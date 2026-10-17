"""scripts/telegram_relay.py (#1639): states, exit codes and ask's reply wait.

No network: the one HTTP helper is replaced by a scripted fake, and sleeping
is a no-op.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from scripts import telegram_relay as relay

OLD = "2026-10-17T10:00:00Z"
NEW = "2026-10-17T10:05:00Z"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("LAUNCHER_RELAY_URL", "https://127.0.0.1:8445")
    monkeypatch.setenv("LAUNCHER_RELAY_PROFILE", "health")
    monkeypatch.setenv("LAUNCHER_RELAY_TOKEN", "fictional-token")
    monkeypatch.setattr(relay.time, "sleep", lambda s: None)


def _script(monkeypatch, replies):
    """Replace _call with one that answers from ``replies`` per (method, path)."""
    calls = []

    def fake(method, path, *, query=None, body=None, timeout=30.0):
        calls.append((method, path, body))
        queue = replies[(method, path)]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(relay, "_call", fake)
    return calls


def _reply(stamp, status="idle", text="answer"):
    return {"state": "reply", "text": text, "timestamp": stamp, "status": status}


def test_ask_returns_the_new_finished_reply(env, monkeypatch, capsys):
    calls = _script(monkeypatch, {
        ("GET", "read"): [_reply(OLD, text="old"), _reply(NEW, status="working", text="partial"),
                          _reply(NEW, text="done")],
        ("POST", "steer"): [{"state": "delivered"}],
    })
    assert relay.main(["ask", "conv-1", "how", "are", "you"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("STATE=reply\n") and out.rstrip().endswith("done")
    assert "partial" not in out and "old" not in out
    steers = [c for c in calls if c[1] == "steer"]
    assert steers == [("POST", "steer", {"session_id": "conv-1", "data": "how are you"})]


def test_ask_never_passes_old_text_off_as_new(env, monkeypatch, capsys):
    monkeypatch.setattr(relay.time, "monotonic", iter(range(0, 10_000, 50)).__next__)
    calls = _script(monkeypatch, {
        ("GET", "read"): [_reply(OLD, text="old")],
        ("POST", "steer"): [{"state": "delivered"}],
    })
    assert relay.main(["ask", "conv-1", "hi", "--timeout", "120"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("STATE=no_reply_yet\n") and "old" not in out
    assert "do not send it again" in out
    assert sum(1 for c in calls if c[1] == "steer") == 1


def test_ask_on_a_busy_conversation_reports_queued_without_waiting(env, monkeypatch, capsys):
    # A fake clock, so a wait that should not happen ends at once instead of
    # running the real two minutes.
    monkeypatch.setattr(relay.time, "monotonic", iter(range(0, 10_000, 50)).__next__)
    calls = _script(monkeypatch, {
        ("GET", "read"): [_reply(OLD, status="working")],
        ("POST", "steer"): [{"state": "queued"}],
    })
    assert relay.main(["ask", "conv-1", "hi"]) == 0
    assert capsys.readouterr().out.startswith("STATE=queued\n")
    assert [c[1] for c in calls] == ["read", "steer"]


@pytest.mark.parametrize("state", ["not_this_skill", "not_found", "refused", "host_unreachable"])
def test_ask_sends_nothing_when_the_target_is_refused(env, monkeypatch, capsys, state):
    calls = _script(monkeypatch, {("GET", "read"): [{"state": state, "message": "no"}]})
    assert relay.main(["ask", "conv-1", "hi"]) == 1
    assert capsys.readouterr().out.startswith(f"STATE={state}\n")
    assert all(c[1] != "steer" for c in calls)


def test_a_stranded_steer_is_reported_not_retried(env, monkeypatch, capsys):
    calls = _script(monkeypatch, {
        ("GET", "read"): [_reply(OLD)],
        ("POST", "steer"): [{"state": "stranded", "message": "not ingested"}],
    })
    assert relay.main(["ask", "conv-1", "hi"]) == 1
    assert capsys.readouterr().out.startswith("STATE=stranded\n")
    assert sum(1 for c in calls if c[1] == "steer") == 1


def test_without_a_board_status_a_reply_waits_for_quiet_output(env, monkeypatch, capsys):
    """No hook row (a freshly resumed conversation): the host's epoch-seconds
    ``last_output_at`` decides, and recent output means still streaming."""
    now = relay.datetime.now(relay.timezone.utc).timestamp()
    busy = {**_reply(NEW, status=None, text="partial"), "last_output_at": now}
    quiet = {**_reply(NEW, status=None, text="done"), "last_output_at": now - 60}
    _script(monkeypatch, {
        ("GET", "read"): [_reply(OLD, text="old"), busy, quiet],
        ("POST", "steer"): [{"state": "delivered"}],
    })
    assert relay.main(["ask", "conv-1", "hi"]) == 0
    assert capsys.readouterr().out.rstrip().endswith("done")


def test_missing_environment_is_its_own_exit(monkeypatch, capsys):
    for name in ("LAUNCHER_RELAY_URL", "LAUNCHER_RELAY_PROFILE", "LAUNCHER_RELAY_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert relay.main(["find"]) == 2
    assert capsys.readouterr().out.startswith("STATE=refused\n")


def test_a_non_loopback_url_is_refused(env, monkeypatch, capsys):
    monkeypatch.setenv("LAUNCHER_RELAY_URL", "https://example.com:8445")
    assert relay.main(["find"]) == 2


def test_http_refusals_keep_the_routes_state(env, monkeypatch):
    def refuse(request, timeout, context):
        raise urllib.error.HTTPError(
            request.full_url, 403, "Forbidden", {},
            io.BytesIO(json.dumps({"detail": {"state": "not_this_skill",
                                              "message": "not yours"}}).encode()))

    monkeypatch.setattr(relay.urllib.request, "urlopen", refuse)
    assert relay._call("GET", "read", query={"session_id": "x"}) == {
        "state": "not_this_skill", "message": "not yours"}


def test_an_unreachable_launcher_is_unknown_not_stranded(env, monkeypatch):
    def down(request, timeout, context):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(relay.urllib.request, "urlopen", down)
    assert relay._call("POST", "steer", body={})["state"] == "unknown"


def test_the_token_rides_a_header_never_the_url(env, monkeypatch):
    seen = {}

    def capture(request, timeout, context):
        seen.update(url=request.full_url, token=request.get_header("X-relay-token"))
        raise urllib.error.URLError("stop")

    monkeypatch.setattr(relay.urllib.request, "urlopen", capture)
    relay._call("GET", "conversations", query={"q": "ferry"})
    assert seen["token"] == "fictional-token" and "fictional-token" not in seen["url"]
    assert seen["url"].startswith(
        "https://127.0.0.1:8445/api/life-os/channels/health/relay/conversations?")
