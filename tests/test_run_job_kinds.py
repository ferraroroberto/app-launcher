"""Executor-level end-to-end fires for the new job kinds (issue #70).

Real ``RunJobCommand.execute()`` runs — a real child process spawns, real
``run.json``/``output.log`` land on disk — same harness pattern as
``tests/test_run_job_dry_run.py``. ``inline-shell`` uses a ``.bat`` body
(no PowerShell-path assumption needed). ``http-check`` hits a real local
``http.server`` on loopback (deterministic, fully offline) rather than
mocking ``httpx`` inside a separate process the test can't reach.
"""

from __future__ import annotations

import http.server
import threading
from types import SimpleNamespace

import pytest

from app.cli.commands import run_job_cmd as rjc
from src import jobs as jobs_mod
from src import jobs_history as jobs_history_mod
from src import jobs_queue as jobs_queue_mod
from src.app_config import AppConfig
from src.jobs_config import Job, JobsConfig, save_jobs


@pytest.fixture
def isolated_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
    monkeypatch.setattr(jobs_queue_mod, "JOBS_QUEUE_PATH", tmp_path / "_queue.json")
    from src import jobs_config as jc

    monkeypatch.setattr(jc, "DEFAULT_JOBS_PATH", tmp_path / "jobs.json")
    return tmp_path


def _silence_notifier(monkeypatch):
    monkeypatch.setattr(
        rjc, "load_webapp_config",
        lambda: SimpleNamespace(
            notify_on_failure=False,
            notify_failure_summary=False,
            notify_failure_streak=0,
        ),
    )


def _fire(job: Job) -> int:
    save_jobs(JobsConfig(jobs=[job]))
    cmd = rjc.RunJobCommand(AppConfig())
    return cmd.execute(SimpleNamespace(
        job_id=job.id, trigger="manual", run_id=None, params=None, dry_run=False,
    ))


class TestInlineShellExecutor:
    def test_inline_bat_body_runs_and_writes_run_dir(self, isolated_jobs, monkeypatch):
        _silence_notifier(monkeypatch)
        job = Job(
            id="inline-demo", name="Inline demo", script_path="",
            kind="inline-shell",
            kind_config={
                "script_body": (
                    "@echo off\r\n"
                    "echo hello-inline\r\n"
                    "echo artifact-ok>\"%JOB_ARTIFACT_DIR%\\report.txt\"\r\n"
                ),
                "ext": ".bat",
            },
        )
        rc = _fire(job)
        assert rc == 0

        latest = jobs_mod.latest_run("inline-demo")
        assert latest is not None
        assert latest["status"] == "success"
        assert latest["exit_code"] == 0

        run_dir = jobs_mod.runs_dir("inline-demo") / latest["run_id"]
        temp_script = run_dir / "_inline.bat"
        assert temp_script.is_file()
        assert "echo hello-inline" in temp_script.read_text(encoding="utf-8")
        output = (run_dir / "output.log").read_text(encoding="utf-8", errors="replace")
        assert "hello-inline" in output
        assert (run_dir / "artifacts" / "report.txt").read_text(
            encoding="utf-8"
        ).strip() == "artifact-ok"

    def test_missing_script_file_fails_cleanly_with_run_record(
        self, isolated_jobs, monkeypatch
    ):
        # A file-kind job whose script got moved/deleted between save-time
        # and fire-time (job_from_dict only requires script_path be
        # non-empty, not that the file exists) must still finalise a
        # visible failed run record — not silently strand an empty run dir.
        # This exercises the run_dir-created-before-build_invocation
        # reorder from issue #70 (inline-shell needs run_dir to exist
        # before build_invocation runs, so run_dir now always exists by
        # the time a build failure can occur).
        _silence_notifier(monkeypatch)
        job = Job(id="py-missing", name="Py missing", script_path=str(isolated_jobs / "ghost.py"))
        rc = _fire(job)
        assert rc == 2

        latest = jobs_mod.latest_run("py-missing")
        assert latest is not None
        assert latest["status"] == "failed"
        assert latest["exit_code"] == -1
        assert "invocation error" in latest.get("note", "")
        run_dir = jobs_mod.runs_dir("py-missing") / latest["run_id"]
        assert list(run_dir.iterdir())  # not an empty stranded directory


class TestHttpCheckExecutor:
    @pytest.fixture
    def local_server(self):
        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass  # keep test output quiet

        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/"
        finally:
            server.shutdown()
            thread.join(timeout=5)

    def test_http_check_success(self, isolated_jobs, monkeypatch, local_server):
        _silence_notifier(monkeypatch)
        job = Job(
            id="http-ok", name="HTTP ok", script_path="", kind="http-check",
            kind_config={"url": local_server, "expect_status": 200},
        )
        rc = _fire(job)
        assert rc == 0

        latest = jobs_mod.latest_run("http-ok")
        assert latest["status"] == "success"
        assert latest["exit_code"] == 0
        run_dir = jobs_mod.runs_dir("http-ok") / latest["run_id"]
        output = (run_dir / "output.log").read_text(encoding="utf-8", errors="replace")
        assert "200" in output

    def test_http_check_wrong_expected_status_fails(self, isolated_jobs, monkeypatch, local_server):
        _silence_notifier(monkeypatch)
        job = Job(
            id="http-mismatch", name="HTTP mismatch", script_path="", kind="http-check",
            kind_config={"url": local_server, "expect_status": 404},
        )
        rc = _fire(job)
        assert rc == 1

        latest = jobs_mod.latest_run("http-mismatch")
        assert latest["status"] == "failed"
        assert latest["exit_code"] == 1

    def test_http_check_unreachable_url_fails(self, isolated_jobs, monkeypatch):
        _silence_notifier(monkeypatch)
        # Port 1 on loopback: nothing listens there — a fast, deterministic
        # connection-refused without touching the network stack for real.
        job = Job(
            id="http-down", name="HTTP down", script_path="", kind="http-check",
            kind_config={"url": "http://127.0.0.1:1/", "timeout": 3},
        )
        rc = _fire(job)
        assert rc == 1
        latest = jobs_mod.latest_run("http-down")
        assert latest["status"] == "failed"
