"""run_job_cmd's venv walk-up and invocation, and src.jobs run-history I/O."""

from __future__ import annotations

import sqlite3

import pytest

from app.cli.commands.run_job_cmd import build_invocation
from src import jobs as jobs_mod
from src import jobs_history as jobs_history_mod
from src import jobs_index as jobs_index_mod
from src.jobs import resolve_venv_python
from src.jobs_config import (
    Job,
)


# ================================================================ executor


class TestResolveVenvPython:
    def test_walks_up_to_sibling_venv(self, tmp_path):
        # Layout: <root>/proj/sub/script.py with <root>/proj/.venv/Scripts/python.exe
        root = tmp_path
        proj = root / "proj"
        sub = proj / "sub"
        sub.mkdir(parents=True)
        venv_python = proj / ".venv" / "Scripts" / "python.exe"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("stub")
        script = sub / "script.py"
        script.write_text("# stub")
        resolved = resolve_venv_python(script)
        assert resolved == venv_python

    def test_returns_none_when_no_venv(self, tmp_path):
        script = tmp_path / "lonely.py"
        script.write_text("# stub")
        assert resolve_venv_python(script) is None


class TestBuildInvocation:
    def test_bat_dispatch(self, tmp_path):
        bat = tmp_path / "demo.bat"
        bat.write_text("@echo off")
        job = Job(id="demo", name="Demo", script_path=str(bat), args="auto")
        argv, cwd, env = build_invocation(job, run_dir=tmp_path)
        assert argv == ["cmd.exe", "/c", str(bat), "auto"]
        assert cwd == bat.parent
        assert env == {}

    def test_py_dispatch_with_venv(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        venv_py = proj / ".venv" / "Scripts" / "python.exe"
        venv_py.parent.mkdir(parents=True)
        venv_py.write_text("stub")
        script = proj / "sub" / "scrape.py"
        script.parent.mkdir()
        script.write_text("# stub")
        job = Job(id="ls", name="LS", script_path=str(script), args="")
        argv, cwd, env = build_invocation(job, run_dir=tmp_path)
        assert argv == [str(venv_py), str(script)]
        # cwd is the project root (where the .venv lives).
        assert cwd == proj
        # PYTHONPATH points at the project root so package imports resolve.
        assert env["PYTHONPATH"] == str(proj)

    def test_bad_suffix_rejected(self, tmp_path):
        job = Job(id="x", name="X", script_path=str(tmp_path / "x.txt"))
        with pytest.raises(ValueError, match="unsupported"):
            build_invocation(job, run_dir=tmp_path)


# ============================================================= run history


class TestRunHistory:
    def test_new_run_dir_creates(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
        rd = jobs_mod.new_run_dir("demo", "20260523T060000")
        assert rd.exists()
        assert rd.parent.name == "demo"

    def test_new_run_dir_handles_collisions(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
        rd1 = jobs_mod.new_run_dir("demo", "20260523T060000")
        rd2 = jobs_mod.new_run_dir("demo", "20260523T060000")
        assert rd1 != rd2
        assert rd2.name.endswith("-2")

    def test_write_and_read_run_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
        rd = jobs_mod.new_run_dir("demo", "20260523T060000")
        jobs_mod.write_run_json(rd, status="running", trigger="manual")
        jobs_mod.write_run_json(rd, status="success", exit_code=0)
        record = jobs_mod.read_run(rd)
        # Updates merge — trigger from the first write survives.
        assert record["status"] == "success"
        assert record["trigger"] == "manual"
        assert record["exit_code"] == 0

    def test_kill_precedence_survives_later_finalise(self, tmp_path, monkeypatch):
        # Race (#316): the webapp's Kill writes a killed/failed record, then the
        # naturally-finishing executor finalises with success. The kill's
        # terminal outcome must win — the user explicitly stopped the run.
        monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
        rd = jobs_mod.new_run_dir("demo", "20260523T060000")
        jobs_mod.write_run_json(rd, status="running", started_at="t0")
        # Kill endpoint fires.
        jobs_mod.write_run_json(
            rd, status="failed", exit_code=-9, finished_at="t1", killed=True
        )
        # Executor's finalise lands afterwards with its own outcome + stats.
        jobs_mod.write_run_json(
            rd,
            status="success",
            exit_code=0,
            finished_at="t2",
            duration_seconds=1.5,
            peak_rss_bytes=1024,
        )
        record = jobs_mod.read_run(rd)
        assert record["killed"] is True
        assert record["status"] == "failed"
        assert record["exit_code"] == -9
        assert record["finished_at"] == "t1"
        # Harmless resource stats from the finalise still merge.
        assert record["peak_rss_bytes"] == 1024

    def test_list_runs_newest_first(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
        for stamp in ("20260101T060000", "20260102T060000", "20260103T060000"):
            rd = jobs_mod.new_run_dir("demo", stamp)
            jobs_mod.write_run_json(rd, status="success")
        runs = jobs_mod.list_runs("demo")
        assert [r["run_id"] for r in runs] == [
            "20260103T060000",
            "20260102T060000",
            "20260101T060000",
        ]

    def test_prune_keeps_latest_n(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
        for i in range(5):
            stamp = f"2026010{i + 1}T060000"
            rd = jobs_mod.new_run_dir("demo", stamp)
            jobs_mod.write_run_json(rd, status="success")
        removed = jobs_mod.prune_runs("demo", keep=2)
        assert removed == 3
        survivors = sorted((tmp_path / "demo").iterdir())
        assert [p.name for p in survivors] == [
            "20260104T060000",
            "20260105T060000",
        ]

    def test_prune_keeps_pinned_runs_outside_normal_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
        pinned = None
        for i in range(5):
            stamp = f"2026010{i + 1}T060000"
            rd = jobs_mod.new_run_dir("demo", stamp)
            jobs_mod.write_run_json(rd, status="success", pinned=(i == 0))
            if i == 0:
                pinned = rd
        assert jobs_mod.prune_runs("demo", keep=2) == 2
        survivors = {path.name for path in (tmp_path / "demo").iterdir()}
        assert survivors == {
            "20260101T060000",
            "20260104T060000",
            "20260105T060000",
        }
        assert pinned is not None and pinned.is_dir()

    def test_read_output_tail(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
        rd = jobs_mod.new_run_dir("demo", "20260523T060000")
        (rd / "output.log").write_text("hello\nworld\n", encoding="utf-8")
        assert "world" in jobs_mod.read_output_tail(rd)

    def test_artifact_listing_reports_name_size_and_mtime(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
        rd = jobs_mod.new_run_dir("demo", "20260523T060000")
        artifact = rd / "artifacts" / "report.csv"
        artifact.write_text("a,b\n1,2\n", encoding="utf-8")
        listed = jobs_mod.list_artifacts(rd)
        assert listed[0]["name"] == "report.csv"
        assert listed[0]["size"] == artifact.stat().st_size
        assert "T" in listed[0]["mtime"]

    def test_index_search_is_newest_first_and_rebuilds_schema(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
        for stamp in ("20260101T060000", "20260103T060000"):
            rd = jobs_mod.new_run_dir("demo", stamp)
            (rd / "output.log").write_text(
                f"Traceback marker from {stamp}\n", encoding="utf-8"
            )
            jobs_mod.write_run_json(
                rd,
                job_id="demo",
                run_id=stamp,
                status="failed",
                started_at=stamp,
            )
        hits = jobs_index_mod.search_runs("Traceback")
        assert [hit["run_id"] for hit in hits] == [
            "20260103T060000",
            "20260101T060000",
        ]

        with sqlite3.connect(jobs_index_mod.index_path()) as conn:
            conn.execute("PRAGMA user_version=0")
        jobs_index_mod.ensure_index()
        with sqlite3.connect(jobs_index_mod.index_path()) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == (
                jobs_index_mod.SCHEMA_VERSION
            )
        assert len(jobs_index_mod.search_runs("marker", status="failed")) == 2

    def test_search_hit_outcome_matches_the_history_list_for_a_killed_run(
        self, tmp_path, monkeypatch
    ):
        """#1007 — a launcher-terminated run must classify the same in search
        as in the history list.

        ``run_outcome`` keeps a ``failed`` verdict as ``failed`` when
        ``killed``/``watchdog``/``reaped`` is set, instead of softening it to
        "not confirmed". Those flags were absent from the index schema and the
        search SELECT, so the override could never fire on a search hit: the
        same run rendered ❓ in search and ❌ in the history list, contradicting
        ``search_runs``' own docstring.
        """
        monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
        stamp = "20260210T060000"
        rd = jobs_mod.new_run_dir("demo", stamp)
        (rd / "output.log").write_text("watchdog marker\n", encoding="utf-8")
        # 121 is an "unconfirmed" code, so the flag is what decides the
        # verdict — without it this run classifies as unconfirmed.
        jobs_mod.write_run_json(
            rd,
            job_id="demo",
            run_id=stamp,
            status="failed",
            started_at=stamp,
            exit_code=121,
            watchdog=True,
        )

        from_history = jobs_history_mod.read_run(rd)
        assert from_history["outcome"] == "failed", from_history

        hits = jobs_index_mod.search_runs("watchdog")
        assert len(hits) == 1, hits
        assert hits[0]["outcome"] == from_history["outcome"], (
            f"search says {hits[0]['outcome']!r}, history says "
            f"{from_history['outcome']!r} for the same run"
        )

    def test_search_hit_does_not_leak_the_launcher_terminated_columns(
        self, tmp_path, monkeypatch
    ):
        """The three flags are selected only to classify (#1007); the hit's
        response shape stays what it was."""
        monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
        stamp = "20260211T060000"
        rd = jobs_mod.new_run_dir("demo", stamp)
        (rd / "output.log").write_text("shapecheck marker\n", encoding="utf-8")
        jobs_mod.write_run_json(
            rd,
            job_id="demo",
            run_id=stamp,
            status="failed",
            started_at=stamp,
            exit_code=121,
            reaped=True,
        )
        hit = jobs_index_mod.search_runs("shapecheck")[0]
        assert hit["outcome"] == "failed"
        for flag in ("killed", "watchdog", "reaped"):
            assert flag not in hit, f"{flag} leaked into the search response"
