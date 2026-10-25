"""/api/jobs run records: history, the bulk next-run cache, kill and reap-on-poll."""

from __future__ import annotations


import pytest

from tests.api.jobs._support import _seed_one_job, _stub_path


# ============================================================= run history


class TestRunHistory:
    def test_404_for_unknown_job(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.get("/api/jobs/nope/runs")
        assert resp.status_code == 404

    def test_round_trip_returns_recorded_run(
        self, webapp_client, mocked_jobs_side_effects, monkeypatch
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        # Trigger a run — the route pre-creates the run dir + writes
        # run.json with status=pending.
        run = client.post("/api/jobs/" + created["id"] + "/run").json()
        listing = client.get("/api/jobs/" + created["id"] + "/runs")
        assert listing.status_code == 200
        runs = listing.json()["runs"]
        assert len(runs) == 1
        assert runs[0]["run_id"] == run["run_id"]
        # And the single-run endpoint includes the (empty) output tail.
        single = client.get(
            "/api/jobs/" + created["id"] + "/runs/" + run["run_id"]
        )
        assert single.status_code == 200
        record = single.json()["run"]
        assert record["status"] == "pending"
        assert record.get("output_tail") == ""

    def test_unknown_run_id_returns_404(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        resp = client.get("/api/jobs/" + created["id"] + "/runs/nope")
        assert resp.status_code == 404

    def test_artifact_download_and_traversal_guard(
        self, webapp_client, mocked_jobs_side_effects
    ):
        from src import jobs as jobs_mod

        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Artifacts").json()["job"]
        run = client.post(f"/api/jobs/{created['id']}/run").json()
        run_dir = jobs_mod.runs_dir(created["id"]) / run["run_id"]
        artifact = run_dir / "artifacts" / "report.csv"
        artifact.write_bytes(b"a,b\n1,2\n")

        detail = client.get(f"/api/jobs/{created['id']}/runs/{run['run_id']}")
        assert detail.json()["run"]["artifacts"][0]["name"] == "report.csv"
        download = client.get(
            f"/api/jobs/{created['id']}/runs/{run['run_id']}/artifacts/report.csv"
        )
        assert download.status_code == 200
        assert download.text == "a,b\n1,2\n"
        traversal = client.get(
            f"/api/jobs/{created['id']}/runs/{run['run_id']}"
            "/artifacts/%2E%2E%2Frun.json"
        )
        assert traversal.status_code == 400

    def test_pin_toggle_survives_pruning(
        self, webapp_client, mocked_jobs_side_effects
    ):
        from src import jobs as jobs_mod

        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Pinned").json()["job"]
        first = client.post(f"/api/jobs/{created['id']}/run").json()["run_id"]
        response = client.put(
            f"/api/jobs/{created['id']}/runs/{first}", json={"pinned": True}
        )
        assert response.status_code == 200
        assert response.json()["run"]["pinned"] is True
        for i in range(3):
            rd = jobs_mod.new_run_dir(created["id"], f"2026120{i + 1}T010101")
            jobs_mod.write_run_json(rd, status="success")
        jobs_mod.prune_runs(created["id"], keep=1)
        assert (jobs_mod.runs_dir(created["id"]) / first).is_dir()

    def test_live_stream_snapshot_delta_and_close(
        self, webapp_client, mocked_jobs_side_effects
    ):
        from src import jobs as jobs_mod

        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Stream").json()["job"]
        run_id = client.post(f"/api/jobs/{created['id']}/run").json()["run_id"]
        run_dir = jobs_mod.runs_dir(created["id"]) / run_id
        with client.websocket_connect(
            f"/api/jobs/{created['id']}/runs/{run_id}/stream"
        ) as websocket:
            snapshot = websocket.receive_json()
            assert snapshot == {"type": "snapshot", "output": "", "status": "pending"}
            (run_dir / "output.log").write_bytes(b"hello live\n")
            delta = websocket.receive_json()
            assert delta["type"] == "chunk"
            assert delta["output"] == "hello live\n"
            jobs_mod.write_run_json(run_dir, status="success", exit_code=0)
            final = websocket.receive_json()
            assert final == {"type": "status", "status": "success"}

    def test_cross_run_search_returns_snippet_and_filters_job(
        self, webapp_client, mocked_jobs_side_effects
    ):
        from src import jobs as jobs_mod

        client, _, _ = webapp_client
        first = _seed_one_job(client, name="First").json()["job"]
        second = _seed_one_job(client, name="Second").json()["job"]
        for job, stamp in ((first, "20260101T010101"), (second, "20260102T010101")):
            run_dir = jobs_mod.new_run_dir(job["id"], stamp)
            (run_dir / "output.log").write_bytes(b"prefix unique-needle suffix\n")
            jobs_mod.write_run_json(
                run_dir,
                job_id=job["id"],
                run_id=stamp,
                status="success",
                started_at=stamp,
            )
        response = client.get(
            "/api/jobs/runs/search",
            params={"q": "unique-needle", "job": first["id"]},
        )
        assert response.status_code == 200
        matches = response.json()["matches"]
        assert len(matches) == 1
        assert matches[0]["job_id"] == first["id"]
        assert "unique-needle" in matches[0]["snippet"]


# ====================================================== bulk-cache schtasks


class TestBulkCacheNextRun:
    """`GET /api/jobs` must make at most one schtasks call per cache window
    regardless of job count — the issue-#66 perf prerequisite. The whole
    Jobs tab v1 was N+1 fork+exec on Windows.
    """

    def test_one_schtasks_call_per_window_for_n_jobs(
        self, webapp_client, monkeypatch
    ):
        # We don't want the router-level query_next_run mock here — we
        # want to exercise the real src.jobs query_next_run via the
        # cache. The fixture above mocks `query_next_run` directly on
        # jobs_mod, so re-import the *un*-mocked routine.
        from unittest.mock import MagicMock

        from app.webapp.routers import jobs as jobs_router
        from src import jobs as jobs_mod
        from src.jobs import schtasks as jobs_schtasks_mod

        client, _, _ = webapp_client

        # Stub only the schtasks side-effects (sync/delete/spawn) and
        # the stats helpers. Leave query_next_run pointing at the real
        # cached implementation.
        monkeypatch.setattr(jobs_router.jobs_mod, "sync_schtasks", MagicMock(return_value=[]))
        monkeypatch.setattr(jobs_router.jobs_mod, "delete_schtasks", MagicMock(return_value=[]))
        monkeypatch.setattr(
            jobs_router.jobs_mod,
            "run_stats",
            MagicMock(
                return_value={
                    "p50": None, "p95": None, "success_rate_30d": None,
                    "completed_count": 0, "last7": [],
                }
            ),
        )
        monkeypatch.setattr(jobs_router.jobs_mod, "is_stuck", MagicMock(return_value=False))

        # Reset the module-level cache so this test starts fresh.
        jobs_mod.invalidate_next_run_cache()

        # Count schtasks invocations.
        calls: List[List[str]] = []

        def fake_run(argv):
            import subprocess
            calls.append(list(argv))
            # Return one fake task per registered job so the cache
            # actually has entries to look up.
            stdout = (
                "TaskName: \\AppLauncher\\demo-0\nNext Run Time: 2026-06-01 06:00:00\n\n"
                "TaskName: \\AppLauncher\\demo-1\nNext Run Time: 2026-06-01 06:00:00\n\n"
                "TaskName: \\AppLauncher\\demo-2\nNext Run Time: 2026-06-01 06:00:00\n\n"
                "TaskName: \\AppLauncher\\demo-3\nNext Run Time: 2026-06-01 06:00:00\n\n"
                "TaskName: \\AppLauncher\\demo-4\nNext Run Time: 2026-06-01 06:00:00\n\n"
            )
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=stdout, stderr="")

        # _run_schtasks is owned by src.jobs.schtasks (issue #315 split) —
        # query_next_run's cache reads it as its own module global.
        monkeypatch.setattr(jobs_schtasks_mod, "_run_schtasks", fake_run)

        # Seed N=5 jobs.
        for i in range(5):
            client.post(
                "/api/jobs",
                json={"name": f"demo-{i}", "script_path": _stub_path(f"d{i}.bat")},
            )
        # Drop any schtasks calls made by create (those go through
        # sync_schtasks which is mocked).
        calls.clear()
        # Cache might have been populated/dirtied by a sync; reset.
        jobs_mod.invalidate_next_run_cache()

        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        assert len(resp.json()["jobs"]) == 5
        # First call populates the cache → exactly one schtasks shell-out
        # for all five jobs.
        first_window = list(calls)
        assert len(first_window) == 1
        # A second call within the TTL must not hit schtasks again.
        client.get("/api/jobs")
        assert calls == first_window

    def test_invalidation_after_sync(self, monkeypatch):

        from src import jobs as jobs_mod
        from src.jobs import schtasks as jobs_schtasks_mod

        # Set up a fake runner that records calls.
        calls = []

        def fake_run(argv):
            import subprocess
            calls.append(list(argv))
            # A working query: an empty answer reads as a failed one and
            # falls back to the whole-machine query, a second call (#1516).
            stdout = "TaskName: \\AppLauncher\\demo\nNext Run Time: N/A\n"
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=stdout, stderr="")

        # _run_schtasks is owned by src.jobs.schtasks (issue #315 split).
        monkeypatch.setattr(jobs_schtasks_mod, "_run_schtasks", fake_run)
        jobs_mod.invalidate_next_run_cache()

        # Warm the cache.
        jobs_mod.query_next_run("demo")
        assert len(calls) == 1
        # Second call within TTL → still 1.
        jobs_mod.query_next_run("demo")
        assert len(calls) == 1
        # Invalidate (what sync_schtasks does) → next call re-shells.
        jobs_mod.invalidate_next_run_cache()
        jobs_mod.query_next_run("demo")
        assert len(calls) == 2


# ============================================================= kill route


class TestKillRun:
    def test_404_on_unknown_job(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.post("/api/jobs/nope/runs/some-rid/kill")
        assert resp.status_code == 404

    def test_404_on_unknown_run(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        resp = client.post(
            "/api/jobs/" + created["id"] + "/runs/no-such-run/kill"
        )
        assert resp.status_code == 404

    def test_409_when_run_already_final(
        self, webapp_client, mocked_jobs_side_effects, monkeypatch
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        # Pre-create a run dir with status=success — not killable.
        from src import jobs as jobs_mod
        run_dir = jobs_mod.new_run_dir(created["id"], "20260524T080000")
        jobs_mod.write_run_json(
            run_dir,
            run_id=run_dir.name,
            status="success",
            started_at="2026-05-24T08:00:00",
            finished_at="2026-05-24T08:00:05",
        )
        resp = client.post(
            "/api/jobs/" + created["id"] + "/runs/" + run_dir.name + "/kill"
        )
        assert resp.status_code == 409

    def test_kills_running_and_finalises(
        self, webapp_client, mocked_jobs_side_effects, monkeypatch
    ):
        from unittest.mock import MagicMock

        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        from src import jobs as jobs_mod
        run_dir = jobs_mod.new_run_dir(created["id"], "20260524T080000")
        jobs_mod.write_run_json(
            run_dir,
            run_id=run_dir.name,
            status="running",
            started_at="2026-05-24T08:00:00",
            pid=4242,
        )

        kill_spy = MagicMock(return_value=[4242, 4243])
        from app.webapp.routers import jobs as jobs_router
        monkeypatch.setattr(jobs_router, "kill_process_tree", kill_spy)

        resp = client.post(
            "/api/jobs/" + created["id"] + "/runs/" + run_dir.name + "/kill"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["signalled"] == [4242, 4243]
        kill_spy.assert_called_once_with(4242, 5.0)

        # The run record was finalised with the kill markers.
        record = jobs_mod.read_run(run_dir)
        assert record["status"] == "failed"
        assert record["exit_code"] == -9
        assert record["killed"] is True
        assert record.get("finished_at")


# ==================================================== reap on poll (issue #591)


class TestReapOnPoll:
    """A run stranded "running" by a dead executor self-heals on the very
    next GET /api/jobs poll — no human needs to notice and tap Kill."""

    @pytest.mark.parametrize(
        ("pid_alive", "running", "status"),
        [(False, False, "failed"), (True, True, "running")],
        ids=["stranded-run-reaped", "live-run-left-running"],
    )
    def test_get_jobs_reaps_only_a_stranded_run(
        self, webapp_client, mocked_jobs_side_effects, monkeypatch,
        pid_alive, running, status,
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        from src import jobs as jobs_mod
        from src.jobs import reap as jobs_reap_mod

        run_dir = jobs_mod.new_run_dir(created["id"], "20260524T080000")
        jobs_mod.write_run_json(
            run_dir,
            run_id=run_dir.name,
            status="running",
            started_at="2026-05-24T08:00:00",
            pid=4242,
        )
        monkeypatch.setattr(jobs_reap_mod, "is_pid_alive", lambda *a, **k: pid_alive)

        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        job_payload = resp.json()["jobs"][0]
        assert job_payload["running"] is running
        assert job_payload["last_run"]["status"] == status

        record = jobs_mod.read_run(run_dir)
        assert record["status"] == status
        assert record.get("reaped", False) is (not pid_alive)

    def test_last_run_carries_the_unconfirmed_outcome(
        self, webapp_client, mocked_jobs_side_effects
    ):
        """Issue #916: the row payload must carry the *derived* outcome, not
        just the persisted status, or the client has nothing to render the
        third state from. The two are deliberately both present: ``status``
        still says what was written to disk."""
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        from src import jobs as jobs_mod

        run_dir = jobs_mod.new_run_dir(created["id"], "20260524T080000")
        jobs_mod.write_run_json(
            run_dir,
            run_id=run_dir.name,
            status="failed",
            started_at="2026-05-24T08:00:00",
            finished_at="2026-05-24T08:05:00",
            exit_code=122,
        )

        last_run = client.get("/api/jobs").json()["jobs"][0]["last_run"]
        assert last_run["status"] == "failed"
        assert last_run["outcome"] == "unconfirmed"
        assert "never verified" in last_run["outcome_reason"]

    def test_last_run_keeps_a_genuine_failure_failed(
        self, webapp_client, mocked_jobs_side_effects
    ):
        """The over-correction guard: 123 stays a failure, and names itself."""
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        from src import jobs as jobs_mod

        run_dir = jobs_mod.new_run_dir(created["id"], "20260524T080000")
        jobs_mod.write_run_json(
            run_dir,
            run_id=run_dir.name,
            status="failed",
            started_at="2026-05-24T08:00:00",
            finished_at="2026-05-24T08:05:00",
            exit_code=123,
        )

        last_run = client.get("/api/jobs").json()["jobs"][0]["last_run"]
        assert last_run["outcome"] == "failed"
        assert "delivered no work" in last_run["outcome_reason"]

    def test_last_run_with_unfinished_work_is_unconfirmed(
        self, webapp_client, mocked_jobs_side_effects
    ):
        """#959: exit 118 is work still running when the provider exited — the
        weekly-recap-draft runs that delivered and still read as a failed
        delivery assertion."""
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        from src import jobs as jobs_mod

        run_dir = jobs_mod.new_run_dir(created["id"], "20260913T220007")
        jobs_mod.write_run_json(
            run_dir,
            run_id=run_dir.name,
            status="failed",
            started_at="2026-09-13T22:00:07",
            finished_at="2026-09-13T22:09:00",
            exit_code=118,
        )

        last_run = client.get("/api/jobs").json()["jobs"][0]["last_run"]
        assert last_run["outcome"] == "unconfirmed"
        assert "delivery assertion" not in last_run["outcome_reason"]
        assert "unfinished" in last_run["outcome_reason"]

    def test_run_history_rows_carry_their_outcome(
        self, webapp_client, mocked_jobs_side_effects
    ):
        """The expanded history list renders per run, so every row needs the
        classification too — not only the newest one on the collapsed row."""
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        from src import jobs as jobs_mod

        for run_id, code in (("20260524T080000", 123), ("20260524T090000", 122)):
            rd = jobs_mod.new_run_dir(created["id"], run_id)
            jobs_mod.write_run_json(
                rd, run_id=run_id, status="failed",
                started_at="2026-05-24T08:00:00", exit_code=code,
            )

        runs = client.get(f"/api/jobs/{created['id']}/runs").json()["runs"]
        assert {r["run_id"]: r["outcome"] for r in runs} == {
            "20260524T080000": "failed",
            "20260524T090000": "unconfirmed",
        }
