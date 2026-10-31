"""Screen time export to task-os (#1678, step 2/2): src/usage/export.py.

The exporter runs against a fake task-os (a real HTTP server on a loopback
port, so the real ``requests`` path and error mapping run) and a synthetic
usage log in ``tmp_path``. The real usage log and the live task-os are never
touched. Pinned:

  * the last 7 completed days plus today by default;
  * one entry per day, devices merged, the device split in the note;
  * a day that has not changed since the last send makes no request, and a
    changed day is PUT again (idempotent by ``usage:app-launcher:<day>``);
  * a day that rounds to 0 is a DELETE, and a 404 there is fine;
  * the toast text says what changed, or that nothing did;
  * a dry run makes no HTTP call and writes no status file;
  * not set up, category missing, task-os down and task-os refusing are
    distinct errors, each recorded for the sheet's "last sent" line.
"""

from __future__ import annotations

import argparse
import http.server
import json
import socket
import threading
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.cli.commands.usage_export_cmd import UsageExportCommand
from app.webapp.routers import screen_time
from src.usage import export as usage_export
from src.usage_log import UsageLog, format_ts
from src.webapp_config import WebappConfig

TODAY = date(2026, 3, 12)
D1, D2, D3 = TODAY - timedelta(days=3), TODAY - timedelta(days=2), TODAY - timedelta(days=1)
CATEGORY_ID = 42


# ------------------------------------------------------------ fixtures


def _at(day: date, hh: int, mm: int) -> str:
    """A host-local wall-clock moment as the log's stored UTC string."""
    return format_ts(datetime.combine(day, time(hh, mm)).astimezone(timezone.utc))


def _visit(sid: str, device: str, day: date, start: tuple, end: tuple, mid: Optional[tuple] = None) -> list:
    rows = [(_at(day, *start), sid, "session_start", "claude", None, device, "windows")]
    if mid:
        rows.append((_at(day, *mid), sid, "view", "board", None, device, "windows"))
    rows.append((_at(day, *end), sid, "session_end", None, None, device, "windows"))
    return rows


@pytest.fixture
def usage_db(tmp_path: Path) -> Path:
    """D1: PC 20 m + phone 30 m; D2: nothing; D3: phone 45 m; today: phone 10 m."""
    db = tmp_path / "usage.sqlite3"
    rows = (
        _visit("v-pc-1", "desktop", D1, (9, 0), (9, 20), mid=(9, 10))
        + _visit("v-ph-1", "phone", D1, (12, 0), (12, 30))
        + _visit("v-ph-3", "phone", D3, (20, 0), (20, 45))
        + _visit("v-ph-t", "phone", TODAY, (8, 0), (8, 10))
    )
    UsageLog(db).insert_rows(rows, now=datetime.combine(TODAY, time(23, 0)).astimezone(timezone.utc))
    return db


class FakeTaskOs:
    """In-memory task-os time API: meta, PUT/DELETE by source_id."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.store: Dict[str, dict] = {}
        self.areas = [{"id": 2, "slug": "work", "name": "Work", "archived": False, "categories": [
            {"id": CATEGORY_ID, "slug": "launcher", "name": "Launcher", "archived": False},
        ]}]
        self.people = [{"id": "p1", "name": "Person 1"}]
        self.reject_put: Optional[str] = None

    def handle(self, method: str, path: str, body: Optional[dict]) -> tuple[int, Optional[dict]]:
        self.calls.append((method, path, body))
        if method == "GET" and path == "/api/time/meta":
            return 200, {"people": self.people, "areas": self.areas}
        prefix = "/api/time/entries/by-source/"
        if path.startswith(prefix):
            key = path[len(prefix):]
            if method == "PUT":
                if self.reject_put:
                    return 422, {"error": {"code": "validation_error", "message": self.reject_put}}
                created = key not in self.store
                self.store[key] = body or {}
                return (201 if created else 200), {"source_id": key, **(body or {})}
            if method == "DELETE":
                if key not in self.store:
                    return 404, {"error": {"code": "not_found", "message": "no entry"}}
                del self.store[key]
                return 204, None
        return 404, {"error": {"code": "not_found", "message": "Not Found"}}

    @property
    def writes(self) -> List[tuple]:
        return [c for c in self.calls if c[0] in ("PUT", "DELETE")]


@pytest.fixture
def task_os():
    fake = FakeTaskOs()

    class Handler(http.server.BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            status, payload = fake.handle(self.command, self.path, body)
            self.send_response(status)
            data = json.dumps(payload).encode() if payload is not None else b""
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_PUT = do_DELETE = _serve

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fake.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()


def _cfg(url: str, **overrides: str) -> WebappConfig:
    values = {"task_os_url": url, "task_os_person": "p1",
              "task_os_area": "work", "task_os_category": "launcher", **overrides}
    return WebappConfig(**values)


def _run(cfg: WebappConfig, usage_db: Path, tmp_path: Path, **kwargs: Any) -> usage_export.ExportResult:
    return usage_export.run_export(
        cfg, since=kwargs.pop("since", D1), today=TODAY, db_path=usage_db,
        status_path=tmp_path / "status.json", **kwargs,
    )


def _closed_port_url() -> str:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


# ------------------------------------------------------------ the numbers


def test_default_window_is_the_last_seven_completed_days_and_today() -> None:
    days = usage_export.send_days(TODAY)
    assert days[0] == TODAY - timedelta(days=7)
    assert days[-1] == TODAY
    assert len(days) == 8
    assert usage_export.send_days(TODAY, D3) == [D3, TODAY]
    assert usage_export.send_days(TODAY, TODAY) == [TODAY]
    with pytest.raises(ValueError):
        usage_export.send_days(TODAY, TODAY + timedelta(days=1))


def test_dry_run_counts_each_day_and_sends_nothing(task_os: FakeTaskOs, usage_db: Path, tmp_path: Path) -> None:
    result = _run(_cfg(task_os.url), usage_db, tmp_path, dry_run=True)

    assert [(e.day, e.minutes, e.action) for e in result.entries] == [
        (D1, 50, "put"), (D2, 0, "delete"), (D3, 45, "put"), (TODAY, 10, "put"),
    ]
    assert result.entries[0].by_device == {"phone": 30, "desktop": 20}
    assert result.entries[0].note == "Launcher screen time: PC 20 m, Phone 30 m"
    assert result.entries[0].source_id == f"usage:app-launcher:{D1.isoformat()}"
    assert task_os.calls == []
    assert not (tmp_path / "status.json").exists()


def test_dry_run_needs_no_task_os_config(usage_db: Path, tmp_path: Path) -> None:
    result = _run(WebappConfig(task_os_person=""), usage_db, tmp_path, dry_run=True)
    assert result.total_minutes == 105


def _put(day: date, minutes: int, note: str) -> tuple:
    return ("PUT", f"/api/time/entries/by-source/usage:app-launcher:{day}", {
        "person": "p1", "category_id": CATEGORY_ID, "day": day.isoformat(),
        "minutes": minutes, "note": note,
    })


def _status_file(tmp_path: Path) -> Dict[str, Any]:
    return json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))


def test_first_send_puts_each_day_today_included(
    task_os: FakeTaskOs, usage_db: Path, tmp_path: Path,
) -> None:
    first = _run(_cfg(task_os.url), usage_db, tmp_path)

    assert task_os.writes == [
        _put(D1, 50, "Launcher screen time: PC 20 m, Phone 30 m"),
        ("DELETE", f"/api/time/entries/by-source/usage:app-launcher:{D2}", None),
        _put(D3, 45, "Launcher screen time: Phone 45 m"),
        _put(TODAY, 10, "Launcher screen time: Phone 10 m"),
    ]
    # D2 was never in task-os (the DELETE got a 404), so nothing was removed.
    assert [(o.day, o.outcome) for o in first.outcomes] == [
        (D1, "added"), (D2, "unchanged"), (D3, "added"), (TODAY, "added"),
    ]
    assert (first.put, first.deleted, first.unchanged) == (3, 0, 1)
    assert sorted(task_os.store) == [
        f"usage:app-launcher:{D1}", f"usage:app-launcher:{D3}", f"usage:app-launcher:{TODAY}",
    ]
    status = usage_export.read_status(tmp_path / "status.json")
    assert status["last_ok"]["put"] == 3 and status["last_ok"]["unchanged"] == 1
    assert status["last_ok"]["last"] == TODAY.isoformat()
    assert status["last_error"] is None


def test_an_immediate_second_send_makes_no_write(
    task_os: FakeTaskOs, usage_db: Path, tmp_path: Path,
) -> None:
    _run(_cfg(task_os.url), usage_db, tmp_path)
    task_os.calls.clear()

    second = _run(_cfg(task_os.url), usage_db, tmp_path)

    assert task_os.writes == []
    assert [o.outcome for o in second.outcomes] == ["unchanged"] * 4
    assert (second.put, second.deleted) == (0, 0)
    assert second.summary() == "Nothing new to send. Today and the last 3 days are up to date in task-os."


def test_only_a_changed_day_is_sent_again(
    task_os: FakeTaskOs, usage_db: Path, tmp_path: Path,
) -> None:
    _run(_cfg(task_os.url), usage_db, tmp_path)
    task_os.calls.clear()
    # Ten more phone minutes today: the running total grows.
    UsageLog(usage_db).insert_rows(
        _visit("v-ph-t2", "phone", TODAY, (9, 0), (9, 10)),
        now=datetime.combine(TODAY, time(23, 0)).astimezone(timezone.utc),
    )

    third = _run(_cfg(task_os.url), usage_db, tmp_path)

    assert task_os.writes == [_put(TODAY, 20, "Launcher screen time: Phone 20 m")]
    assert [(o.day, o.outcome) for o in third.outcomes if o.outcome != "unchanged"] == [(TODAY, "updated")]
    assert third.summary() == "Sent: today 20 m (updated), yesterday unchanged, 2 earlier days unchanged."


def test_a_changed_device_split_with_the_same_total_is_sent_again(
    task_os: FakeTaskOs, usage_db: Path, tmp_path: Path,
) -> None:
    _run(_cfg(task_os.url), usage_db, tmp_path)
    status = _status_file(tmp_path)
    status["sent"]["days"][D1.isoformat()]["by_device"] = {"desktop": 50}
    (tmp_path / "status.json").write_text(json.dumps(status), encoding="utf-8")
    task_os.calls.clear()

    _run(_cfg(task_os.url), usage_db, tmp_path)

    assert task_os.writes == [_put(D1, 50, "Launcher screen time: PC 20 m, Phone 30 m")]


def test_a_new_target_resends_everything(task_os: FakeTaskOs, usage_db: Path, tmp_path: Path) -> None:
    _run(_cfg(task_os.url), usage_db, tmp_path)
    task_os.calls.clear()
    task_os.people.append({"id": "p2", "name": "Person 2"})

    _run(_cfg(task_os.url, task_os_person="p2"), usage_db, tmp_path)

    assert [c[0] for c in task_os.writes] == ["PUT", "DELETE", "PUT", "PUT"]


def test_the_status_file_holds_per_day_integers_only(
    task_os: FakeTaskOs, usage_db: Path, tmp_path: Path,
) -> None:
    _run(_cfg(task_os.url), usage_db, tmp_path)
    ledger = _status_file(tmp_path)["sent"]
    assert set(ledger) == {"target", "days"}
    assert ledger["days"][D1.isoformat()] == {"minutes": 50, "by_device": {"desktop": 20, "phone": 30}}
    assert all(isinstance(day["minutes"], int) and all(isinstance(v, int) for v in day["by_device"].values())
               for day in ledger["days"].values())


def test_a_refusal_midway_keeps_the_days_already_sent(
    task_os: FakeTaskOs, usage_db: Path, tmp_path: Path,
) -> None:
    real_handle = task_os.handle

    def refuse_today(method: str, path: str, body: Optional[dict]) -> tuple[int, Optional[dict]]:
        if method == "PUT" and path.endswith(str(TODAY)):
            return 422, {"error": {"code": "validation_error", "message": "no"}}
        return real_handle(method, path, body)

    task_os.handle = refuse_today  # type: ignore[method-assign]
    with pytest.raises(usage_export.TaskOsRejected):
        _run(_cfg(task_os.url), usage_db, tmp_path)
    task_os.handle = real_handle  # type: ignore[method-assign]
    task_os.calls.clear()

    _run(_cfg(task_os.url), usage_db, tmp_path)

    assert task_os.writes == [_put(TODAY, 10, "Launcher screen time: Phone 10 m")]


def test_a_day_that_drops_to_zero_deletes_what_was_sent(
    task_os: FakeTaskOs, usage_db: Path, tmp_path: Path,
) -> None:
    _run(_cfg(task_os.url), usage_db, tmp_path)
    status = _status_file(tmp_path)
    status["sent"]["days"][D2.isoformat()] = {"minutes": 30, "by_device": {"phone": 30}}
    (tmp_path / "status.json").write_text(json.dumps(status), encoding="utf-8")
    task_os.store[f"usage:app-launcher:{D2}"] = {"minutes": 30}
    task_os.calls.clear()

    again = _run(_cfg(task_os.url), usage_db, tmp_path)

    assert f"usage:app-launcher:{D2}" not in task_os.store
    assert [c[0] for c in task_os.writes] == ["DELETE"]
    assert [o.outcome for o in again.outcomes if o.day == D2] == ["removed"]
    assert again.deleted == 1

    # Once removed, the zero day is known and costs no further request.
    task_os.calls.clear()
    _run(_cfg(task_os.url), usage_db, tmp_path)
    assert task_os.writes == []


# ------------------------------------------------------------ the toast text


def _outcomes(*rows: tuple) -> list:
    """``(days ago, minutes, outcome)`` rows, oldest first."""
    return [usage_export.DayOutcome(TODAY - timedelta(days=ago), minutes, outcome)
            for ago, minutes, outcome in rows]


def test_summary_names_today_and_yesterday_and_folds_older_days() -> None:
    days = _outcomes(
        (7, 0, "unchanged"), (6, 30, "unchanged"), (5, 20, "unchanged"), (4, 0, "unchanged"),
        (3, 10, "unchanged"), (2, 15, "unchanged"), (1, 248, "unchanged"), (0, 312, "updated"),
    )
    assert usage_export._summary(days, TODAY) == (
        "Sent: today 5 h 12 m (updated), yesterday unchanged, 6 earlier days unchanged.")


def test_summary_names_an_older_change_and_a_removal() -> None:
    two, three = TODAY - timedelta(days=2), TODAY - timedelta(days=3)
    days = _outcomes((3, 45, "added"), (2, 0, "removed"), (1, 30, "updated"), (0, 5, "unchanged"))
    assert usage_export._summary(days, TODAY) == (
        "Sent: today unchanged, yesterday 30 m (updated), "
        f"{two:%a} {two.day} {two:%b} removed, {three:%a} {three.day} {three:%b} 45 m (added).")


def test_summary_counts_many_older_changes_instead_of_listing_them() -> None:
    days = _outcomes(*[(ago, 10, "added") for ago in range(7, 0, -1)], (0, 5, "added"))
    assert usage_export._summary(days, TODAY) == (
        "Sent: today 5 m (added), yesterday 10 m (added), 6 earlier days changed.")


def test_summary_when_only_today_is_in_the_window() -> None:
    assert usage_export._summary(_outcomes((0, 5, "unchanged")), TODAY) == (
        "Nothing new to send. Today is already in task-os.")


def test_task_os_down_is_unreachable(usage_db: Path, tmp_path: Path) -> None:
    with pytest.raises(usage_export.TaskOsUnreachable) as caught:
        _run(_cfg(_closed_port_url()), usage_db, tmp_path)
    assert caught.value.status == 503 and caught.value.exit_code == 4
    error = usage_export.read_status(tmp_path / "status.json")["last_error"]
    assert error["kind"] == "unreachable"
    assert "127.0.0.1" not in error["message"]


def test_task_os_refusing_the_data_is_rejected(task_os: FakeTaskOs, usage_db: Path, tmp_path: Path) -> None:
    task_os.reject_put = "an entry cannot be on a future day"
    with pytest.raises(usage_export.TaskOsRejected) as caught:
        _run(_cfg(task_os.url), usage_db, tmp_path)
    assert "an entry cannot be on a future day" in str(caught.value)
    assert caught.value.status == 502 and caught.value.exit_code == 5
    assert usage_export.read_status(tmp_path / "status.json")["last_error"]["kind"] == "rejected"


def test_a_missing_category_is_its_own_error_and_never_created(
    task_os: FakeTaskOs, usage_db: Path, tmp_path: Path,
) -> None:
    with pytest.raises(usage_export.CategoryNotFound) as caught:
        _run(_cfg(task_os.url, task_os_category="coding"), usage_db, tmp_path)
    assert caught.value.exit_code == 3
    assert "work/coding" in str(caught.value)
    assert [c[0] for c in task_os.calls] == ["GET"]

    task_os.areas[0]["categories"][0]["archived"] = True
    with pytest.raises(usage_export.CategoryNotFound):
        _run(_cfg(task_os.url), usage_db, tmp_path)


def test_not_set_up_fails_before_any_call(task_os: FakeTaskOs, usage_db: Path, tmp_path: Path) -> None:
    with pytest.raises(usage_export.ExportNotConfigured) as caught:
        _run(_cfg(task_os.url, task_os_person=""), usage_db, tmp_path)
    assert "task_os_person" in str(caught.value) and caught.value.exit_code == 2
    assert task_os.calls == []
    assert not usage_export.is_configured(WebappConfig())

    with pytest.raises(usage_export.ExportNotConfigured, match="not one of task-os's people"):
        _run(_cfg(task_os.url, task_os_person="nobody"), usage_db, tmp_path)
    assert task_os.writes == []


def test_no_usage_log_is_never_read_as_all_zero(task_os: FakeTaskOs, tmp_path: Path) -> None:
    with pytest.raises(usage_export.UsageLogMissing):
        usage_export.run_export(
            _cfg(task_os.url), today=TODAY, db_path=tmp_path / "absent.sqlite3",
            status_path=tmp_path / "status.json",
        )
    assert task_os.calls == []


def test_a_later_success_keeps_the_last_failure_for_the_sheet(
    task_os: FakeTaskOs, usage_db: Path, tmp_path: Path,
) -> None:
    with pytest.raises(usage_export.TaskOsUnreachable):
        _run(_cfg(_closed_port_url()), usage_db, tmp_path)
    _run(_cfg(task_os.url), usage_db, tmp_path)
    status = usage_export.read_status(tmp_path / "status.json")
    assert status["last_ok"] and status["last_error"]
    assert status["last_ok"]["at"] >= status["last_error"]["at"]


@pytest.mark.parametrize("minutes, whole", [(0.4, 0), (0.5, 1), (44.6, 45), (2000.0, 1440)])
def test_minutes_round_half_up_within_a_day(minutes: float, whole: int) -> None:
    assert usage_export._whole_minutes(minutes) == whole


# ------------------------------------------------------------ CLI + route


@pytest.mark.parametrize("error, code", [
    (usage_export.ExportNotConfigured("x"), 2),
    (usage_export.CategoryNotFound("x"), 3),
    (usage_export.TaskOsUnreachable("x"), 4),
    (usage_export.TaskOsRejected("x"), 5),
    (usage_export.UsageLogMissing("x"), 6),
])
def test_cli_exit_code_names_the_failure(monkeypatch: pytest.MonkeyPatch, error: Exception, code: int) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr("app.cli.commands.usage_export_cmd.run_export", boom)
    monkeypatch.setattr("app.cli.commands.usage_export_cmd.load_webapp_config", WebappConfig)
    args = argparse.Namespace(since=None, dry_run=False)
    assert UsageExportCommand(None).execute(args) == code


def _route_client(cfg: WebappConfig) -> TestClient:
    app = FastAPI()
    app.state.webapp_config = cfg
    app.include_router(screen_time.router)
    return TestClient(app)


def test_route_without_a_usage_log_says_so(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(usage_export, "default_status_path", lambda: tmp_path / "status.json")
    monkeypatch.setattr(usage_export, "default_db_path", lambda: tmp_path / "absent.sqlite3")
    answer = _route_client(_cfg("https://127.0.0.1:1")).post("/api/screen-time/export")
    assert answer.status_code == 409
    assert "no usage log yet" in answer.json()["detail"]


def test_route_status_and_send(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, usage_db: Path) -> None:
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(usage_export, "default_status_path", lambda: status_path)
    monkeypatch.setattr(usage_export, "default_db_path", lambda: usage_db)
    client = _route_client(WebappConfig())
    assert client.get("/api/screen-time/export").json() == {
        "configured": False, "last_ok": None, "last_error": None,
    }
    answer = client.post("/api/screen-time/export")
    assert answer.status_code == 409
    assert "task_os_person" in answer.json()["detail"]

    def fake_run(cfg: WebappConfig) -> usage_export.ExportResult:
        result = usage_export.ExportResult(D1, TODAY, [], dry_run=False, put=3)
        usage_export._record(status_path, result=result)
        return result

    monkeypatch.setattr(usage_export, "run_export", fake_run)
    answer = _route_client(_cfg("https://127.0.0.1:1")).post("/api/screen-time/export")
    assert answer.status_code == 200
    body = answer.json()
    assert body["result"]["put"] == 3 and body["configured"] is True
    assert body["result"]["summary"] == "Nothing to send."
    assert body["last_ok"]["put"] == 3
