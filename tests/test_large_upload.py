"""Large-file attach (issue #1430): a file put on the machine, not in the context.

Two halves. The session-host's ``POST /sessions/{sid}/large-file`` streams a
raw body to ``.launcher-tmp/large/<sid>/`` under a caller-given byte limit,
and session end plus a TTL prune clean up after it (and, since nothing ever
pruned them before, after ordinary attachments too). The webapp's
``POST /api/claude-code/sessions/{sid}/large-file`` keeps the upload security
posture of ``/image`` (Tailscale-only, refused over the public tunnel,
audited), enforces ``large_upload_max_mb`` and relays the body as a stream.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.session_host import server as sh_server
from src import session_client as real_session_client

_SID = "abc123"
_DAY = 24 * 3600


# --------------------------------------------------------------- session-host


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv(sh_server.UPLOAD_ROOT_ENV, raising=False)
    target = tmp_path / "project"
    target.mkdir()
    return target


@pytest.fixture()
def host(project: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    session = SimpleNamespace(project_dir=str(project), write=lambda data: None)
    monkeypatch.setattr(sh_server.manager, "get", lambda sid: session)
    return TestClient(sh_server.app)


def _large_dir(project: Path) -> Path:
    return project / ".launcher-tmp" / "large" / _SID


def test_streams_the_body_into_the_session_leaf(host: TestClient, project: Path) -> None:
    body = os.urandom(3 * 1024 * 1024 + 17)  # several flushes plus a tail
    resp = host.post(
        f"/sessions/{_SID}/large-file",
        params={"name": "scans.zip", "max_bytes": 10 * 1024 * 1024},
        content=body,
    )
    assert resp.status_code == 200, resp.text
    out = Path(resp.json()["path"])
    assert resp.json()["bytes"] == len(body)
    assert out.parent == _large_dir(project)
    assert out.name.endswith("-scans.zip")
    assert out.read_bytes() == body


def test_chunked_body_streams_too(host: TestClient) -> None:
    """The webapp relays with no Content-Length (chunked): the limit still holds."""
    def gen():
        for _ in range(4):
            yield b"x" * 1000

    ok = host.post(
        f"/sessions/{_SID}/large-file", params={"name": "a.bin", "max_bytes": 4000},
        content=gen(),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["bytes"] == 4000


def test_over_the_limit_is_refused_and_leaves_no_file(host: TestClient, project: Path) -> None:
    def gen():
        for _ in range(5):
            yield b"x" * 1000

    resp = host.post(
        f"/sessions/{_SID}/large-file", params={"name": "big.bin", "max_bytes": 3000},
        content=gen(),
    )
    assert resp.status_code == 413
    assert not list(_large_dir(project).glob("*"))


def test_declared_length_over_the_limit_is_refused_up_front(host: TestClient, project: Path) -> None:
    resp = host.post(
        f"/sessions/{_SID}/large-file", params={"name": "big.bin", "max_bytes": 10},
        content=b"x" * 11,
    )
    assert resp.status_code == 413
    assert not _large_dir(project).exists()


def test_empty_body_is_refused_and_leaves_no_file(host: TestClient, project: Path) -> None:
    resp = host.post(
        f"/sessions/{_SID}/large-file", params={"name": "e.bin", "max_bytes": 10},
        content=b"",
    )
    assert resp.status_code == 400
    assert not list(_large_dir(project).glob("*"))


def test_name_is_sanitised_like_an_ordinary_upload(host: TestClient, project: Path) -> None:
    resp = host.post(
        f"/sessions/{_SID}/large-file",
        params={"name": "..\\..\\evil name.t x!t", "max_bytes": 100},
        content=b"data",
    )
    assert resp.status_code == 200, resp.text
    out = Path(resp.json()["path"])
    assert out.parent == _large_dir(project)
    assert out.name.endswith("-evil_name")


def test_unknown_session_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sh_server.manager, "get", lambda sid: None)
    resp = TestClient(sh_server.app).post(
        "/sessions/nope/large-file", params={"max_bytes": 10}, content=b"x"
    )
    assert resp.status_code == 404


def test_healthz_advertises_the_feature() -> None:
    body = TestClient(sh_server.app).get("/healthz").json()
    assert "large_upload" in body["features"]


def _aged(path: Path, seconds: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))
    return path


def test_ttl_prune_keeps_fresh_and_drops_expired(project: Path) -> None:
    root = project / ".launcher-tmp"
    old_attach = _aged(root / "old.png", 8 * _DAY)
    new_attach = _aged(root / "new.png", 6 * _DAY)
    future = _aged(root / "future.png", -_DAY)  # dated ahead: unknown, kept
    old_large = _aged(root / "large" / "s1" / "old.zip", 2 * _DAY)
    new_large = _aged(root / "large" / "s2" / "new.zip", 3600)
    agent_dir = root / "large" / "s3" / "extracted"
    agent_dir.mkdir(parents=True)
    _aged(root / "large" / "s3" / "old.zip", 2 * _DAY)

    removed = sh_server._prune_uploads(str(project))

    assert removed == 3
    assert not old_attach.exists() and not old_large.exists()
    assert new_attach.exists() and new_large.exists() and future.exists()
    assert not (root / "large" / "s1").exists()  # emptied leaf removed
    assert agent_dir.exists()  # never recurses into what the agent made


def test_session_end_drops_only_that_sessions_uploads(project: Path) -> None:
    root = project / ".launcher-tmp" / "large"
    mine = _aged(root / _SID / "mine.zip", 60)
    theirs = _aged(root / "other" / "theirs.zip", 60)
    attach = _aged(project / ".launcher-tmp" / "shot.png", 60)

    sh_server._cleanup_ended_sessions(
        [SimpleNamespace(session_id=_SID, project_dir=str(project))]
    )

    assert not mine.exists() and not (root / _SID).exists()
    assert theirs.exists() and attach.exists()


def test_session_end_leaves_what_the_agent_created(project: Path) -> None:
    leaf = project / ".launcher-tmp" / "large" / _SID
    upload = _aged(leaf / "scans.zip", 60)
    (leaf / "unzipped").mkdir()

    sh_server._drop_session_uploads(str(project), _SID)

    assert not upload.exists()
    assert (leaf / "unzipped").is_dir()


def test_reap_dead_hands_back_the_reaped_sessions() -> None:
    from src.session_host import SessionManager

    manager = SessionManager()
    dead = SimpleNamespace(alive=False, session_id="d", project_dir="p")
    live = SimpleNamespace(alive=True, session_id="l", project_dir="p")
    manager._sessions.update({"d": dead, "l": live})

    assert manager.reap_dead() == [dead]
    assert list(manager._sessions) == ["l"]


# --------------------------------------------------------------------- webapp

_TAILNET = ("100.64.1.2", 50000)
_ROUTE = f"/api/claude-code/sessions/{_SID}/large-file"


@pytest.fixture()
def webapp(webapp_client):
    _client, app, overrides = webapp_client
    session = overrides["session"]
    session.supports_large_upload.return_value = True
    received = {}

    async def fake_upload(port, sid, name, chunks, max_bytes):
        data = b""
        async for chunk in chunks:
            data += chunk
        received.update(sid=sid, name=name, data=data, max_bytes=max_bytes)
        return {"ok": True, "path": f"C:/p/.launcher-tmp/large/{sid}/x-{name}", "bytes": len(data)}

    session.upload_large_file = fake_upload
    return TestClient(app, client=_TAILNET), app, overrides, received


def test_webapp_relays_the_stream_and_audits(webapp) -> None:
    client, app, overrides, received = webapp
    resp = client.post(_ROUTE, params={"name": "scans.zip"}, content=b"payload")
    assert resp.status_code == 200, resp.text
    assert received["data"] == b"payload"
    assert received["name"] == "scans.zip"
    assert received["max_bytes"] == app.state.webapp_config.large_upload_max_mb * 1024 * 1024
    overrides["audit"].session_log.assert_any_call(
        _SID, "large_file", path=resp.json()["path"], bytes=7, client=_TAILNET[0]
    )


def test_webapp_quietly_ends_an_upload_the_client_dropped(webapp) -> None:
    """A cancelled upload is a 400 with no audit line, not an unhandled error."""
    from starlette.requests import ClientDisconnect

    client, _app, overrides, _received = webapp

    async def dropped(port, sid, name, chunks, max_bytes):
        raise ClientDisconnect()

    overrides["session"].upload_large_file = dropped
    resp = client.post(_ROUTE, params={"name": "a"}, content=b"x")
    assert resp.status_code == 400
    assert "interrupted" in resp.json()["detail"]
    assert not [
        c for c in overrides["audit"].session_log.call_args_list if c.args[1:2] == ("large_file",)
    ]


def test_webapp_refuses_the_public_tunnel(webapp) -> None:
    client, _app, overrides, received = webapp
    resp = client.post(_ROUTE, params={"name": "a"}, content=b"x", headers={"Cf-Ray": "1"})
    assert resp.status_code == 403
    assert not received


def test_webapp_refuses_off_tailnet(webapp) -> None:
    _client, app, _overrides, received = webapp
    resp = TestClient(app, client=("203.0.113.9", 5000)).post(
        _ROUTE, params={"name": "a"}, content=b"x"
    )
    assert resp.status_code == 403
    assert not received


def test_webapp_refuses_over_the_setting_before_relaying(webapp) -> None:
    client, app, _overrides, received = webapp
    app.state.webapp_config.large_upload_max_mb = 1
    resp = client.post(_ROUTE, params={"name": "a"}, content=b"x" * (1024 * 1024 + 1))
    assert resp.status_code == 413
    assert "limit 1 MB" in resp.json()["detail"]
    assert not received


def test_webapp_names_a_session_host_too_old_for_the_route(webapp) -> None:
    client, _app, overrides, received = webapp
    overrides["session"].supports_large_upload.return_value = False
    resp = client.post(_ROUTE, params={"name": "a"}, content=b"x")
    assert resp.status_code == 501
    assert "restart" in resp.json()["detail"]
    assert not received


def test_webapp_says_unreachable_when_the_session_host_is_down(webapp) -> None:
    client, _app, overrides, _received = webapp
    overrides["session"].supports_large_upload.return_value = None
    resp = client.post(_ROUTE, params={"name": "a"}, content=b"x")
    assert resp.status_code == 503


def test_large_upload_setting_round_trips_and_is_bounded(webapp_client) -> None:
    client, _app, _overrides = webapp_client
    assert client.get("/api/config").json()["large_upload_max_mb"] == 2048
    assert client.post("/api/config", json={"large_upload_max_mb": 500}).status_code == 200
    assert client.get("/api/config").json()["large_upload_max_mb"] == 500
    assert client.post("/api/config", json={"large_upload_max_mb": 0}).status_code == 400
    assert client.post("/api/config", json={"large_upload_max_mb": "9"}).status_code == 400


# ------------------------------------------------------- client <-> host, real


def test_client_streams_into_the_real_session_host(host: TestClient, project: Path, monkeypatch) -> None:
    """session_client.upload_large_file against the real session-host app."""
    real_client = httpx.AsyncClient

    def asgi_client(**kwargs):
        kwargs.pop("trust_env", None)
        return real_client(transport=httpx.ASGITransport(app=sh_server.app), **kwargs)

    monkeypatch.setattr(real_session_client.httpx, "AsyncClient", asgi_client)

    async def chunks():
        for i in range(3):
            yield bytes([i]) * 1000

    result = asyncio.run(
        real_session_client.upload_large_file(8446, _SID, "doc.pdf", chunks(), 10_000)
    )
    assert result["bytes"] == 3000
    assert Path(result["path"]).read_bytes() == b"\x00" * 1000 + b"\x01" * 1000 + b"\x02" * 1000

    with pytest.raises(real_session_client.SessionHostError) as err:
        asyncio.run(real_session_client.upload_large_file(8446, _SID, "x", chunks(), 10))
    assert err.value.status == 413
