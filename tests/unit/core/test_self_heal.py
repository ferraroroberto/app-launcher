"""Tray self-heal: grace period, bounded restarts, and the tray wiring."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import List

from app.tray import tray
from app.tray.self_heal import (
    WEDGE_GRACE_PROBES,
    RestartBudget,
    SelfHealer,
    should_heal_webapp,
)
from app.webapp import observability
from app.webapp.manager import (
    HEALTH_ANSWERING,
    HEALTH_BOUND_NOT_ANSWERING,
    HEALTH_DOWN,
    OWNERSHIP_NONE,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_dead_webapp_heals_at_the_alert_but_a_wedge_waits_out_the_grace():
    assert should_heal_webapp(HEALTH_DOWN, 3, 3)
    assert not should_heal_webapp(HEALTH_ANSWERING, 99, 3)
    assert not should_heal_webapp(HEALTH_BOUND_NOT_ANSWERING, 3 + WEDGE_GRACE_PROBES - 1, 3)
    assert should_heal_webapp(HEALTH_BOUND_NOT_ANSWERING, 3 + WEDGE_GRACE_PROBES, 3)


def test_budget_backs_off_exponentially_then_caps_per_hour():
    clock = _Clock()
    budget = RestartBudget(max_per_hour=3, base_backoff_s=100, max_backoff_s=1000, clock=clock)
    assert budget.blocked_reason() is None
    budget.record()
    clock.now = 99
    assert "backing off 100s" in budget.blocked_reason()
    clock.now = 100
    assert budget.blocked_reason() is None
    budget.record()
    clock.now = 299
    assert "backing off 200s" in budget.blocked_reason()
    clock.now = 300
    budget.record()
    # Three attempts inside the hour: the cap holds even with no back-off left.
    budget.reset()
    clock.now = 3599
    assert "hourly cap" in budget.blocked_reason()
    clock.now = 3600  # the first attempt ages out of the window
    assert budget.blocked_reason() is None


def test_crash_loop_never_restarts_faster_than_the_cap():
    clock = _Clock()
    runs: List[float] = []
    healer = SelfHealer(
        "webapp",
        lambda: runs.append(clock.now) or "done",
        RestartBudget(max_per_hour=3, base_backoff_s=120, clock=clock),
        log=lambda _msg: None,
    )
    # A webapp that dies at once: the watchdog asks every 60 s for two hours.
    for minute in range(120):
        clock.now = minute * 60.0
        healer.attempt("down")
    # Back-off doubles while it stays dead; the hourly cap holds the 4th to 3600 s.
    assert runs == [0.0, 120.0, 360.0, 3600.0, 4560.0, 6360.0]


def test_deferral_logs_once_per_streak_and_failures_are_logged_not_raised():
    clock = _Clock()
    lines: List[str] = []

    def _boom() -> str:
        raise RuntimeError("bind failed")

    healer = SelfHealer("webapp", _boom, RestartBudget(clock=clock), lines.append)
    assert healer.attempt("t1") is True
    assert healer.attempt("t2") is False
    assert healer.attempt("t3") is False
    assert lines == [
        "SELF-HEAL webapp restarting (t1)",
        "SELF-HEAL webapp restart failed: RuntimeError: bind failed",
        lines[2],
    ]
    assert lines[2].startswith("SELF-HEAL webapp deferred (t2): backing off")


class _FakeManager:
    def __init__(self, healths: List[str]) -> None:
        self.healths = healths
        self.config = SimpleNamespace(enabled=True, port=18445)
        self.calls: List[str] = []

    def probe_health(self) -> str:
        return self.healths.pop(0) if len(self.healths) > 1 else self.healths[0]

    def child_exit_code(self):
        return 1

    def status(self):
        return SimpleNamespace(ownership=OWNERSHIP_NONE)

    def stop(self):
        self.calls.append("stop")

    def start(self, wait: bool = True):
        self.calls.append("start")
        return SimpleNamespace(health=HEALTH_ANSWERING, detail="running")


def _tray(manager: _FakeManager, monkeypatch, lines: List[str]) -> tray.TrayApp:
    app = tray.TrayApp.__new__(tray.TrayApp)
    app.manager = manager
    app.webapp_healer = SelfHealer(
        "webapp", app._restart_webapp_for_heal, RestartBudget(), lines.append
    )
    return app


def test_wedged_webapp_is_stopped_then_reclaimed_then_started(monkeypatch):
    lines: List[str] = []
    reclaimed: List[int] = []
    monkeypatch.setattr(tray, "_reclaim_port", lambda port: reclaimed.append(port))
    manager = _FakeManager([HEALTH_BOUND_NOT_ANSWERING] * 3 + [HEALTH_ANSWERING])
    app = _tray(manager, monkeypatch, lines)
    app._heal_webapp(3 + WEDGE_GRACE_PROBES)
    assert manager.calls == ["stop", "start"]
    assert reclaimed == [18445]
    assert lines[0].startswith("SELF-HEAL webapp restarting (13 failed probes, health=bound_not_answering")
    assert "process exited with code 1" in lines[0]
    assert lines[1] == "SELF-HEAL webapp done: running"


def test_webapp_answering_again_before_the_heal_is_left_alone(monkeypatch):
    lines: List[str] = []
    manager = _FakeManager([HEALTH_ANSWERING])
    app = _tray(manager, monkeypatch, lines)
    app._heal_webapp(50)
    assert manager.calls == [] and lines == []


def test_session_host_heal_spawns_only_when_nothing_listens(monkeypatch):
    spawned: List[list] = []
    monkeypatch.setattr(tray, "_session_host_port", lambda: 18446)
    monkeypatch.setattr(tray.subprocess, "Popen", lambda cmd, **_kw: spawned.append(cmd))
    monkeypatch.setattr(tray, "SESSION_HOST_LOG", Path("nul") if tray.sys.platform == "win32" else Path("/dev/null"))
    app = tray.TrayApp.__new__(tray.TrayApp)

    monkeypatch.setattr(tray.registered_trays, "port_listening", lambda _p: True)
    assert app._start_session_host() == "adopted the session-host already listening on :18446"
    assert spawned == []

    monkeypatch.setattr(tray.registered_trays, "port_listening", lambda _p: False)
    assert app._start_session_host() == "spawned a detached session-host on :18446"
    assert spawned[0][-3:] == ["session-host", "--port", "18446"]


def test_session_host_watchdog_never_kills(monkeypatch):
    """The session-host path has no stop or reclaim in it: a listening one,
    however slow, counts as up, and a dead one is only respawned."""
    called: List[str] = []
    monkeypatch.setattr(tray, "_reclaim_port", lambda p: called.append("reclaim"))
    monkeypatch.setattr(tray.TrayApp, "_stop_session_host", lambda self: called.append("stop"))
    app = tray.TrayApp.__new__(tray.TrayApp)
    app.session_host_healer = SelfHealer(
        "session-host", lambda: called.append("start") or "spawned", RestartBudget(), lambda _m: None
    )
    app._heal_session_host(2)
    assert called == ["start"]


def test_webapp_warning_lands_in_the_rotating_log(tmp_path, monkeypatch):
    path = tmp_path / "webapp-warnings.log"
    monkeypatch.setattr(observability, "_WARNINGS_LOG_PATH", path)
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        observability.ensure_warning_log_handler()
        observability.ensure_warning_log_handler()  # idempotent
        added = [h for h in root.handlers if h not in before]
        assert len(added) == 1
        assert isinstance(added[0], logging.handlers.RotatingFileHandler)
        logging.getLogger("app.webapp.probe").warning("⚠️ probe warning")
        logging.getLogger("app.webapp.probe").info("not persisted")
        added[0].flush()
        text = path.read_text(encoding="utf-8")
        assert "[WARNING] app.webapp.probe: ⚠️ probe warning" in text
        assert "not persisted" not in text
    finally:
        for h in root.handlers[:]:
            if h not in before:
                root.removeHandler(h)
                h.close()

