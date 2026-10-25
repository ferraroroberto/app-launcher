"""The job-failed chief mailbox event (#1783).

The state dir is always a ``tmp_path``: nothing here touches the real
``~/.claude/hooks/state/chief-inbox``. The chief and the canonical-instance
facts are faked at their seams; ``chief_live`` has its own cases against a
faked session-host list.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.webapp.chief_doorbell import count_pending
from src.chief import job_failed as cjf
from src import instance_role, notifications
from src.clients import session as session_client


def _cfg(tmp_path, **over):
    base = dict(
        chief_job_failures=True,
        chief_inbox_dir=str(tmp_path / "chief-inbox"),
        projects_dir=str(tmp_path / "projects"),
        sessions_state_file=str(tmp_path / "sessions-state.json"),
        session_host_port=54321,
        notify_on_failure=False,
        notify_failure_streak=0,
        notify_failure_summary=False,
        llm_hub_url="",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _job(tmp_path, **over):
    base = dict(
        id="snapshot",
        name="Snapshot freshness",
        script_path=str(tmp_path / "projects" / "life-os" / "scripts" / "snap.py"),
        exit_outcomes={},
        alert_on_failure=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _run_dir(tmp_path, *, output="boom\n", trigger="scheduled", name="20261022-040000"):
    run_dir = tmp_path / "runs" / name
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"trigger": trigger}), encoding="utf-8")
    if output is not None:
        (run_dir / "output.log").write_text(output, encoding="utf-8")
    return run_dir


def _fail(tmp_path, cfg=None, *, job=None, status="failed", exit_code=1, run=None, **kw):
    """Finalise one run through the real hook; returns the cfg it used."""
    cfg = cfg or _cfg(tmp_path)
    notifications.notify_failure(
        cfg, job or _job(tmp_path), run or _run_dir(tmp_path),
        status=status, exit_code=exit_code, **kw,
    )
    return cfg


def _events(cfg):
    inbox = Path(cfg.chief_inbox_dir)
    if not inbox.is_dir():
        return []
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(inbox.iterdir())
        if p.name.endswith(".json") and not p.name.startswith(".")
    ]


@pytest.fixture
def armed(monkeypatch):
    """Canonical instance, live chief: the two facts a publish needs."""
    monkeypatch.setattr(instance_role, "canonical_instance", lambda *a, **k: (True, "canonical"))
    monkeypatch.setattr(cjf, "chief_live", lambda cfg: True)


class TestEventShape:
    def test_exit_code_failure_matches_the_contract(self, tmp_path, armed):
        job = _job(tmp_path)
        (event,) = _events(_fail(tmp_path, job=job, exit_code=3))

        assert len(event["id"]) == 32 and event["id"] == event["id"].lower()
        assert {k: event[k] for k in (
            "v", "kind", "event", "job_id", "job_name", "run_id", "exit_code",
            "outcome", "trigger", "script_path", "repo", "excerpt", "excerpt_source",
        )} == {
            "v": 2, "kind": "job-failed", "event": "JobFailed", "job_id": "snapshot",
            "job_name": "Snapshot freshness", "run_id": "20261022-040000", "exit_code": 3,
            "outcome": "failed", "trigger": "schedule", "script_path": job.script_path,
            "repo": "life-os", "excerpt": "boom", "excerpt_source": "job-output",
        }
        for null_field in ("launcher_sid", "number", "dispatch_id"):
            assert event[null_field] is None
        assert event["ts"].endswith("Z") and len(event["ts"]) == 20

    def test_reaped_run_has_null_exit_code_and_chain_trigger(self, tmp_path, armed):
        cfg = _fail(tmp_path, exit_code=None, reaped=True,
                    run=_run_dir(tmp_path, trigger="chain:upstream"))
        (event,) = _events(cfg)
        assert (event["exit_code"], event["outcome"], event["trigger"]) == (None, "failed", "chain")

    def test_unconfirmed_exit_code_is_not_confirmed(self, tmp_path, armed):
        # 114: cancellation not confirmed, an exit code the outcome table
        # itself classifies as unconfirmed.
        assert _events(_fail(tmp_path, exit_code=114))[0]["outcome"] == "not-confirmed"

    def test_excerpt_is_the_last_1500_chars(self, tmp_path, armed):
        run = _run_dir(tmp_path, output="A" * 4000 + "B" * 1500)
        assert _events(_fail(tmp_path, run=run))[0]["excerpt"] == "B" * 1500

    def test_no_output_is_a_null_excerpt_with_a_reason(self, tmp_path, armed):
        (event,) = _events(_fail(tmp_path, run=_run_dir(tmp_path, output=None)))
        assert event["excerpt"] is None and event["excerpt_source"] is None
        assert event["excerpt_unavailable"]

    def test_repo_is_the_projects_child_holding_the_script(self, tmp_path):
        projects = str(tmp_path / "projects")
        assert cjf.owning_repo(str(tmp_path / "projects" / "Life-OS" / "a.py"), projects) == "Life-OS"
        assert cjf.owning_repo(str(tmp_path / "elsewhere" / "a.py"), projects) is None
        assert cjf.owning_repo("a.py", projects) is None
        assert cjf.owning_repo(str(tmp_path / "projects" / "a.py"), projects) is None


class TestExactlyOneEvent:
    def test_one_failed_run_is_one_pending_file_named_ms_dash_id(self, tmp_path, armed):
        cfg = _fail(tmp_path)
        (name,) = os.listdir(cfg.chief_inbox_dir)
        stamp, _, rest = name.partition("-")
        assert stamp.isdigit() and rest == f"{_events(cfg)[0]['id']}.json"
        assert count_pending(Path(cfg.chief_inbox_dir), 0) == 1

    @pytest.mark.parametrize("status", ["success", "running", "killed"])
    def test_a_run_that_did_not_fail_publishes_nothing(self, tmp_path, armed, status):
        assert not os.path.exists(_fail(tmp_path, status=status, exit_code=0).chief_inbox_dir)

    def test_a_deferred_exit_code_publishes_nothing(self, tmp_path, armed):
        job = _job(tmp_path, exit_outcomes={75: "deferred"})
        assert not os.path.exists(_fail(tmp_path, job=job, exit_code=75).chief_inbox_dir)


class TestPreconditions:
    @pytest.mark.parametrize("live", [False, None])
    def test_no_confirmed_chief_publishes_nothing(self, tmp_path, monkeypatch, live):
        """``None`` is an unreadable session-host: not confirmed, so not a chief."""
        monkeypatch.setattr(instance_role, "canonical_instance", lambda *a, **k: (True, "canonical"))
        monkeypatch.setattr(cjf, "chief_live", lambda cfg: live)
        assert not os.path.exists(_fail(tmp_path).chief_inbox_dir)

    @pytest.mark.parametrize("value", [False, "true", 1, None])
    def test_switch_off_or_not_a_real_boolean_publishes_nothing(self, tmp_path, armed, value):
        cfg = _cfg(tmp_path, chief_job_failures=value)
        assert not os.path.exists(_fail(tmp_path, cfg).chief_inbox_dir)

    def test_a_stub_config_without_the_key_publishes_nothing(self, tmp_path, armed):
        cfg = _cfg(tmp_path)
        del cfg.chief_job_failures
        assert not os.path.exists(_fail(tmp_path, cfg).chief_inbox_dir)

    def test_non_canonical_instance_publishes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(instance_role, "canonical_instance", lambda *a, **k: (False, "linked-worktree"))
        monkeypatch.setattr(cjf, "chief_live", lambda cfg: pytest.fail("must not look for a chief"))
        assert not os.path.exists(_fail(tmp_path).chief_inbox_dir)

    def test_disposable_autoboot_instance_is_never_canonical(self, tmp_path, monkeypatch):
        """The real predicate, not the fake: the gate's webapp must not publish."""
        monkeypatch.setenv("LAUNCHER_SESSION_HOST_PORT", "9999")
        monkeypatch.setattr(cjf, "chief_live", lambda cfg: True)
        assert not os.path.exists(_fail(tmp_path).chief_inbox_dir)


class TestBestEffort:
    def test_a_write_failure_is_swallowed_and_other_channels_still_run(
        self, tmp_path, armed, monkeypatch, caplog
    ):
        monkeypatch.setattr(cjf.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
        pushed = []
        notifier = SimpleNamespace(notify=lambda *a, **k: pushed.append(a))
        with caplog.at_level("WARNING"):
            cfg = _fail(tmp_path, _cfg(tmp_path, notify_on_failure=True), notifier=notifier)
        assert pushed and "not published" in caplog.text
        assert not [n for n in os.listdir(cfg.chief_inbox_dir) if not n.startswith(".")]

    def test_a_push_failure_does_not_stop_the_event(self, tmp_path, armed):
        def boom(*a, **k):
            raise RuntimeError("pushover down")

        cfg = _fail(tmp_path, _cfg(tmp_path, notify_on_failure=True),
                    notifier=SimpleNamespace(notify=boom))
        assert len(_events(cfg)) == 1


class TestAtomicPublish:
    def test_the_json_name_appears_only_after_the_replace(self, tmp_path, monkeypatch):
        inbox = tmp_path / "chief-inbox"
        seen = {}
        real_replace = os.replace

        def spy(src, dst):
            seen["before"] = sorted(p.name for p in inbox.iterdir())
            real_replace(src, dst)

        monkeypatch.setattr(cjf.os, "replace", spy)
        path = cjf.publish_event(inbox, {"id": "a" * 32}, now_ms=1_800_000_000_000)

        (tmp_name,) = seen["before"]
        assert tmp_name.startswith(".event.") and tmp_name.endswith(".tmp")
        assert [p.name for p in inbox.iterdir()] == [path.name] == [f"1800000000000-{'a' * 32}.json"]

    def test_a_failed_write_leaves_neither_json_nor_temp(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cjf.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("no")))
        with pytest.raises(OSError):
            cjf.publish_event(tmp_path / "chief-inbox", {"id": "b" * 32})
        assert list((tmp_path / "chief-inbox").iterdir()) == []


class TestChiefLive:
    @pytest.mark.parametrize("sessions,expected", [
        ([{"session_id": "s1", "kind": "pty", "alive": True, "label": "chief"}], True),
        # A chief typed by hand into a plain session carries no label and self-heals.
        ([{"session_id": "s1", "kind": "pty", "alive": True, "project_dir": "E:/x/fleet-config",
           "prompt_title": "/chief"}], True),
        ([{"session_id": "s1", "kind": "pty", "alive": True, "label": "worker"},
          {"session_id": "s2", "kind": "pty", "alive": False, "label": "chief"}], False),
        (session_client.SessionHostError("connection refused"), None),
    ])
    def test_live_absent_and_unknown(self, tmp_path, monkeypatch, sessions, expected):
        def fake(port):
            if isinstance(sessions, Exception):
                raise sessions
            return sessions

        monkeypatch.setattr(session_client, "list_sessions", fake)
        assert cjf.chief_live(_cfg(tmp_path)) is expected
