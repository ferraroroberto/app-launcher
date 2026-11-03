"""Time spent, end to end: the real client's sends replayed into the real server half.

The foreground-time rules have their own server tests (`test_usage_log.py`), but
those start from rows. A change to `usage-log.js` that drops the `session_start`
on a return to the foreground, or leaves a closed sheet billing its minutes, keeps
every one of them green. Here `_usage_log_time_probe.mjs` drives the vendored
client through a scripted day on a virtual clock (tabs, sheets nested and removed,
taps, a hide and show, a desktop idle gap), each batch is recorded by `UsageLog`
at the minute it was sent, and the minutes per day, device and view must come out
exact. Node is part of the gate's toolchain, so its absence fails here rather than
skipping.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.subprocess_flags import NO_WINDOW
from src.usage_log import UsageLog, foreground_time, parse_batch

REPO_ROOT = Path(__file__).resolve().parents[3]
CLIENT = REPO_ROOT / "app" / "webapp" / "static" / "_vendored" / "usage-log" / "usage-log.js"
PROBE = Path(__file__).resolve().parent / "_usage_log_time_probe.mjs"
#: The probe's minute 0.
START = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
DAY = datetime(2026, 10, 12, tzinfo=UTC)


def replay(client: Path, db: Path) -> list[dict[str, Any]]:
    """Run the probe against `client`, record each send at its minute; returns the sends."""
    node = shutil.which("node")
    assert node, "node is required to run the usage-log time-spent test"
    done = subprocess.run(
        [node, str(PROBE), str(client)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        creationflags=NO_WINDOW, check=False,
    )
    assert done.returncode == 0, done.stderr
    sends: list[dict[str, Any]] = json.loads(done.stdout)
    log = UsageLog(db)
    for send in sends:
        batch = parse_batch(json.dumps(send["body"]).encode())
        assert batch.rejected == 0, f"the server rejected part of a send: {send}"
        log.record(batch, now=START + timedelta(minutes=send["minute"]))
    return sends


@pytest.fixture(scope="module")
def recorded(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db = tmp_path_factory.mktemp("time") / "usage.sqlite3"
    replay(CLIENT, db)
    return db


def time_spent(db: Path) -> dict[str, Any]:
    conn = sqlite3.connect(db)
    try:
        return foreground_time(conn, DAY, DAY + timedelta(days=1), UTC)
    finally:
        conn.close()


def test_minutes_per_device_and_in_total(recorded: Path) -> None:
    out = time_spent(recorded)
    # Desktop: 10 min, then 1 + the 20-minute idle gap cut to 15 + 2. Phone: 60 to 90.
    assert out["by_device"] == {"phone": 30.0, "desktop": 28.0}
    assert out["total_minutes"] == 58.0


def test_minutes_per_view_follow_tabs_and_sheets(recorded: Path) -> None:
    assert time_spent(recorded)["by_view"] == {
        "home": 24.0,           # 60-62, 66-70, and 72-90 once the removed sheet hands back
        "settings": 21.0,       # 4-5, 8-10 after the sheet closed, and the whole second interval
        "board": 4.0,
        "confirm-sheet": 3.0,
        "links-sheet": 3.0,     # 62-63, then 64-66 once the nested sheet above it closed
        "share-sheet": 2.0,
        "link-edit": 1.0,
    }


def test_one_day_series_carries_the_same_minutes(recorded: Path) -> None:
    out = time_spent(recorded)
    assert [d["day"] for d in out["days_series"]] == ["2026-10-12"]
    day = out["days_series"][0]
    assert day["minutes"] == 58.0
    assert day["by_device"] == out["by_device"] and day["by_view"] == out["by_view"]


def test_taps_carry_the_view_they_happened_on(recorded: Path) -> None:
    conn = sqlite3.connect(recorded)
    try:
        taps = conn.execute("SELECT control_id, view_id FROM events WHERE kind = 'tap' ORDER BY id").fetchall()
    finally:
        conn.close()
    assert taps == [
        ("refresh-all", "board"), ("confirm-yes", "confirm-sheet"),
        ("refresh-all", "settings"), ("refresh-all", "settings"),
        ("link-save", "links-sheet"),
    ]
