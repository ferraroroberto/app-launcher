"""`visible` job flag (issue #91).

A visible job (a) materialises its schtasks `/TR` under `python.exe` so a
console window appears on a scheduled fire, and (b) makes the executor tee
the child's output to that console as well as `output.log`. These tests
cover the schema round-trip, the interpreter choice, and the tee helper.
"""

from __future__ import annotations

import io
import subprocess
import threading
from types import SimpleNamespace
from typing import List

from app.cli.commands import run_job_supervision
from app.cli.commands.run_job_supervision import (
    tee_pipe_to_file_and_console,
)
from src import jobs as jobs_mod
from src import jobs_config as jc
from src.jobs_config import (
    Job,
    JobsConfig,
    Schedule,
    job_from_dict,
    save_jobs,
    update_job,
)


def _mk_completed(stdout: str = "", rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")


class TestVisibleRoundTrip:
    def test_default_is_false_and_omitted(self):
        job = Job(id="j", name="J", script_path="C:\\x\\s.py")
        assert job.visible is False
        assert "visible" not in job.to_dict()

    def test_true_emitted_and_parsed(self):
        job = Job(id="j", name="J", script_path="C:\\x\\s.py", visible=True)
        payload = job.to_dict()
        assert payload["visible"] is True
        # Round-trips back through job_from_dict.
        assert job_from_dict(payload).visible is True

    def test_from_dict_defaults_false(self):
        job = job_from_dict(
            {"id": "j", "name": "J", "script_path": "C:\\x\\s.py"}
        )
        assert job.visible is False

    def test_update_job_toggles(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jc, "DEFAULT_JOBS_PATH", tmp_path / "jobs.json")
        cfg = JobsConfig(jobs=[Job(id="j", name="J", script_path="C:\\x\\s.py")])
        save_jobs(cfg)
        update_job(cfg, "j", visible=True)
        assert jc.get_by_id(cfg, "j").visible is True
        update_job(cfg, "j", visible=False)
        assert jc.get_by_id(cfg, "j").visible is False


class TestTaskRunCommandInterpreter:
    def test_visible_uses_python_not_pythonw(self):
        tr = jobs_mod.task_run_command("demo", visible=True)
        assert "pythonw.exe" not in tr
        assert "python.exe" in tr

    def test_default_uses_pythonw(self):
        tr = jobs_mod.task_run_command("demo")
        assert "pythonw.exe" in tr


class TestSyncSchtasksHonoursVisible:
    def test_visible_job_tr_uses_python(self):
        job = Job(
            id="vis",
            name="Vis",
            script_path="C:\\stub\\demo.py",
            schedule=Schedule(type="weekly", day="THU", at="22:00"),
            visible=True,
        )
        calls: List[List[str]] = []

        def runner(argv):
            calls.append(argv)
            if argv[:2] == ["schtasks", "/Query"]:
                return _mk_completed(stdout="", rc=0)
            return _mk_completed(rc=0)

        created = jobs_mod.sync_schtasks(job, runner=runner)
        assert created == ["\\AppLauncher\\vis"]
        create = next(c for c in calls if c[:2] == ["schtasks", "/Create"])
        tr = create[create.index("/TR") + 1]
        assert "python.exe" in tr and "pythonw.exe" not in tr


class TestTeeHelper:
    def test_writes_to_both_file_and_console(self, monkeypatch):
        data = b"line one\nline two\n"
        console_buf = io.BytesIO()
        monkeypatch.setattr(
            "sys.stdout", SimpleNamespace(buffer=console_buf), raising=False
        )
        fh = io.BytesIO()
        tee_pipe_to_file_and_console(io.BytesIO(data), fh)
        assert fh.getvalue() == data
        assert console_buf.getvalue() == data

    def test_survives_missing_console(self, monkeypatch):
        # No usable console (buffer absent) → file half still works.
        data = b"only file\n"
        monkeypatch.setattr("sys.stdout", SimpleNamespace(), raising=False)
        fh = io.BytesIO()
        tee_pipe_to_file_and_console(io.BytesIO(data), fh)
        assert fh.getvalue() == data


class _StalledConsole:
    """Console double whose ``write`` blocks until explicitly released.

    Models the issue #694 trigger: a Windows console in mark/select mode
    accepts the write call and simply never returns — no exception, no
    timeout.
    """

    def __init__(self) -> None:
        self.entered_write = threading.Event()
        self.release = threading.Event()
        self.written = bytearray()

    def write(self, chunk: bytes) -> None:
        self.entered_write.set()
        self.release.wait()
        self.written.extend(chunk)

    def flush(self) -> None:
        pass


class _BrokenConsole:
    """Console double that raises on every write, as a closed console does."""

    def __init__(self) -> None:
        self.attempts = 0

    def write(self, chunk: bytes) -> None:
        self.attempts += 1
        raise OSError("console closed")

    def flush(self) -> None:  # pragma: no cover — never reached
        pass


class TestTeeStalledConsole:
    """Issue #694 — a non-draining console must not wedge the run."""

    def test_stalled_console_does_not_block_file_half(self, monkeypatch):
        # More chunks than the console queue can hold, so a stalled
        # console is guaranteed to hit the drop path rather than block.
        chunk_count = run_job_supervision._CONSOLE_QUEUE_MAX_CHUNKS * 4
        data = b"".join(bytes([65 + (i % 26)]) * 4096 for i in range(chunk_count))
        console = _StalledConsole()
        monkeypatch.setattr(
            "sys.stdout", SimpleNamespace(buffer=console), raising=False
        )
        # A wedged console can't consume the EOF sentinel, so the helper
        # waits out this bounded join; keep the test fast.
        monkeypatch.setattr(run_job_supervision, "_CONSOLE_DRAIN_TIMEOUT_SECONDS", 0.5)
        drops: List[int] = []
        monkeypatch.setattr(run_job_supervision, "_log_console_tee_drops", drops.append)

        fh = io.BytesIO()
        done = threading.Event()

        def run() -> None:
            tee_pipe_to_file_and_console(io.BytesIO(data), fh)
            done.set()

        reader = threading.Thread(target=run, daemon=True)
        reader.start()
        try:
            # The console really did stall — otherwise this test proves
            # nothing about the blocking case.
            assert console.entered_write.wait(timeout=10)
            # ...and the reader still ran to EOF anyway.
            assert done.wait(timeout=10), "reader blocked on the stalled console"
            assert fh.getvalue() == data
            # Console output is lossy by design; the file is not.
            assert len(console.written) < len(data)
            # ...and the loss left a breadcrumb rather than being silent.
            assert drops and drops[0] > 0
        finally:
            console.release.set()
            reader.join(timeout=10)

    def test_broken_console_mid_stream_keeps_filling_file(self, monkeypatch):
        data = b"x" * 4096 + b"y" * 4096 + b"z" * 100
        console = _BrokenConsole()
        monkeypatch.setattr(
            "sys.stdout", SimpleNamespace(buffer=console), raising=False
        )
        fh = io.BytesIO()
        tee_pipe_to_file_and_console(io.BytesIO(data), fh)
        assert fh.getvalue() == data
        # Stopped writing after the first failure instead of retrying.
        assert console.attempts == 1
