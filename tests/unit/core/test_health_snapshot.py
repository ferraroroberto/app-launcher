"""The health snapshot: every section reads, none of them changes anything.

Every process, path, token and figure here is invented.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from src.request_timing import snapshot as hs

NOW = datetime(2026, 3, 4, 12, 0, 0)
ROWS = [
    ("busy.exe", 11, 180.0, 300.0),
    ("claude.exe", 22, 40.0, 500.0),
    ("codex.exe", 23, 5.0, 450.0),
    ("idle.exe", 33, 0.0, 10.0),
]
STATS = {
    "inflight": 2, "max_inflight": 17, "window_s": 300.0, "uptime_s": 99.0,
    "verdicts": {"board": {"ok": 3, "over": 1, "breach": 2}}, "git_status_age_s": 41.5,
}


@pytest.fixture
def sources(tmp_path, monkeypatch) -> hs.Sources:
    monkeypatch.setattr(hs, "listener_pids", lambda port: [4242] if port == 1 else [])
    managed = tmp_path / "chief-managed.json"
    managed.write_text(json.dumps({"lane-a": {}, "lane-b": {}}), encoding="utf-8")
    return hs.Sources(
        webapp_port=1, session_host_port=2, managed_file=managed,
        sessions_file=tmp_path / "sessions-state.json",
    )


def _text(sources, rows=ROWS, stats=STATS, why="", tail=("a slow line",)) -> str:
    return hs.build_text(sources, now=NOW, rows=rows, stats=stats, stats_why=why, slow_tail=list(tail))


def test_report_names_load_agents_lanes_requests_and_the_tail(sources):
    text = _text(sources)
    assert "webapp :1: pid 4242" in text
    assert "session-host :2: not listening" in text
    assert "busy.exe pid 11: 180%" in text
    assert "agent processes: 2" in text
    assert "lane marks (chief-managed): 2" in text
    assert "session rows: absent" in text
    assert "peak 17" in text
    assert "git-status cache age: 41.5s" in text
    assert "board: 3 ok, 1 over soft, 2 breach" in text
    assert "  a slow line" in text
    assert "memory:" in text


def test_an_unreadable_socket_table_reads_unknown_not_not_listening(sources, monkeypatch):
    monkeypatch.setattr(hs, "listener_pids", lambda port: None)
    assert "webapp :1: unknown (socket table unreadable)" in _text(sources)


def test_the_idle_pseudo_process_is_not_a_cpu_consumer(sources):
    text = _text(sources, rows=[("System Idle Process", 0, 1200.0, 0.0), *ROWS])
    assert "System Idle Process" not in text


def test_a_down_webapp_reads_unknown_never_idle(sources):
    text = _text(sources, stats=None, why="webapp on :1 unreachable (ConnectError)")
    assert "requests: unknown (webapp on :1 unreachable (ConnectError))" in text
    assert "git-status cache age: unknown" in text


def test_missing_psutil_rows_read_unknown(sources):
    text = _text(sources, rows=None)
    assert "top CPU: unknown" in text and "agent processes: unknown" in text


def test_a_scan_that_has_not_run_is_not_a_zero_age(sources):
    text = _text(sources, stats={**STATS, "git_status_age_s": None})
    assert "git-status cache age: no scan yet" in text


def test_save_writes_one_redacted_file_and_touches_nothing_else(sources, tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "SLOW_LOG", tmp_path / "slow.log")
    (tmp_path / "slow.log").write_text("GET /x Authorization: Bearer leakyleaky\n", encoding="utf-8")
    out = tmp_path / "health"
    before = {p.name for p in tmp_path.iterdir()}
    snap = hs.save_snapshot(
        sources, literals=("leakyleaky",), out_dir=out, now=NOW,
        sampler=lambda: ROWS, stats_reader=lambda port: (STATS, ""),
    )
    assert snap.path == out / "health-20260304-120000.txt"
    assert snap.path.read_text(encoding="utf-8") == snap.text
    assert "leakyleaky" not in snap.text
    assert {p.name for p in tmp_path.iterdir()} - before == {"health"}
    assert [p.name for p in out.iterdir()] == [snap.path.name]


def test_an_unreachable_webapp_is_reported_by_the_reader(monkeypatch):
    def refuse(*args, **kwargs):
        raise hs.httpx.ConnectError("refused")

    monkeypatch.setattr(hs.httpx, "get", refuse)
    stats, why = hs.fetch_request_stats(8445)
    assert stats is None and "unreachable" in why and "ConnectError" in why
