"""Contract: argv handed to the detached run-job spawn stays intact.

``src.jobs_schtasks.spawn_run_job_detached`` re-parents the executor through
``cmd /c start`` (issue #416) so the child is orphaned out of the tray's
process subtree. That hop means the command line is tokenized a second time
by ``cmd.exe``, which does **not** honour the backslash-escaping convention
``subprocess.list2cmdline`` uses — so a value carrying one of cmd's own
metacharacters does not survive as a single argv element.

The repo already enforces this contract one layer down for ``.bat`` argv
(``src/jobs_kinds/batch.py``, issue #409); these tests pin the same contract
at the earlier hop, where webhook-mapped values (``src.jobs_webhook``
``resolve_mapping``) first reach a command line.
"""

from __future__ import annotations

import pytest

from src import jobs_schtasks


@pytest.fixture
def captured_spawn(monkeypatch):
    """Capture the argv ``spawn_run_job_detached`` would hand to Popen."""
    seen = {}

    class _FakeProc:
        pid = 4321

    def _fake_popen(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        return _FakeProc()

    monkeypatch.setattr(jobs_schtasks.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(jobs_schtasks, "_launcher_python", lambda: "py.exe")
    monkeypatch.setattr(jobs_schtasks, "_launcher_py", lambda: "launcher.py")
    return seen


@pytest.mark.parametrize(
    "bad_value",
    [
        "a&b",
        "a|b",
        "a^b",
        "a<b",
        "a>b",
        'a"b',
        "%PATH%",
        "before-%USERPROFILE%-after",
    ],
)
def test_param_value_with_cmd_metacharacter_is_refused(
    captured_spawn, bad_value
):
    """A param value that cannot survive the second tokenization is refused
    loudly rather than being handed to ``cmd`` in a broken quote state."""
    with pytest.raises(ValueError):
        jobs_schtasks.spawn_run_job_detached(
            "demo-job",
            "run-1",
            trigger="manual",
            params={"x": bad_value},
        )
    assert "cmd" not in captured_spawn, (
        "refused values must never reach the spawn"
    )


def test_ordinary_param_values_still_spawn(captured_spawn):
    """Regression guard: the refusal above must not block normal params."""
    pid = jobs_schtasks.spawn_run_job_detached(
        "demo-job",
        "run-1",
        trigger="manual",
        params={"branch": "feat/some-thing", "count": 3},
    )
    assert pid == 4321
    cmd = captured_spawn["cmd"]
    assert cmd[:5] == ["cmd", "/c", "start", "", "/b"]
    assert "--params" in cmd


def test_percent_encoded_param_value_still_spawns(captured_spawn):
    """Regression guard (issue #810): a lone `%` (URL-encoding) must not be
    swept up by the `%name%` env-var-expansion refusal."""
    pid = jobs_schtasks.spawn_run_job_detached(
        "demo-job",
        "run-1",
        trigger="manual",
        params={"q": "100%20off"},
    )
    assert pid == 4321
    assert "--params" in captured_spawn["cmd"]


def test_no_params_path_is_unaffected(captured_spawn):
    """Schedule / Stream-Deck callers omit params entirely."""
    jobs_schtasks.spawn_run_job_detached("demo-job", "run-1", trigger="schedule")
    assert "--params" not in captured_spawn["cmd"]


class TestRefusalReachesTheRunRecord:
    """A refused value must not escape as an unhandled exception.

    Since #794 the refusal fires *inside*
    ``src.jobs_queue.admit_and_spawn``, which has already created the run
    directory and written ``status="pending"`` by the time the spawn is
    attempted. An escaping ``ValueError`` would therefore leave exactly the
    orphaned ``pending`` record that helper exists to prevent (and, on the
    webhook/manual routes, an unhandled 500 rather than the route's own
    "spawn failed" translation). It is recorded like a failed spawn.
    """

    @pytest.fixture
    def isolated_jobs(self, tmp_path, monkeypatch):
        from src import jobs_history as jobs_history_mod
        from src import jobs_queue as jobs_queue_mod

        monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
        monkeypatch.setattr(
            jobs_queue_mod, "JOBS_QUEUE_PATH", tmp_path / "_queue.json"
        )
        return tmp_path

    def test_refused_param_is_recorded_as_failed_not_raised(self, isolated_jobs):
        import json

        from src import jobs_queue as jobs_queue_mod
        from src.jobs_config import Job

        job = Job(id="solo", name="Solo", script_path="C:/x.py")

        meta = jobs_queue_mod.admit_and_spawn(
            [job], job, "webhook", params={"branch": "a&b"}
        )

        assert meta is not None
        assert meta["status"] == "failed"
        assert meta["exit_code"] == -1
        assert "&" in meta["spawn_error"]
        assert meta["refused"] is True, (
            "a refused argv must be distinguishable from a genuine spawn "
            "failure (issue #810) so the route can answer 400, not 500"
        )
        record = json.loads(
            (isolated_jobs / "solo" / meta["run_id"] / "run.json").read_text(
                encoding="utf-8"
            )
        )
        assert record["status"] == "failed", "no orphaned pending record"
        assert record["spawn_error"] == meta["spawn_error"]
        assert record["refused"] is True

    def test_genuine_spawn_failure_is_not_marked_refused(
        self, isolated_jobs, monkeypatch
    ):
        """An `OSError` from the spawn itself (not an `ArgvRejected`) must
        keep `refused=False` so the route still answers 500 (issue #810)."""
        from src import jobs_queue as jobs_queue_mod
        from src.jobs_config import Job

        def _boom(*args, **kwargs):
            raise OSError("the executable vanished")

        monkeypatch.setattr(jobs_queue_mod, "spawn_run_job_detached", _boom)

        job = Job(id="solo", name="Solo", script_path="C:/x.py")
        meta = jobs_queue_mod.admit_and_spawn([job], job, "webhook")

        assert meta is not None
        assert meta["status"] == "failed"
        assert meta["refused"] is False

    def test_refused_param_on_the_queued_path_does_not_escape_the_drain(
        self, isolated_jobs
    ):
        """The mutex-queued branch enqueues without attempting a spawn, so a
        refused value reaches `spawn_run_job_detached` only at drain time —
        called by the kill route and the reaper, neither of which fired it."""
        from src import jobs_queue as jobs_queue_mod
        from src.jobs_config import Job

        holder = Job(id="holder", name="Holder", script_path="C:/h.py",
                     mutex_group="g")
        queued = Job(id="queued", name="Queued", script_path="C:/q.py",
                     mutex_group="g")
        from tests.test_jobs_admit_and_spawn import _seed_run

        _seed_run(isolated_jobs, "holder", "run-holder", status="running")

        meta = jobs_queue_mod.admit_and_spawn(
            [holder, queued], queued, "webhook", params={"branch": "a&b"}
        )
        assert meta is not None and meta["status"] == "queued"

        # No exception, and the drain reports "nothing spawned".
        assert jobs_queue_mod.drain_mutex_queue("g") is None
