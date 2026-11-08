"""The perf-ledger reader behind the Apps tab: what it keeps, and what it refuses to call a pass."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import perf_ledger


def _rec(app: str, run_id: str, overall: str = "pass", cold=None, warm=None, kb=None, **extra) -> dict:
    checks = {}
    for cid, value, budget in (
        ("cold.ready_ms", cold, 2500),
        ("warm.ready_ms", warm, 1500),
        ("warm.bytes_kb", kb, 800),
    ):
        if value is not None:
            status = "pass" if value <= budget else "fail"
            checks[cid] = {"status": status, "budget": budget, "measured": value}
    ts = f"{run_id[:4]}-{run_id[4:6]}-{run_id[6:8]}T{run_id[9:11]}:{run_id[11:13]}:{run_id[13:15]}Z"
    return {
        "schema_version": 1, "run_id": run_id, "ts": ts, "app": app, "build": "abc1234",
        "overall": overall, "counts": {"pass": len(checks), "fail": 0, "unmeasured": 0},
        "checks": checks, "endpoints": {}, **extra,
    }


def _write(path: Path, *lines) -> Path:
    path.write_text("\n".join(l if isinstance(l, str) else json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    return path


class TestReadLedger:
    def test_missing_file_is_missing_not_empty_ok(self, tmp_path):
        status, records, _ = perf_ledger.read_ledger(tmp_path / "nope.jsonl")
        assert (status, records) == ("missing", [])

    def test_unreadable_file_is_unreadable(self, tmp_path):
        bad = tmp_path / "ledger.jsonl"
        bad.write_bytes(b"\xff\xfe\x00 not utf-8 \xff")
        status, records, _ = perf_ledger.read_ledger(bad)
        assert (status, records) == ("unreadable", [])

    def test_a_directory_in_place_of_the_file_is_unreadable(self, tmp_path):
        status, _, _ = perf_ledger.read_ledger(tmp_path)
        assert status == "unreadable"

    def test_bad_lines_are_skipped_and_counted_not_fatal(self, tmp_path):
        path = _write(
            tmp_path / "l.jsonl",
            _rec("a", "20261107T020000Z"), "{not json", "[1, 2]",
            {"app": "", "run_id": "x"}, {"app": "a"}, "",
        )
        status, records, skipped = perf_ledger.read_ledger(path)
        assert status == "ok" and len(records) == 1 and skipped == 4

    def test_last_line_per_app_and_run_wins(self, tmp_path):
        path = _write(
            tmp_path / "l.jsonl",
            _rec("a", "20261107T020000Z", cold=900),
            _rec("a", "20261107T020000Z", cold=1000),
        )
        _, records, _ = perf_ledger.read_ledger(path)
        assert [r["checks"]["cold.ready_ms"]["measured"] for r in records] == [1000]


class TestSummarise:
    def test_latest_run_and_trend_oldest_first(self):
        # Written out of order: the latest run is the greatest run id, not the last line.
        recs = [
            _rec("a", "20261107T030000Z", cold=1800, warm=600, kb=410),
            _rec("a", "20261105T030000Z", cold=2100),
            _rec("a", "20261106T030000Z", cold=1950),
        ]
        entry = perf_ledger.summarise(recs)["a"]
        assert entry["run_id"] == "20261107T030000Z"
        assert entry["trend"] == [2100, 1950, 1800]
        assert entry["cold_ready_ms"] == {"measured": 1800, "budget": 2500, "status": "pass"}
        assert entry["warm_bytes_kb"]["measured"] == 410
        assert entry["issues_url"] is None

    def test_trend_keeps_the_last_twelve_measured_runs(self):
        recs = [_rec("a", f"202611{d:02d}T010000Z", cold=1000 + d) for d in range(1, 21)]
        trend = perf_ledger.summarise(recs)["a"]["trend"]
        assert len(trend) == perf_ledger.TREND_RUNS and trend[-1] == 1020

    def test_unmeasured_check_is_not_a_value_and_not_in_the_trend(self):
        rec = _rec("a", "20261107T030000Z", overall="unmeasured", cold=1000)
        rec["checks"]["cold.ready_ms"] = {"status": "unmeasured", "budget": 2500, "measured": None}
        entry = perf_ledger.summarise([rec])["a"]
        assert entry["state"] == "unmeasured"
        assert entry["cold_ready_ms"] is None and entry["trend"] == []

    def test_over_budget_names_the_failing_checks_and_links_the_issues(self):
        rec = _rec("photo-ocr", "20261107T030000Z", overall="over-budget", cold=3100)
        entry = perf_ledger.summarise([rec])["photo-ocr"]
        assert entry["state"] == "over-budget" and entry["fails"] == ["cold.ready_ms"]
        assert entry["issues_url"].endswith("/photo-ocr/issues?q=is%3Aissue+is%3Aopen+label%3Aperf-review")

    @pytest.mark.parametrize("overall", ["PASS", "green", None, 3, ""])
    def test_an_unrecognised_verdict_is_unknown_never_pass(self, overall):
        rec = _rec("a", "20261107T030000Z")
        rec["overall"] = overall
        assert perf_ledger.summarise([rec])["a"]["state"] == "unknown"

    def test_a_record_seeded_from_an_issue_log_has_no_numbers(self):
        rec = _rec("a", "20261107T020000Z", seeded_from="issue-run-log")
        rec["checks"] = {}
        entry = perf_ledger.summarise([rec])["a"]
        assert entry["seeded"] is True and entry["cold_ready_ms"] is None and entry["trend"] == []

    def test_non_numeric_measured_is_not_a_number(self):
        rec = _rec("a", "20261107T030000Z", cold=1000)
        rec["checks"]["cold.ready_ms"]["measured"] = "1000"
        rec["checks"]["warm.ready_ms"] = {"status": "pass", "budget": 1, "measured": True}
        entry = perf_ledger.summarise([rec])["a"]
        assert entry["cold_ready_ms"] is None and entry["warm_ready_ms"] is None


class TestRowMapping:
    def test_the_nearest_ancestor_folder_the_ledger_names_wins(self):
        apps = ["app-launcher", "photo-ocr"]
        assert perf_ledger.ledger_app_for("E:\\automation\\photo-ocr\\launch_app.bat", apps) == "photo-ocr"
        assert perf_ledger.ledger_app_for("E:/automation/Photo-OCR/scripts/run.bat", apps) == "photo-ocr"

    def test_the_bat_file_name_is_never_a_folder(self):
        assert perf_ledger.ledger_app_for("E:\\x\\y\\photo-ocr", ["photo-ocr"]) is None

    def test_no_match_is_none(self):
        assert perf_ledger.ledger_app_for("E:\\automation\\other\\run.bat", ["photo-ocr"]) is None


class TestBuildReport:
    def test_ok_ledger(self, tmp_path):
        path = _write(tmp_path / "l.jsonl", _rec("a", "20261107T030000Z", cold=1000))
        report = perf_ledger.build_report(path)
        assert report["ledger"] == "ok" and set(report["apps"]) == {"a"}

    def test_missing_and_unreadable_carry_no_apps(self, tmp_path):
        assert perf_ledger.build_report(tmp_path / "x.jsonl") == {"ledger": "missing", "apps": {}}
        assert perf_ledger.build_report(tmp_path)["ledger"] == "unreadable"

    def test_the_path_follows_the_hooks_state_dir_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_HOOKS_STATE_DIR", str(tmp_path))
        assert perf_ledger.ledger_path() == tmp_path / "perf-review" / "ledger.jsonl"
