"""Screen time's By feature counts: the query, the route and the privacy shape.

The page records one ``tap`` per successful use under a ``feat.*`` control id
(``static/settings/feature-track.js``); this pins that the server counts them
per device class and range, and that nothing but the id reaches the log.

Every value is invented.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW
from src.usage.feature_counts import FEATURE_IDS, feature_counts
from src.usage_log import SESSION_ID_PATTERN, UsageLog, format_ts

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SESSION = "3f2b8c1e-0d4a-4b6f-9c2e-1a2b3c4d5e6f"
_NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def _tap(control_id: str, device: str, when: datetime = _NOW, kind: str = "tap"):
    return (format_ts(when), _SESSION, kind, "claude", control_id, device, "other")


def _counts(tmp_path, items, since=_NOW - timedelta(days=1), until=_NOW + timedelta(minutes=1)):
    log = UsageLog(tmp_path / "usage.sqlite3")
    log.insert_rows(items, now=_NOW)
    conn = sqlite3.connect(log.db_path)
    try:
        return {r["id"]: r for r in feature_counts(conn, since, until)["features"]}
    finally:
        conn.close()


class TestFeatureCounts:
    def test_one_use_per_device_raises_each_count_by_one_per_device(self, tmp_path):
        items = [_tap(fid, device) for fid in FEATURE_IDS for device in ("phone", "desktop")]
        got = _counts(tmp_path, items)
        assert list(got) == list(FEATURE_IDS), "every feature is listed, in the page's order"
        for fid in FEATURE_IDS:
            assert got[fid]["by_device"] == {"phone": 1, "desktop": 1}
            assert got[fid]["total"] == 2

    def test_a_second_use_counts_again_and_an_unused_feature_reads_zero(self, tmp_path):
        items = [_tap("feat.dictation", "phone")] * 3
        got = _counts(tmp_path, items)
        assert got["feat.dictation"] == {
            "id": "feat.dictation", "total": 3, "by_device": {"phone": 3},
        }
        assert got["feat.ocr"] == {"id": "feat.ocr", "total": 0, "by_device": {}}

    def test_only_feature_taps_inside_the_window_count(self, tmp_path):
        items = [
            _tap("feat.ocr", "phone", _NOW - timedelta(days=2)),     # before the window
            _tap("feat.ocr", "phone", _NOW + timedelta(hours=2)),    # after it
            _tap("feat.ocr", "phone", kind="view"),                  # not a tap
            _tap("mic.start", "phone"),                              # not a feature
            _tap("feat.unknown", "phone"),                           # not on the list
            _tap("feat.ocr", "tablet"),                              # the one use
        ]
        got = _counts(tmp_path, items)
        assert {fid: r["total"] for fid, r in got.items() if r["total"]} == {"feat.ocr": 1}
        assert got["feat.ocr"]["by_device"] == {"tablet": 1}

    def test_every_id_fits_the_log_s_bounded_pattern(self):
        from src.usage_log import ID_PATTERN

        assert all(ID_PATTERN.fullmatch(fid) for fid in FEATURE_IDS)
        assert len(set(FEATURE_IDS)) == len(FEATURE_IDS)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_the_page_recorder_sends_only_listed_ids():
    result = subprocess.run(
        ["node", str(_REPO_ROOT / "tests" / "js" / "feature_track.test.mjs")],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, (
        f"feature_track.test.mjs failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout


@pytest.fixture
def usage_env(tmp_path, monkeypatch):
    """Point the runtime-data dir at a temp dir before the app is built."""
    data_dir = tmp_path / "runtime-data"
    monkeypatch.setenv("APP_LAUNCHER_DATA_DIR", str(data_dir))
    return data_dir / "usage.sqlite3"


def _post(client, device: str, events):
    body = {"session_id": _SESSION, "device_class": device, "os_class": "other", "events": events}
    assert SESSION_ID_PATTERN.fullmatch(_SESSION)
    return client.post("/api/usage/events", json=body)


class TestRoute:
    def test_uses_posted_by_the_page_come_back_per_device(self, usage_env, webapp_client):
        client, _, _ = webapp_client
        for device in ("phone", "desktop"):
            events = [{"kind": "tap", "id": fid, "view": "claude"} for fid in FEATURE_IDS]
            assert _post(client, device, events).json()["accepted"] == len(FEATURE_IDS)
        for days in (1, 7, 30):
            body = client.get(f"/api/usage/features?days={days}").json()
            assert body["days"] == days and body["log_exists"] is True
            assert [r["id"] for r in body["features"]] == list(FEATURE_IDS)
            assert all(r["by_device"] == {"phone": 1, "desktop": 1} for r in body["features"])

    def test_no_log_yet_is_zero_use_and_says_so(self, usage_env, webapp_client):
        client, _, _ = webapp_client
        body = client.get("/api/usage/features?days=7").json()
        assert body["log_exists"] is False
        assert [r["total"] for r in body["features"]] == [0] * len(FEATURE_IDS)
        assert not usage_env.exists(), "reading the counts must not create the log"

    def test_a_range_outside_the_bounds_is_refused(self, usage_env, webapp_client):
        client, _, _ = webapp_client
        assert client.get("/api/usage/features?days=0").status_code == 422
        assert client.get("/api/usage/features?days=9999").status_code == 422

    def test_a_use_logs_its_id_and_nothing_a_client_adds(self, usage_env, webapp_client):
        client, _, _ = webapp_client
        secret = "SECRET-FILE-NAME.png"
        sent = _post(client, "phone", [{
            "kind": "tap", "id": "feat.attach.image", "view": "claude",
            "name": secret, "text": secret, "transcript": secret, "bytes": 123,
        }])
        assert sent.json() == {"accepted": 1, "rejected": 0}
        conn = sqlite3.connect(usage_env)
        try:
            rows = conn.execute(
                "SELECT kind, view_id, control_id, device_class, os_class FROM events"
            ).fetchall()
            everything = "\n".join(conn.iterdump())
        finally:
            conn.close()
        assert rows == [("tap", "claude", "feat.attach.image", "phone", "other")]
        assert secret not in everything
