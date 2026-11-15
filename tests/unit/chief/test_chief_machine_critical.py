"""The machine-critical chief mailbox event.

The hub's verdict is stubbed as the parsed reading; the inbox and the open
incident file are always a ``tmp_path``, so nothing here touches the real
mailbox, the real hub or a real process table. Time is a fixed ``datetime``.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.webapp.chief_doorbell import count_pending
from src import instance_role
from src.chief import machine_critical as mc
from src.clients import hub_health

T0 = datetime(2026, 11, 15, 16, 20, 5, tzinfo=timezone.utc)


def _cfg(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        chief_inbox_dir=str(tmp_path / "chief-inbox"),
        llm_hub_url="http://127.0.0.1:8000",
        projects_dir=str(tmp_path / "projects"),
    )


def _events(cfg) -> list:
    inbox = Path(cfg.chief_inbox_dir)
    return [json.loads(p.read_text("utf-8")) for p in sorted(inbox.glob("*.json"))] if inbox.exists() else []


def _holder(name="example-app-webapp", pid=4242, value=18400):
    return {"name": name, "pid": pid, "value": value, "tcp": value, "udp": 0}


def _reading(*critical, verdict="act", host="example-host", holders=None):
    return {
        "verdict": verdict, "why": None, "reason": "r", "fix": None,
        "machine": {
            "host": host, "ports_percent": 82.0, "commit_percent": 40.0,
            "holders": [_holder()] if holders is None else holders, "memory_holders": [],
            "last_event": None,
            "critical": [{"metric": m, "value": 82.0} for m in critical],
        },
    }


def _fake_classify(row, _projects_dir, **_):
    return {"name": row["name"], "pid": row["pid"], "owner": "fleet", "repo": "example-app", "value": row["value"]}


@pytest.fixture(autouse=True)
def _canonical(monkeypatch, tmp_path):
    monkeypatch.setattr(instance_role, "canonical_instance", lambda *a, **k: (True, "canonical"))
    monkeypatch.setattr(mc, "STATE_FILE", tmp_path / "open.json")
    mc.reset()


def _observe(cfg, reading, minutes=0, **kw):
    return mc.observe(cfg, reading, T0 + timedelta(minutes=minutes), classify_row=_fake_classify, **kw)


def test_an_act_on_ports_publishes_one_event_with_the_contract_shape(tmp_path):
    cfg = _cfg(tmp_path)
    (path,) = _observe(cfg, _reading("tcp_percent"))
    (event,) = _events(cfg)
    assert path.parent == Path(cfg.chief_inbox_dir)
    assert event.pop("id")
    assert event == {
        "v": 2, "kind": "machine-critical", "event": "MachineCritical",
        "host": "example-host", "metric": "tcp_percent", "value": 82.0, "threshold": None,
        "offenders": [{"name": "example-app-webapp", "pid": 4242, "owner": "fleet",
                       "repo": "example-app", "value": 18400}],
        "verdict_url": "http://127.0.0.1:8000/admin/api/machines/health",
        "first_seen": "2026-11-15T16:20:05Z", "launcher_sid": None, "repo": None, "number": None,
        "agent": None, "dispatch_id": None, "ts": "2026-11-15T16:20:05Z",
    }
    assert count_pending(Path(cfg.chief_inbox_dir), int(time.time() * 1000)) == 1  # the doorbell rings on it


def test_a_second_poll_of_the_same_incident_publishes_none(tmp_path):
    cfg = _cfg(tmp_path)
    _observe(cfg, _reading("tcp_percent"))
    assert _observe(cfg, _reading("tcp_percent"), minutes=1) == []
    assert len(_events(cfg)) == 1


def test_clearing_then_re_tripping_publishes_a_new_incident(tmp_path):
    cfg = _cfg(tmp_path)
    _observe(cfg, _reading("tcp_percent"))
    assert _observe(cfg, _reading(verdict="ok"), minutes=5) == []
    _observe(cfg, _reading("tcp_percent"), minutes=10)
    first, second = _events(cfg)
    assert first["first_seen"] == "2026-11-15T16:20:05Z"
    assert second["first_seen"] == "2026-11-15T16:30:05Z"


def test_an_incident_open_across_a_restart_publishes_nothing_new(tmp_path):
    cfg = _cfg(tmp_path)
    _observe(cfg, _reading("tcp_percent"))
    mc.reset()  # a new process: only the file remembers
    assert _observe(cfg, _reading("tcp_percent"), minutes=2) == []
    assert len(_events(cfg)) == 1


@pytest.mark.parametrize("reading", [
    {"verdict": "unknown", "why": "unreachable", "reason": "down", "fix": None},
    {"verdict": "unknown", "why": "hub", "reason": "no sample", "fix": None},
    {"verdict": "unknown", "why": "disabled", "reason": None, "fix": None},
])
def test_an_unreadable_hub_never_publishes(tmp_path, reading):
    cfg = _cfg(tmp_path)
    assert _observe(cfg, reading) == []
    assert _events(cfg) == []


def test_an_unreadable_poll_neither_closes_nor_repeats_an_incident(tmp_path):
    cfg = _cfg(tmp_path)
    _observe(cfg, _reading("tcp_percent"))
    _observe(cfg, {"verdict": "unknown", "why": "unreachable", "reason": "down", "fix": None}, minutes=1)
    assert _observe(cfg, _reading("tcp_percent"), minutes=2) == []
    assert len(_events(cfg)) == 1


def test_each_critical_metric_is_its_own_incident_and_a_clear_closes_only_its_own(tmp_path):
    cfg = _cfg(tmp_path)
    assert len(_observe(cfg, _reading("tcp_percent", "commit_percent"))) == 2
    assert _observe(cfg, _reading("commit_percent"), minutes=1) == []  # ports cleared
    assert len(_observe(cfg, _reading("tcp_percent", "commit_percent"), minutes=2)) == 1  # ports again
    assert sorted(e["metric"] for e in _events(cfg)) == ["commit_percent", "tcp_percent", "tcp_percent"]


def test_a_failed_write_is_retried_on_the_next_poll(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    real = mc.publish_event

    def broken(*_a, **_k):
        raise OSError("disk")

    monkeypatch.setattr(mc, "publish_event", broken)
    assert _observe(cfg, _reading("tcp_percent")) == []
    monkeypatch.setattr(mc, "publish_event", real)
    assert len(_observe(cfg, _reading("tcp_percent"), minutes=1)) == 1


def test_a_non_canonical_instance_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(instance_role, "canonical_instance", lambda *a, **k: (False, "worktree"))
    cfg = _cfg(tmp_path)
    assert _observe(cfg, _reading("tcp_percent")) == []
    assert _events(cfg) == [] and not mc.STATE_FILE.exists()


def test_commit_names_the_memory_holders_as_offenders(tmp_path):
    cfg = _cfg(tmp_path)
    reading = _reading("commit_percent")
    reading["machine"]["memory_holders"] = [{"name": "llama-server.exe", "pid": 7, "value": 24998.8}]
    _observe(cfg, reading)
    (event,) = _events(cfg)
    assert [o["name"] for o in event["offenders"]] == ["llama-server.exe"]


@pytest.mark.parametrize("metric", ["tcpip_4231", "udp_percent", "ephemeral_ports_percent", "commit_percent"])
def test_the_hub_findings_that_count_as_critical(metric):
    assert hub_health.is_critical_metric(metric)


@pytest.mark.parametrize("metric", ["tcp_time_wait", "total_handles", "ram_available_percent", None])
def test_other_findings_are_not_critical(metric):
    assert not hub_health.is_critical_metric(metric)


class TestClassify:
    def _row(self, name="pythonw.exe", pid=11):
        return {"name": name, "pid": pid, "value": 33}

    def _run(self, tmp_path, row, live, folder):
        projects = tmp_path / "projects"
        (projects / "example-app").mkdir(parents=True, exist_ok=True)
        return mc.classify(row, str(projects), live_name=lambda pid: live, owner_folder=lambda pid: folder)

    def test_an_interpreter_in_a_fleet_repo_is_fleet_and_names_the_repo(self, tmp_path):
        got = self._run(tmp_path, self._row(), "pythonw.exe", "example-app")
        assert (got["owner"], got["repo"]) == ("fleet", "example-app")

    def test_a_worktree_folder_resolves_to_its_repo(self, tmp_path):
        got = self._run(tmp_path, self._row(), "pythonw.exe", "example-app-wt-2442")
        assert (got["owner"], got["repo"]) == ("fleet", "example-app")

    def test_a_non_interpreter_in_a_repo_folder_is_not_the_apps_process(self, tmp_path):
        got = self._run(tmp_path, self._row("claude.exe"), "claude.exe", "example-app")
        assert (got["owner"], got["repo"]) == ("non-fleet", None)

    def test_an_interpreter_outside_every_fleet_repo_is_non_fleet(self, tmp_path):
        got = self._run(tmp_path, self._row(), "pythonw.exe", "somewhere-else")
        assert (got["owner"], got["repo"]) == ("non-fleet", None)

    def test_a_gone_or_reused_pid_is_unknown_never_guessed(self, tmp_path):
        assert self._run(tmp_path, self._row(), None, "example-app")["owner"] == "unknown"
        assert self._run(tmp_path, self._row(), "chrome.exe", "example-app")["owner"] == "unknown"

    def test_a_row_without_a_pid_is_unknown(self, tmp_path):
        assert self._run(tmp_path, {"name": "x.exe", "pid": None, "value": 1}, "x.exe", None)["owner"] == "unknown"
