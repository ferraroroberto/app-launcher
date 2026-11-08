"""GET /api/apps/perf — the Apps tab's phone-speed results from a fixture ledger."""

from __future__ import annotations

import json
import os
from pathlib import Path

from src.registry import AppEntry, Registry, save_registry


def _seed(tmp_registry_path, apps):
    save_registry(Registry(scan_root="C:\\stub", apps=apps), path=tmp_registry_path)


def _app(app_id: str, kind: str, bat: str) -> AppEntry:
    return AppEntry(id=app_id, name=app_id, kind=kind, bat_path=bat, added_at="2026-01-01T00:00:00")


def _ledger(*records) -> Path:
    path = Path(os.environ["CLAUDE_HOOKS_STATE_DIR"]) / "perf-review" / "ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _record(app: str, run_id: str, overall: str, cold: float, budget: float = 2500) -> dict:
    status = "pass" if cold <= budget else "fail"
    return {
        "schema_version": 1, "run_id": run_id, "ts": "2026-11-07T03:00:00Z", "app": app,
        "build": "abc1234", "overall": overall, "counts": {"pass": 1, "fail": 0, "unmeasured": 0},
        "checks": {"cold.ready_ms": {"status": status, "budget": budget, "measured": cold}},
        "endpoints": {},
    }


class TestAppsPerf:
    def test_no_ledger_file_is_missing_with_no_rows_green(self, webapp_client):
        client, _, overrides = webapp_client
        _seed(overrides["tmp_registry_path"], [_app("a-tray", "tray", "C:\\x\\photo-ocr\\tray.bat")])
        body = client.get("/api/apps/perf").json()
        assert body["ledger"] == "missing" and body["apps"] == {} and body["rows"] == {}

    def test_rows_map_to_the_ledger_app_of_their_folder(self, webapp_client):
        client, _, overrides = webapp_client
        _seed(overrides["tmp_registry_path"], [
            _app("ocr-tray", "tray", "C:\\x\\photo-ocr\\tray.bat"),
            _app("ocr-nested", "streamlit", "C:\\x\\photo-ocr\\scripts\\run.bat"),
            _app("ocr-tunnel", "tunnel", "C:\\x\\photo-ocr\\webapp_tunnel.bat"),
            _app("other", "webapp", "C:\\x\\unmeasured\\webapp.bat"),
        ])
        _ledger(
            _record("photo-ocr", "20261106T030000Z", "pass", 2000),
            _record("photo-ocr", "20261107T030000Z", "over-budget", 3100),
        )
        body = client.get("/api/apps/perf").json()
        assert body["ledger"] == "ok"
        # The tunnel row and the row with no ledger app carry no entry.
        assert body["rows"] == {"ocr-tray": "photo-ocr", "ocr-nested": "photo-ocr"}
        entry = body["apps"]["photo-ocr"]
        assert entry["state"] == "over-budget" and entry["trend"] == [2000, 3100]
        assert entry["cold_ready_ms"]["measured"] == 3100
        assert "label%3Aperf-review" in entry["issues_url"]

    def test_an_unreadable_ledger_is_reported_not_hidden(self, webapp_client):
        client, _, _ = webapp_client
        path = _ledger(_record("a", "20261107T030000Z", "pass", 1000))
        path.write_bytes(b"\xff\xfe\x00 \xff")
        assert client.get("/api/apps/perf").json()["ledger"] == "unreadable"

    def test_a_bad_line_does_not_hide_the_rest(self, webapp_client):
        client, _, _ = webapp_client
        path = _ledger(_record("a", "20261107T030000Z", "pass", 1000))
        path.write_text(path.read_text(encoding="utf-8") + "{broken\n", encoding="utf-8")
        body = client.get("/api/apps/perf").json()
        assert body["ledger"] == "ok" and body["apps"]["a"]["state"] == "pass"
