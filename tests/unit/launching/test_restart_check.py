"""A restart that did not replace the webapp process must fail the check."""

from __future__ import annotations

import json

import pytest

from src.launching import restart_check

HEAD = "86224f1b"


def _version(pid, sha=HEAD):
    body = {"git_sha": sha}
    if pid is not None:
        body["pid"] = pid
    return body


class TestDecide:
    def test_same_pid_at_head_is_not_a_restart(self):
        ok, reason = restart_check.decide({"pid": 100}, _version(100), HEAD)
        assert not ok
        assert "same process" in reason

    def test_new_pid_at_head_passes(self):
        ok, _ = restart_check.decide({"pid": 100}, _version(200), HEAD)
        assert ok

    def test_nothing_running_before_accepts_any_new_process(self):
        ok, _ = restart_check.decide({"pid": None}, _version(200), HEAD)
        assert ok

    def test_new_pid_on_the_wrong_sha_fails(self):
        ok, reason = restart_check.decide({"pid": 100}, _version(200, sha="deadbeef"), HEAD)
        assert not ok
        assert "expected HEAD" in reason

    def test_nothing_answering_fails(self):
        ok, _ = restart_check.decide({"pid": 100}, None, HEAD)
        assert not ok

    def test_an_answer_without_a_pid_is_not_confirmed(self):
        ok, reason = restart_check.decide({"pid": 100}, _version(None), HEAD)
        assert not ok
        assert "no pid" in reason

    def test_unknown_sha_never_passes(self):
        ok, _ = restart_check.decide({"pid": 100}, _version(200, sha="unknown"), HEAD)
        assert not ok


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(restart_check, "BASELINE_FILE", tmp_path / "baseline.json")
    monkeypatch.setattr(restart_check, "RESTART_LOG", tmp_path / "restart.log")
    monkeypatch.setattr(restart_check, "resolve_git_sha", lambda: HEAD)
    monkeypatch.setattr(restart_check.time, "sleep", lambda _s: None)
    return tmp_path


class TestVerify:
    def test_unchanged_process_exits_nonzero_and_logs(self, files, monkeypatch):
        monkeypatch.setattr(restart_check, "fetch_version", lambda _p: _version(100))
        assert restart_check.snapshot(8445) == 0
        assert restart_check.verify(8445, timeout_s=0) == 1
        log = (files / "restart.log").read_text(encoding="utf-8")
        assert "verify FAILED" in log and "pid 100" in log

    def test_replaced_process_passes(self, files, monkeypatch):
        monkeypatch.setattr(restart_check, "fetch_version", lambda _p: _version(100))
        restart_check.snapshot(8445)
        monkeypatch.setattr(restart_check, "fetch_version", lambda _p: _version(200))
        assert restart_check.verify(8445, timeout_s=0) == 0

    def test_missing_baseline_is_not_confirmed(self, files, monkeypatch):
        monkeypatch.setattr(restart_check, "fetch_version", lambda _p: _version(200))
        assert restart_check.verify(8445, timeout_s=0) == 2
        assert "NOT CONFIRMED" in (files / "restart.log").read_text(encoding="utf-8")

    def test_baseline_for_another_port_is_not_confirmed(self, files, monkeypatch):
        (files / "baseline.json").write_text(json.dumps({"port": 9999, "pid": 1}), encoding="utf-8")
        monkeypatch.setattr(restart_check, "fetch_version", lambda _p: _version(200))
        assert restart_check.verify(8445, timeout_s=0) == 2

    def test_snapshot_with_nothing_running_records_no_pid(self, files, monkeypatch):
        monkeypatch.setattr(restart_check, "fetch_version", lambda _p: None)
        restart_check.snapshot(8445)
        assert json.loads((files / "baseline.json").read_text(encoding="utf-8"))["pid"] is None
