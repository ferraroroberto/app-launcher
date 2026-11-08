"""A Job's outbox: files a scheduled run sends without a hosted session.

The executor scrubs the per-session identity it would otherwise inherit and
stamps the run's own; the route takes a file for a run that is still
running, through the session outbox's guards. Every file is invented under
``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.cli.commands import run_job_cmd

# ---------------------------------------------------------------- child env


def test_a_job_child_never_inherits_a_hosted_sessions_identity(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APP_LAUNCHER_SESSION_ID", "stale-session")
    monkeypatch.setenv("APP_LAUNCHER_AGENT", "claude")
    run_dir = tmp_path / "jobs" / "demo" / "20261107T120000"
    run_dir.mkdir(parents=True)
    env = run_job_cmd._child_env("demo", {}, {}, run_dir, False)
    assert "APP_LAUNCHER_SESSION_ID" not in env
    assert "APP_LAUNCHER_AGENT" not in env


def test_a_job_child_is_stamped_with_its_own_run_identity(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APP_LAUNCHER_JOB_RUN_ID", "inherited-run")
    run_dir = tmp_path / "jobs" / "demo" / "20261107T120000"
    run_dir.mkdir(parents=True)
    env = run_job_cmd._child_env("demo", {}, {}, run_dir, False)
    assert env["APP_LAUNCHER_JOB_ID"] == "demo"
    assert env["APP_LAUNCHER_JOB_RUN_ID"] == "20261107T120000"
    assert env["JOB_ARTIFACT_DIR"] == str((run_dir / "artifacts").resolve())


def test_a_job_overlay_cannot_forge_a_session_identity(tmp_path: Path):
    run_dir = tmp_path / "jobs" / "demo" / "r1"
    run_dir.mkdir(parents=True)
    env = run_job_cmd._child_env(
        "demo", {"APP_LAUNCHER_SESSION_ID": "forged"}, {"APP_LAUNCHER_JOB_ID": "other"}, run_dir, False
    )
    assert "APP_LAUNCHER_SESSION_ID" not in env
    assert env["APP_LAUNCHER_JOB_ID"] == "demo"


# ------------------------------------------------------------------- route

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JOB, RUN = "demo", "20261107T120000"


@pytest.fixture
def run_dir(webapp_client, monkeypatch, tmp_path: Path) -> Path:
    """A registered job ``demo`` with one running run; the PC is the caller."""
    from app.webapp.routers import jobs_outbox_routes
    from src import jobs as jobs_mod
    from src import middleware

    monkeypatch.setattr(
        middleware.trust, "LOOPBACK_HOSTS", frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )
    monkeypatch.setattr(jobs_outbox_routes, "get_by_id", lambda cfg, jid: object() if jid == JOB else None)
    folder = jobs_mod.runs_dir(JOB) / RUN
    (folder / "artifacts").mkdir(parents=True)
    jobs_mod.write_run_json(folder, run_id=RUN, status="running")
    webapp_client[1].state.webapp_config.outbox_scratch_roots = []
    return folder


def _post(client, body, job=JOB, run=RUN):
    return client.post(f"/api/jobs/{job}/runs/{run}/outbox", json=body)


def test_a_running_job_sends_a_file_that_the_run_lists_and_serves(webapp_client, run_dir: Path):
    client = webapp_client[0]
    (run_dir / "artifacts" / "shot.png").write_bytes(PNG)
    res = _post(client, {"path": "shot.png", "caption": "Mac screen"})
    assert res.status_code == 200, res.text
    entry = res.json()["entry"]
    assert (entry["kind"], entry["media"], entry["caption"]) == ("image", "image/png", "Mac screen")
    listed = client.get(f"/api/jobs/{JOB}/runs/{RUN}/outbox").json()
    assert listed["rev"] == 1 and listed["entries"] == [entry]
    got = client.get(f"/api/jobs/{JOB}/runs/{RUN}/outbox/{entry['id']}")
    assert got.status_code == 200 and got.content == PNG
    assert got.headers["content-security-policy"].startswith("sandbox")
    assert got.headers["cache-control"] == "no-store"
    assert client.get(f"/api/jobs/{JOB}/runs/{RUN}/outbox/" + "0" * 32).json()["detail"]["reason"] == "entry_not_found"


def test_every_refusal_names_its_own_reason(webapp_client, run_dir: Path, tmp_path: Path, monkeypatch):
    from app.webapp.routers import jobs_outbox_routes
    from src import jobs as jobs_mod

    client = webapp_client[0]
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(PNG)
    (run_dir / "artifacts" / "fake.png").write_bytes(b"not a png")
    (run_dir / "artifacts" / "run.exe").write_bytes(b"MZ")
    seen = {
        "bad_request": _post(client, {"caption": "no path"}),
        "bad_id": _post(client, {"path": "x.png"}, run="a.b"),
        "job_not_found": _post(client, {"path": "x.png"}, job="other"),
        "run_not_found": _post(client, {"path": "x.png"}, run="20990101T000000"),
        "out_of_root": _post(client, {"path": str(outside)}),
        "not_found": _post(client, {"path": "missing.png"}),
        "not_a_file": _post(client, {"path": "."}),
        "type_not_allowed": _post(client, {"path": "run.exe"}),
        "content_mismatch": _post(client, {"path": "fake.png"}),
    }
    reasons = {k: (r.status_code, r.json()["detail"]["reason"]) for k, r in seen.items()}
    assert reasons == {
        "bad_request": (400, "bad_request"),
        "bad_id": (400, "bad_request"),
        "job_not_found": (404, "job_not_found"),
        "run_not_found": (404, "run_not_found"),
        "out_of_root": (403, "out_of_root"),
        "not_found": (404, "not_found"),
        "not_a_file": (422, "not_a_file"),
        "type_not_allowed": (415, "type_not_allowed"),
        "content_mismatch": (415, "content_mismatch"),
    }
    jobs_mod.write_run_json(run_dir, status="success")
    (run_dir / "artifacts" / "late.png").write_bytes(PNG)
    late = _post(client, {"path": "late.png"})
    assert (late.status_code, late.json()["detail"]["reason"]) == (409, "run_not_running")
    monkeypatch.setattr(jobs_outbox_routes, "is_pc_itself", lambda host, headers: False)
    remote = _post(client, {"path": "late.png"})
    assert (remote.status_code, remote.json()["detail"]["reason"]) == (403, "loopback_only")
    assert client.get(f"/api/jobs/{JOB}/runs/{RUN}/outbox").json()["entries"] == []


def test_a_declared_scratch_root_is_also_a_job_send_root(webapp_client, run_dir: Path, tmp_path: Path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "s.png").write_bytes(PNG)
    webapp_client[1].state.webapp_config.outbox_scratch_roots = [str(scratch)]
    assert _post(webapp_client[0], {"path": str(scratch / "s.png")}).status_code == 200


def test_a_job_outbox_read_is_passkey_gated_and_unreachable_off_the_tailnet(webapp_client):
    from src.middleware import _terminal_guard_level

    for path in (f"/api/jobs/{JOB}/runs/{RUN}/outbox", f"/api/jobs/{JOB}/runs/{RUN}/outbox/abc"):
        assert _terminal_guard_level(path) == "passkey", path
    assert _terminal_guard_level(f"/api/jobs/{JOB}/runs/{RUN}") is None
    assert webapp_client[0].get(f"/api/jobs/{JOB}/runs/{RUN}/outbox").status_code == 403


def test_the_run_detail_carries_only_the_sent_file_count(webapp_client, run_dir: Path, monkeypatch):
    from app.webapp.routers import _helpers
    from app.webapp.routers import jobs as jobs_router

    monkeypatch.setattr(jobs_router, "get_by_id", lambda cfg, jid: object() if jid == JOB else None)
    monkeypatch.setattr(_helpers, "get_by_id", lambda cfg, jid: object() if jid == JOB else None)
    client = webapp_client[0]
    detail = lambda: client.get(f"/api/jobs/{JOB}/runs/{RUN}").json()["run"]  # noqa: E731
    assert detail()["sent_files"] == 0
    (run_dir / "artifacts" / "shot.png").write_bytes(PNG)
    assert _post(client, {"path": "shot.png", "caption": "private caption"}).status_code == 200
    run = detail()
    assert run["sent_files"] == 1 and "private caption" not in str(run)
