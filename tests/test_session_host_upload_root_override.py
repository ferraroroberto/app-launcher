"""`LAUNCHER_UPLOAD_ROOT` relocates where uploaded files land (issue #922).

Same defect as #913, a different directory and a different writer:
`app/session_host/server.py::_save_image` resolved `<project>/.launcher-tmp`
from the session's own project dir, and the e2e autoboot runs its disposable
session-host with `project_dir` pointing at this checkout — so every
compose-bar attach test wrote a real file into the checkout's own
`.launcher-tmp`. Measured there before the fix: 3,423 of its 3,600 files were
the harness's, with nothing pruning them and nothing redirecting them.

These pin the override itself. The harness half — that the autoboot fixture
actually injects the variable into the session-host it spawns — is pinned by
the upload breach check in `tests/e2e/conftest.py::_autoboot_server`'s
teardown.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.session_host import server as sh_server

# The env var spelled as a literal rather than read off the module. The two
# route-level tests below have to fail on the *behaviour* when run against
# pre-fix code: an `AttributeError` on a constant that doesn't exist yet
# proves nothing about where the file lands. The name is pinned back to the
# module's constant by its own test, so the two can't drift apart.
_ENV = "LAUNCHER_UPLOAD_ROOT"


@pytest.fixture()
def project_dir(tmp_path: Path) -> Path:
    """A stand-in for the directory a real session is working in."""
    target = tmp_path / "project"
    target.mkdir()
    return target


@pytest.fixture()
def client(project_dir: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    session = SimpleNamespace(project_dir=str(project_dir), write=lambda data: None)
    monkeypatch.setattr(sh_server.manager, "get", lambda sid: session)
    return TestClient(sh_server.app)


def test_defaults_to_the_session_project_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real, non-test path: beside the code the agent is working on."""
    monkeypatch.delenv(sh_server.UPLOAD_ROOT_ENV, raising=False)

    assert sh_server._upload_dir(r"C:\code\myproj") == Path(
        r"C:\code\myproj"
    ) / ".launcher-tmp"


def test_env_relocates_the_root_and_keeps_the_leaf(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(sh_server.UPLOAD_ROOT_ENV, str(tmp_path))

    # The root moves; `.launcher-tmp` stays, so a redirected upload is still
    # recognisably one (the compose-bar e2e assertions pin the leaf).
    assert sh_server._upload_dir(r"C:\code\myproj") == tmp_path / ".launcher-tmp"


def test_blank_env_falls_back_rather_than_writing_off_the_cwd(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty or whitespace value is "unset", not `Path("")` (= CWD)."""
    monkeypatch.setenv(sh_server.UPLOAD_ROOT_ENV, "   ")

    assert sh_server._upload_dir(r"C:\code\myproj") == Path(
        r"C:\code\myproj"
    ) / ".launcher-tmp"


def test_the_module_owns_this_env_var_name() -> None:
    assert sh_server.UPLOAD_ROOT_ENV == _ENV


def test_a_real_upload_lands_in_the_override_not_the_project(
    client: TestClient,
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The whole point, exercised through the route rather than the helper."""
    override = tmp_path / "upload-root"
    monkeypatch.setenv(_ENV, str(override))

    resp = client.post(
        "/sessions/x/image?inline=1",
        files={"file": ("e2e-stub-shot.png", b"\x89PNG fake", "image/png")},
    )

    assert resp.status_code == 200, resp.text
    written = Path(resp.json()["path"])
    assert written.is_file(), "the override root got no file"
    assert written.parent == override / ".launcher-tmp"
    # The session's own project dir was never touched.
    assert not (project_dir / ".launcher-tmp").exists()


def test_without_the_override_the_upload_lands_in_the_project(
    client: TestClient, project_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction: the override is opt-in, not a behaviour change."""
    monkeypatch.delenv(_ENV, raising=False)

    resp = client.post(
        "/sessions/x/image?inline=1",
        files={"file": ("shot.png", b"\x89PNG fake", "image/png")},
    )

    assert resp.status_code == 200, resp.text
    written = Path(resp.json()["path"])
    assert written.parent == project_dir / ".launcher-tmp"
    assert written.is_file()
