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


def test_no_params_path_is_unaffected(captured_spawn):
    """Schedule / Stream-Deck callers omit params entirely."""
    jobs_schtasks.spawn_run_job_detached("demo-job", "run-1", trigger="schedule")
    assert "--params" not in captured_spawn["cmd"]
