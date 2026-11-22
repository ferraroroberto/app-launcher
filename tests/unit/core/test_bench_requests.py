"""The benchmark's arithmetic and its report, with invented samples.

The benchmark itself runs against a live webapp; what is pinned here is what it
makes of a ``Server-Timing`` header and how it states a verdict.
"""

from __future__ import annotations

from scripts import bench_requests as br


def test_server_parts_reads_queue_worker_and_app():
    header = 'queue;dur=12.5;desc="hot-poll", worker;dur=3.0;desc="hot-poll", app;dur=20.0'
    assert br.server_parts(header) == {"queue": 12.5, "worker": 3.0, "app": 20.0}
    assert br.server_parts(None) == {}
    assert br.server_parts("app;dur=1.0") == {"app": 1.0}


def test_summarise_gives_percentiles_and_median_server_split():
    samples = [(float(ms), {"queue": 1.0, "worker": 2.0}) for ms in range(10, 110, 10)]
    row = br.summarise(samples)
    assert row["samples"] == 10
    assert row["p50_ms"] == 55.0
    assert row["p95_ms"] == 100.0
    assert row["max_ms"] == 100.0
    assert row["queue_p50_ms"] == 1.0 and row["worker_p50_ms"] == 2.0


def test_summarise_with_no_server_timing_leaves_the_split_unknown():
    row = br.summarise([(10.0, {}), (20.0, {})])
    assert row["queue_p50_ms"] is None and row["worker_p50_ms"] is None


def test_render_flags_a_p95_over_the_hard_budget_and_names_skips():
    report = {
        "base": "https://127.0.0.1:1", "taken": "2026-03-04T12:00:00", "max_inflight": 4,
        "budgets": {"board": {"soft_s": 0.5, "hard_s": 1.5}},
        "results": {
            "board": {"samples": 5, "p50_ms": 900.0, "p95_ms": 2000.0, "max_ms": 2100.0,
                      "queue_p50_ms": 800.0, "worker_p50_ms": 10.0},
            "send": {"skipped": "writes into a session"},
        },
    }
    text = "\n".join(br.render(report))
    assert "board" in text and "OVER" in text
    assert "send" in text and "skipped: writes into a session" in text
    assert "max in flight: 4" in text
