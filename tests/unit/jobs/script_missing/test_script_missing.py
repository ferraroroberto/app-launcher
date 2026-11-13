"""A saved Job whose script has since moved is flagged, and the chief told once a day.

Save-time pre-flight proves the path existed when the job was saved; these
tests move the script afterwards, the case it cannot see. The mailbox is
always a ``tmp_path``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import instance_role
from src.chief import job_failed
from src.chief import script_missing as chief_script_missing
from src.jobs.config import Job
from src.jobs.script_missing import script_missing


def _job(script: Path, **over) -> Job:
    return Job(id=over.pop("id", "li-scrape"), name=over.pop("name", "LI scrape"),
               script_path=str(script), **over)


def _cfg(tmp_path: Path, **over) -> SimpleNamespace:
    base = dict(chief_job_failures=True, chief_inbox_dir=str(tmp_path / "inbox"),
                projects_dir=str(tmp_path / "projects"))
    base.update(over)
    return SimpleNamespace(**base)


def _events(cfg) -> list:
    inbox = Path(cfg.chief_inbox_dir)
    if not inbox.is_dir():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(inbox.iterdir())
            if p.name.endswith(".json")]


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr(instance_role, "canonical_instance", lambda *a, **k: (True, "canonical"))
    monkeypatch.setattr(job_failed, "chief_live", lambda cfg, kind="": True)


class TestCheck:
    def test_script_deleted_after_save_reads_missing(self, tmp_path):
        script = tmp_path / "scrape.py"
        script.write_text("print('ok')\n", encoding="utf-8")
        job = _job(script)
        assert script_missing(job) is False
        script.unlink()
        assert script_missing(job) is True

    def test_a_directory_in_the_script_slot_is_missing(self, tmp_path):
        (tmp_path / "scrape.py").mkdir()
        assert script_missing(_job(tmp_path / "scrape.py")) is True

    def test_paused_job_is_skipped(self, tmp_path):
        job = _job(tmp_path / "gone.py")
        assert script_missing(job) is True
        paused = _job(tmp_path / "gone.py", schedule=job.schedule, paused_schedule=job.schedule)
        assert paused.is_paused and script_missing(paused) is None

    def test_a_path_the_filesystem_cannot_answer_for_is_not_established(self, tmp_path, monkeypatch):
        script = tmp_path / "scrape.py"
        script.write_text("", encoding="utf-8")

        def denied(_path):
            raise PermissionError("denied")

        monkeypatch.setattr("src.jobs.script_missing.os.stat", denied)
        assert script_missing(_job(script)) is None


class TestJobsList:
    def _seed(self, overrides, script: Path) -> None:
        overrides["tmp_jobs_path"].write_text(json.dumps({"jobs": [
            {"id": "li-scrape", "name": "LI scrape", "script_path": str(script)},
        ]}), encoding="utf-8")

    def _row(self, client) -> dict:
        (row,) = client.get("/api/jobs").json()["jobs"]
        return row

    def test_a_moved_script_shows_on_the_row_and_a_valid_one_shows_nothing(self, webapp_client, tmp_path):
        client, _app, overrides = webapp_client
        script = tmp_path / "scrape.py"
        script.write_text("print('ok')\n", encoding="utf-8")
        self._seed(overrides, script)
        assert self._row(client)["script_missing"] is False
        script.unlink()
        assert self._row(client)["script_missing"] is True


class TestChiefEvent:
    def test_one_event_per_job_per_day(self, tmp_path, armed):
        cfg, seen = _cfg(tmp_path), tmp_path / "seen.json"
        jobs = [_job(tmp_path / "projects" / "content-management" / "gone.py")]
        morning, evening = datetime(2026, 11, 14, 6, 0), datetime(2026, 11, 14, 22, 0)

        assert len(chief_script_missing.observe(cfg, jobs, now=morning, path=seen)) == 1
        assert chief_script_missing.observe(cfg, jobs, now=evening, path=seen) == []
        assert len(_events(cfg)) == 1

        next_day = datetime(2026, 11, 15, 6, 0)
        assert len(chief_script_missing.observe(cfg, jobs, now=next_day, path=seen)) == 1

    def test_event_names_the_job_and_the_outcome(self, tmp_path, armed):
        cfg = _cfg(tmp_path)
        job = _job(tmp_path / "projects" / "content-management" / "gone.py")
        chief_script_missing.observe(cfg, [job], now=datetime(2026, 11, 14, 6, 0), path=tmp_path / "s.json")
        (event,) = _events(cfg)
        assert (event["kind"], event["v"], event["outcome"]) == ("job-failed", 2, "script-missing")
        assert (event["job_id"], event["repo"], event["exit_code"]) == ("li-scrape", "content-management", None)
        assert event["script_path"] == job.script_path and event["run_id"]

    def test_valid_and_paused_jobs_emit_nothing(self, tmp_path, armed):
        cfg = _cfg(tmp_path)
        good = tmp_path / "good.py"
        good.write_text("", encoding="utf-8")
        gone = _job(tmp_path / "gone.py")
        paused = _job(tmp_path / "gone.py", id="p", schedule=gone.schedule, paused_schedule=gone.schedule)
        out = chief_script_missing.observe(cfg, [_job(good), paused], path=tmp_path / "s.json")
        assert out == [] and _events(cfg) == []

    def test_no_live_chief_publishes_nothing_and_the_day_stays_open(self, tmp_path, monkeypatch):
        monkeypatch.setattr(instance_role, "canonical_instance", lambda *a, **k: (True, "canonical"))
        live = {"value": None}
        monkeypatch.setattr(job_failed, "chief_live", lambda cfg, kind="": live["value"])
        cfg, seen = _cfg(tmp_path), tmp_path / "seen.json"
        jobs = [_job(tmp_path / "gone.py")]
        now = datetime(2026, 11, 14, 6, 0)

        assert chief_script_missing.observe(cfg, jobs, now=now, path=seen) == []
        live["value"] = True
        assert len(chief_script_missing.observe(cfg, jobs, now=now, path=seen)) == 1

    def test_non_canonical_instance_publishes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(instance_role, "canonical_instance", lambda *a, **k: (False, "disposable"))
        monkeypatch.setattr(job_failed, "chief_live", lambda cfg, kind="": True)
        cfg = _cfg(tmp_path)
        assert chief_script_missing.observe(cfg, [_job(tmp_path / "gone.py")], path=tmp_path / "s.json") == []

    def test_switch_off_publishes_nothing(self, tmp_path, armed):
        cfg = _cfg(tmp_path, chief_job_failures=False)
        assert chief_script_missing.observe(cfg, [_job(tmp_path / "gone.py")], path=tmp_path / "s.json") == []
