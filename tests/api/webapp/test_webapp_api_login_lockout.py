"""POST /api/login lock-out: 429 + Retry-After, the guessing alert, who is exempt.

Time is a fake clock handed to the lock-out and the alert goes to a fake
notifier on the calling thread, so nothing sleeps and nothing leaves the box.
"""

from __future__ import annotations

import logging
from typing import List

from fastapi.testclient import TestClient

from app.webapp.routers.auth import APP_NAME, build_login_lockout

PASSWORD = "hunter2"
TOKEN = "secret-token"
WRONG = "not-the-password"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: List[str] = []

    def send_text(self, text: str) -> None:
        self.sent.append(text)


def _arm(app, clock: FakeClock, notifier: FakeNotifier) -> None:
    cfg = app.state.webapp_config
    cfg.auth_password = PASSWORD
    cfg.auth_token = TOKEN
    app.state.login_lockout = build_login_lockout(
        lambda: app.state.webapp_config,
        make_notifier=lambda _cfg: notifier,
        spawn=lambda fn: fn(),
        clock=clock,
    )


def _login(client, password=WRONG, headers=None):
    return client.post("/api/login", json={"password": password}, headers=headers)


def test_sixth_wrong_password_is_429_then_correct_one_works_after_the_wait(webapp_client):
    client, app, _ = webapp_client
    clock = FakeClock()
    _arm(app, clock, FakeNotifier())

    # Five free wrong passwords; the sixth is still a 401 but starts the wait.
    assert [_login(client).status_code for _ in range(6)] == [401] * 6
    locked = _login(client)
    assert locked.status_code == 429
    assert int(locked.headers["Retry-After"]) >= 1

    # A locked caller is refused even with the right password, and not counted.
    assert _login(client, PASSWORD).status_code == 429

    clock.now += 3
    ok = _login(client, PASSWORD)
    assert ok.status_code == 200
    assert ok.json() == {"token": TOKEN}


def test_three_wrong_passwords_alert_once(webapp_client):
    client, app, _ = webapp_client
    notifier = FakeNotifier()
    _arm(app, FakeClock(), notifier)

    _login(client)
    _login(client)
    assert notifier.sent == []
    _login(client)
    assert len(notifier.sent) == 1
    text = notifier.sent[0]
    assert APP_NAME in text and "testclient" in text and "3" in text
    assert WRONG not in text and PASSWORD not in text and TOKEN not in text

    for _ in range(2):
        _login(client)
    assert len(notifier.sent) == 1


def test_the_pc_itself_is_never_locked_out(webapp_client):
    _, app, _ = webapp_client
    notifier = FakeNotifier()
    _arm(app, FakeClock(), notifier)
    pc = TestClient(app, client=("127.0.0.1", 50000))

    assert [_login(pc).status_code for _ in range(8)] == [401] * 8
    assert _login(pc, PASSWORD).status_code == 200
    assert notifier.sent == []


def test_tunnel_callers_over_loopback_are_counted_by_their_own_address(webapp_client):
    _, app, _ = webapp_client
    notifier = FakeNotifier()
    _arm(app, FakeClock(), notifier)
    tunnel = TestClient(app, client=("::1", 50000))
    stranger = {"cf-ray": "x", "cf-connecting-ip": "203.0.113.7"}
    owner = {"cf-ray": "y", "cf-connecting-ip": "198.51.100.2"}

    assert [_login(tunnel, headers=stranger).status_code for _ in range(6)] == [401] * 6
    assert _login(tunnel, headers=stranger).status_code == 429
    # Not the PC despite the loopback socket, and not sharing the stranger's counter.
    assert _login(tunnel, PASSWORD, headers=owner).status_code == 200
    assert "203.0.113.7" in notifier.sent[0]


def test_a_non_loopback_caller_cannot_choose_its_own_key(webapp_client):
    client, app, _ = webapp_client
    _arm(app, FakeClock(), FakeNotifier())

    codes = [
        _login(client, headers={"cf-ray": "x", "cf-connecting-ip": f"203.0.113.{n}"}).status_code
        for n in range(7)
    ]
    assert codes == [401] * 6 + [429]


def test_no_alert_channel_is_reported_unconfigured(webapp_client, caplog):
    client, app, _ = webapp_client
    _arm(app, FakeClock(), FakeNotifier())
    app.state.login_lockout = build_login_lockout(
        lambda: app.state.webapp_config,
        make_notifier=lambda _cfg: None,
        spawn=lambda fn: fn(),
        clock=FakeClock(),
    )
    with caplog.at_level(logging.WARNING, logger="src.login_alert"):
        for _ in range(3):
            _login(client)
    assert "unconfigured" in caplog.text and "3 wrong passwords" in caplog.text
