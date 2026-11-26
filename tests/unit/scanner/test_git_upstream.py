"""src.git_upstream: ahead/behind against origin, and Sync all's safe fast-forward.

Every case runs against real repos in tmp_path: a bare ``origin`` and a
clone of it, with a second clone standing in for whoever else pushes.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Tuple

import pytest

from src import git_upstream
from src.subprocess_flags import NO_WINDOW

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, creationflags=NO_WINDOW, timeout=30,
    ).stdout.strip()


def _commit(repo: Path, name: str, text: str = "x\n") -> None:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    # --no-verify: the dev box's global pre-commit hook must not judge a throwaway repo.
    _git(repo, "commit", "--no-verify", "-q", "-m", name)


def _clone(origin: Path, dest: Path) -> Path:
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(dest)],
        check=True, capture_output=True, creationflags=NO_WINDOW, timeout=30,
    )
    _git(dest, "config", "user.email", "t@example.com")
    _git(dest, "config", "user.name", "Test")
    return dest


@pytest.fixture(autouse=True)
def _fresh_ledgers(monkeypatch):
    """Each test starts as a fresh process does: no fetch or pull recorded."""
    monkeypatch.setattr(git_upstream, "_fetches", {})
    monkeypatch.setattr(git_upstream, "_pulled", {})


@pytest.fixture
def repos(tmp_path: Path) -> Tuple[Path, Path, Path]:
    """(origin, mine, theirs): a bare origin on ``main`` and two clones of it."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "Test")
    _commit(seed, "README.md")
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(seed), str(origin)],
        check=True, capture_output=True, creationflags=NO_WINDOW, timeout=30,
    )
    mine = _clone(origin, tmp_path / "mine")
    theirs = _clone(origin, tmp_path / "theirs")
    return origin, mine, theirs


def _push_from(theirs: Path, name: str) -> None:
    _commit(theirs, name)
    _git(theirs, "push", "-q", "origin", "main")


# ------------------------------------------------------------ upstream state


def test_state_is_unchecked_until_a_fetch_and_behind_after(repos):
    _, mine, theirs = repos
    _push_from(theirs, "a.txt")
    assert git_upstream.upstream_state(mine, "main").state == git_upstream.UNCHECKED
    assert git_upstream.fetch(mine).ok
    state = git_upstream.upstream_state(mine, "main")
    assert (state.state, state.behind, state.ahead) == (git_upstream.BEHIND, 1, 0)


def test_up_to_date_only_after_a_good_fetch(repos):
    _, mine, _ = repos
    assert git_upstream.fetch(mine).ok
    assert git_upstream.upstream_state(mine, "main").state == git_upstream.UP_TO_DATE


def test_ahead_and_diverged(repos):
    _, mine, theirs = repos
    _commit(mine, "mine.txt")
    git_upstream.fetch(mine)
    assert git_upstream.upstream_state(mine, "main").state == git_upstream.AHEAD
    _push_from(theirs, "theirs.txt")
    git_upstream.fetch(mine)
    state = git_upstream.upstream_state(mine, "main")
    assert (state.state, state.ahead, state.behind) == (git_upstream.DIVERGED, 1, 1)


def test_a_failed_fetch_reads_unknown_never_up_to_date(repos, tmp_path):
    _, mine, _ = repos
    _git(mine, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    record = git_upstream.fetch(mine)
    assert not record.ok and record.error
    state = git_upstream.upstream_state(mine, "main")
    assert state.state == git_upstream.UNKNOWN
    assert state.reason.startswith("fetch failed")


def test_a_repo_with_no_origin_reads_no_remote(tmp_path):
    repo = tmp_path / "solo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    _commit(repo, "README.md")
    assert git_upstream.upstream_state(repo, "main").state == git_upstream.NO_REMOTE
    assert git_upstream.sync_repo(repo, "main").reason == "no remote"


# ----------------------------------------------------------------- sync_repo


def test_sync_fast_forwards_a_clean_behind_repo(repos):
    _, mine, theirs = repos
    _push_from(theirs, "a.txt")
    _push_from(theirs, "b.txt")
    outcome = git_upstream.sync_repo(mine, "main")
    assert (outcome.outcome, outcome.commits) == (git_upstream.PULLED, 2)
    assert _git(mine, "rev-parse", "HEAD") == _git(theirs, "rev-parse", "HEAD")
    assert git_upstream.was_pulled(mine)
    assert git_upstream.upstream_state(mine, "main").state == git_upstream.UP_TO_DATE


def test_sync_reports_an_up_to_date_repo(repos):
    _, mine, _ = repos
    assert git_upstream.sync_repo(mine, "main").outcome == git_upstream.CURRENT
    assert not git_upstream.was_pulled(mine)


@pytest.mark.parametrize("dirt", ["modified", "untracked"])
def test_sync_skips_a_dirty_repo_and_leaves_it_alone(repos, dirt):
    _, mine, theirs = repos
    _push_from(theirs, "a.txt")
    if dirt == "modified":
        (mine / "README.md").write_text("edited\n", encoding="utf-8")
    else:
        (mine / "new.txt").write_text("new\n", encoding="utf-8")
    head = _git(mine, "rev-parse", "HEAD")
    outcome = git_upstream.sync_repo(mine, "main")
    assert (outcome.outcome, outcome.reason) == (git_upstream.SKIPPED, "dirty")
    assert _git(mine, "rev-parse", "HEAD") == head
    assert _git(mine, "stash", "list") == ""


def test_sync_skips_a_repo_off_its_default_branch(repos):
    _, mine, theirs = repos
    _push_from(theirs, "a.txt")
    _git(mine, "checkout", "-q", "-b", "feature")
    outcome = git_upstream.sync_repo(mine, "main")
    assert (outcome.outcome, outcome.reason) == (git_upstream.SKIPPED, "not on main")
    assert _git(mine, "rev-parse", "main") != _git(theirs, "rev-parse", "HEAD")


def test_sync_skips_an_ahead_repo(repos):
    _, mine, _ = repos
    _commit(mine, "mine.txt")
    head = _git(mine, "rev-parse", "HEAD")
    outcome = git_upstream.sync_repo(mine, "main")
    assert (outcome.outcome, outcome.reason) == (git_upstream.SKIPPED, "ahead")
    assert _git(mine, "rev-parse", "HEAD") == head


def test_sync_skips_a_diverged_repo(repos):
    _, mine, theirs = repos
    _commit(mine, "mine.txt")
    _push_from(theirs, "theirs.txt")
    head = _git(mine, "rev-parse", "HEAD")
    outcome = git_upstream.sync_repo(mine, "main")
    assert (outcome.outcome, outcome.reason) == (git_upstream.SKIPPED, "diverged")
    assert _git(mine, "rev-parse", "HEAD") == head


def test_sync_skips_when_the_fetch_fails(repos, tmp_path):
    _, mine, _ = repos
    _git(mine, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    outcome = git_upstream.sync_repo(mine, "main")
    assert (outcome.outcome, outcome.reason) == (git_upstream.SKIPPED, "fetch failed")


def test_sync_keeps_a_live_file_the_incoming_range_untracks(repos):
    """A merge that untracks a config (rm --cached + .gitignore) must not delete the live copy."""
    _, mine, theirs = repos
    _commit(theirs, "settings.json", '{"live": false}\n')
    _git(theirs, "push", "-q", "origin", "main")
    assert git_upstream.sync_repo(mine, "main").outcome == git_upstream.PULLED
    _git(theirs, "rm", "-q", "--cached", "settings.json")
    (theirs / ".gitignore").write_text("settings.json\n", encoding="utf-8")
    _git(theirs, "add", ".gitignore")
    _git(theirs, "commit", "--no-verify", "-q", "-m", "untrack settings")
    _git(theirs, "push", "-q", "origin", "main")

    outcome = git_upstream.sync_repo(mine, "main")
    assert outcome.outcome == git_upstream.PULLED
    assert outcome.restored == ["settings.json"]
    assert (mine / "settings.json").read_text(encoding="utf-8") == '{"live": false}\n'


def test_launch_refusal_names_how_far_behind(repos):
    _, mine, theirs = repos
    assert git_upstream.launch_refusal(mine, "main") is None
    _push_from(theirs, "a.txt")
    git_upstream.fetch(mine)
    assert git_upstream.launch_refusal(mine, "main") == "main is 1 commit behind origin/main: pull first"


def test_parse_removed_paths_takes_the_old_side_of_a_rename():
    stream = "D\0gone.txt\0R100\0old.json\0new.json\0M\0kept.txt\0"
    assert git_upstream.parse_removed_paths(stream) == ["gone.txt", "old.json"]


# ------------------------------------------------------------------- routes


def _point_projects_at(app, folder: Path) -> None:
    app.state.webapp_config.projects_dir = str(folder)
    app.state.webapp_config.projects_ignore = []


@pytest.fixture
def live_client(webapp_client):
    """The webapp client with its event loop kept alive between requests.

    Sync all runs as a background task; a bare TestClient tears its loop down
    after each request, which would cancel it before the next poll.
    """
    client, app, overrides = webapp_client
    with client:
        yield client, app, overrides


def _finished_run(client, timeout_s: float = 30.0) -> dict:
    """Poll Sync all's progress until the run ends, as the dialog does."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        run = client.get("/api/claude-code/sync-all").json()["run"]
        if run and run["finished_at"]:
            return run
        time.sleep(0.05)
    raise AssertionError("Sync all did not finish")


def _two_projects(app, repos, tmp_path):
    _, mine, theirs = repos
    projects = tmp_path / "projects"
    projects.mkdir()
    shutil.move(str(mine), str(projects / "mine"))
    shutil.move(str(theirs), str(projects / "theirs"))
    _point_projects_at(app, projects)


def test_sync_all_route_reports_every_repo(live_client, repos, tmp_path):
    client, app, _ = live_client
    _, _, theirs = repos
    _push_from(theirs, "a.txt")
    (theirs / "local.txt").write_text("dirt\n", encoding="utf-8")
    _two_projects(app, repos, tmp_path)

    started = client.post("/api/claude-code/sync-all")
    assert started.status_code == 200
    assert started.json()["run"]["total"] == 2

    run = _finished_run(client)
    assert (run["total"], run["done"], run["current"], run["error"]) == (2, 2, None, None)
    by_name = {r["name"]: r for r in run["results"]}
    assert by_name["mine"]["outcome"] == "pulled" and by_name["mine"]["commits"] == 1
    assert (by_name["theirs"]["outcome"], by_name["theirs"]["reason"]) == ("skipped", "dirty")

    rows = {p["id"]: p for p in client.get("/api/claude-code/git-status").json()["projects"]}
    assert rows["mine"]["upstream"]["state"] == "up-to-date"


def test_sync_all_reports_a_repo_that_raised_as_failed_with_its_error(live_client, repos, tmp_path, monkeypatch):
    client, app, _ = live_client
    _two_projects(app, repos, tmp_path)
    real = git_upstream.sync_repo

    def boom(repo, default_branch, **kwargs):
        if Path(repo).name == "theirs":
            raise OSError("disk exploded")
        return real(repo, default_branch, **kwargs)

    monkeypatch.setattr(git_upstream, "sync_repo", boom)
    assert client.post("/api/claude-code/sync-all").status_code == 200

    run = _finished_run(client)
    by_name = {r["name"]: r for r in run["results"]}
    assert (by_name["theirs"]["outcome"], by_name["theirs"]["reason"]) == ("failed", "OSError: disk exploded")
    assert by_name["mine"]["outcome"] == "up-to-date"
    assert run["error"] is None and run["done"] == 2


def test_sync_all_refuses_a_second_run_while_one_is_going(live_client, repos, tmp_path, monkeypatch):
    client, app, _ = live_client
    _two_projects(app, repos, tmp_path)
    release = threading.Event()
    real = git_upstream.sync_repo

    def slow(repo, default_branch, **kwargs):
        release.wait(10)
        return real(repo, default_branch, **kwargs)

    monkeypatch.setattr(git_upstream, "sync_repo", slow)
    try:
        assert client.post("/api/claude-code/sync-all").status_code == 200
        running = client.get("/api/claude-code/sync-all").json()["run"]
        assert running["finished_at"] is None and running["done"] == 0 and running["current"]
        assert client.post("/api/claude-code/sync-all").status_code == 409
    finally:
        release.set()
    assert _finished_run(client)["done"] == 2


def test_git_status_poll_does_no_network_io(webapp_client, repos, tmp_path, monkeypatch):
    client, app, _ = webapp_client
    _, mine, _ = repos
    projects = tmp_path / "projects"
    projects.mkdir()
    shutil.move(str(mine), str(projects / "mine"))
    _point_projects_at(app, projects)

    def no_fetch(*_a, **_k):
        raise AssertionError("the git-status poll must never fetch")

    monkeypatch.setattr(git_upstream, "fetch", no_fetch)
    rows = client.get("/api/claude-code/git-status?fresh=true").json()["projects"]
    assert rows[0]["upstream"]["state"] == "unchecked"


def test_launch_is_refused_on_a_behind_repo(webapp_client, repos, tmp_path):
    client, app, _ = webapp_client
    _, mine, theirs = repos
    _push_from(theirs, "a.txt")
    git_upstream.fetch(mine)
    projects = tmp_path / "projects"
    projects.mkdir()
    shutil.move(str(mine), str(projects / "mine"))
    _point_projects_at(app, projects)

    res = client.post("/api/apps/mine/launch", json={"agent": "claude", "mode": "remote"})
    assert res.status_code == 409
    assert "behind origin/main: pull first" in res.json()["detail"]


def test_gate_real_agent_launch_gets_through_a_behind_checkout(
    webapp_client, repos, tmp_path, monkeypatch
):
    """The gate's real-agent fixture launches in the primary checkout, whose
    ``main`` lags as soon as another lane merges. That is not this lane's tree,
    so the launch must not be refused for it."""
    from app.webapp.routers import apps as apps_router
    from tests.e2e.fixtures import sessions

    client, app, _ = webapp_client
    _, mine, theirs = repos
    _push_from(theirs, "a.txt")
    git_upstream.fetch(mine)
    projects = tmp_path / "projects"
    projects.mkdir()
    shutil.move(str(mine), str(projects / "mine"))
    _point_projects_at(app, projects)

    monkeypatch.setattr(
        apps_router, "spawn_claude_session",
        lambda *_a, **_k: {"session_id": "s1", "kind": "pty", "agent": "claude"},
    )
    monkeypatch.setattr(sessions, "shutil", SimpleNamespace(which=lambda _name: "claude"))
    monkeypatch.setattr(sessions, "require_trusted_launch_target", lambda: projects / "mine")
    monkeypatch.setattr(sessions, "requests", SimpleNamespace(
        post=lambda url, json, headers, **_k: client.post(url, json=json, headers=headers),
    ))

    # autoboot=True: the gate's mode, where a refusal fails instead of skipping.
    assert sessions._launch_claude_via_webapp("", "", autoboot=True) == "s1"
