"""GET /api/context-filter, PUT /api/context-filter/mode (issue #713).

Style mirrors test_webapp_api_config.py / test_webapp_api_basics.py: build
the TestClient via the shared webapp_client fixture (context_filter_mode_file
/ context_filter_log_file point at tmp_path — see conftest.py), assert
response shape, and confirm the absent/corrupt-file degradation never
surfaces as a 500.
"""

from __future__ import annotations

import json
from pathlib import Path


class TestGetContextFilter:
    def test_returns_expected_shape_with_no_files_yet(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/api/context-filter")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"mode", "harnesses", "stats"}
        # No mode.json yet -> absent-file semantics: available True, mode off.
        assert body["mode"] == {
            "available": True,
            "mode": "off",
            "updated_at": None,
            "updated_by": None,
        }
        assert isinstance(body["harnesses"], list) and body["harnesses"]
        for h in body["harnesses"]:
            assert set(h.keys()) == {"id", "label", "status", "note"}
            assert h["status"] in ("active", "unsupported", "planned")
        # No shadow.jsonl yet -> unavailable stats, never a 500.
        assert body["stats"]["available"] is False

    def test_harness_matrix_carries_known_ids(self, webapp_client):
        client, _, _ = webapp_client
        body = client.get("/api/context-filter").json()
        ids = {h["id"] for h in body["harnesses"]}
        assert ids == {"claude", "codex", "grok", "pi", "copilot", "antigravity"}

    def test_corrupt_mode_file_degrades_not_500(self, webapp_client, tmp_path: Path):
        client, app, _ = webapp_client
        cfg = app.state.webapp_config
        Path(cfg.context_filter_mode_file).parent.mkdir(parents=True, exist_ok=True)
        Path(cfg.context_filter_mode_file).write_text("not json {{{", encoding="utf-8")
        resp = client.get("/api/context-filter")
        assert resp.status_code == 200
        assert resp.json()["mode"]["available"] is False

    def test_corrupt_shadow_log_degrades_not_500(self, webapp_client):
        client, app, _ = webapp_client
        cfg = app.state.webapp_config
        log_path = Path(cfg.context_filter_log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Bad lines are tolerated individually — this exercises the file
        # itself being present but every line malformed.
        log_path.write_text("{{{not json\nnot json either\n", encoding="utf-8")
        resp = client.get("/api/context-filter")
        assert resp.status_code == 200
        stats = resp.json()["stats"]
        assert stats["available"] is True
        assert stats["totals"]["rows"] == 0

    def test_stats_reflect_written_rows(self, webapp_client):
        client, app, _ = webapp_client
        cfg = app.state.webapp_config
        log_path = Path(cfg.context_filter_log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": "2026-08-01T00:00:00Z",
            "agent": "claude",
            "command": "git commit -m x",
            "raw_tokens": 1000,
            "compressed_tokens": 400,
        }
        log_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        resp = client.get("/api/context-filter")
        stats = resp.json()["stats"]
        assert stats["available"] is True
        assert stats["totals"]["tokens_saved"] == 600
        assert stats["per_agent"]["claude"]["rows"] == 1


class TestPutContextFilterMode:
    def test_valid_mode_writes_and_returns_fresh_mode(self, webapp_client):
        client, app, _ = webapp_client
        resp = client.put("/api/context-filter/mode", json={"mode": "shadow"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"]["available"] is True
        assert body["mode"]["mode"] == "shadow"
        assert body["mode"]["updated_by"] == "app-launcher"

        cfg = app.state.webapp_config
        on_disk = json.loads(Path(cfg.context_filter_mode_file).read_text(encoding="utf-8"))
        assert on_disk["mode"] == "shadow"

    def test_get_reflects_the_write(self, webapp_client):
        client, _, _ = webapp_client
        client.put("/api/context-filter/mode", json={"mode": "rewrite"})
        body = client.get("/api/context-filter").json()
        assert body["mode"]["mode"] == "rewrite"

    def test_invalid_mode_is_4xx_not_500(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.put("/api/context-filter/mode", json={"mode": "bogus"})
        assert 400 <= resp.status_code < 500

    def test_missing_mode_field_is_4xx_not_500(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.put("/api/context-filter/mode", json={})
        assert 400 <= resp.status_code < 500

    def test_no_orphaned_tmp_file_after_write(self, webapp_client):
        client, app, _ = webapp_client
        client.put("/api/context-filter/mode", json={"mode": "shadow"})
        cfg = app.state.webapp_config
        mode_dir = Path(cfg.context_filter_mode_file).parent
        assert not list(mode_dir.glob("*.tmp"))
