"""The delegate token: Board reads and filing an issue pass, everything else is refused."""

from __future__ import annotations

import logging
import secrets

import pytest
from starlette.testclient import TestClient

from src import github_client
from src.auth import delegate_token

TAILNET = "100.101.102.103"
LOOPBACK = "127.0.0.1"


def _as(app, host: str) -> TestClient:
    return TestClient(app, client=(host, 50000))


def _bearer(value: str) -> dict:
    return {"Authorization": f"Bearer {value}"}


# ------------------------------------------------------------- library


class TestLibrary:
    def test_mint_stores_only_the_salted_hash(self):
        record, raw = delegate_token.mint()
        assert raw not in record.values()
        assert set(record) == {"salt", "hash", "created_at"}
        assert delegate_token.matches(raw, record)

    def test_wrong_empty_and_off_never_match(self):
        record, raw = delegate_token.mint()
        assert not delegate_token.matches("not-the-token", record)
        assert not delegate_token.matches("", record)
        assert not delegate_token.matches(raw, {})
        assert not delegate_token.matches(raw, None)

    def test_a_malformed_record_fails_closed(self):
        record, raw = delegate_token.mint()
        assert not delegate_token.matches(raw, {**record, "salt": "zz-not-hex"})
        assert not delegate_token.matches(raw, {"hash": record["hash"]})
        assert not delegate_token.enabled("a string")

    def test_allow_list_is_exact(self):
        assert delegate_token.allowed("GET", "/api/board")
        assert delegate_token.allowed("get", "/api/board/chief-plan")
        assert delegate_token.allowed("POST", "/api/board/issues/file")
        for method, path in (
            ("GET", "/api/board/"),
            ("POST", "/api/board"),
            ("GET", "/api/board/issues/file"),
            ("POST", "/api/board/issues/start"),
            ("POST", "/api/board/chief-plan/prune"),
            ("GET", "/api/board/github/refresh"),
        ):
            assert not delegate_token.allowed(method, path), (method, path)

    def test_public_dict_never_carries_the_hash(self):
        record, _ = delegate_token.mint()
        public = delegate_token.public_dict(record)
        assert public == {"enabled": True, "created_at": record["created_at"]}
        assert delegate_token.public_dict({}) == {"enabled": False, "created_at": ""}


# ------------------------------------------------------------- middleware


@pytest.fixture
def gated(webapp_client, monkeypatch):
    """The app with the main token and a delegate token both on, saved to the
    isolated config file so a regenerate keeps the main token. Yields
    ``(app, main, raw, filed)``; ``filed`` records each faked issue create."""
    from src.webapp_config import update_webapp_config

    _client, app, overrides = webapp_client
    (overrides["tmp_projects_dir"] / "myrepo").mkdir()
    main = secrets.token_urlsafe(16)
    record, raw = delegate_token.mint()
    app.state.webapp_config = update_webapp_config(
        auth_token=main, api_tokens=[], delegate_token=record
    )
    filed: list = []

    def fake_create(owner, repo, title, body):
        filed.append({"owner": owner, "repo": repo, "title": title, "body": body})
        return f"https://github.com/{owner}/{repo}/issues/1"

    monkeypatch.setattr(github_client, "create_issue", fake_create)
    yield app, main, raw, filed


class TestDelegateAllowed:
    def test_board_and_plan_reads_succeed(self, gated):
        app, _main, raw, _ = gated
        client = _as(app, TAILNET)
        assert client.get("/api/board", headers=_bearer(raw)).status_code == 200
        assert client.get("/api/board/chief-plan", headers=_bearer(raw)).status_code == 200

    def test_filing_an_issue_succeeds_and_says_who_filed_it(self, gated):
        app, _main, raw, filed = gated
        resp = _as(app, TAILNET).post(
            "/api/board/issues/file", headers=_bearer(raw),
            json={"repo": "MyRepo", "title": "Look at the flaky probe", "body": "Details."},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["repo"] == "myrepo"
        assert filed[0]["repo"] == "myrepo"
        assert filed[0]["body"].startswith("Details.")
        assert "delegate token" in filed[0]["body"]

    def test_the_query_parameter_form_works_too(self, gated):
        app, _main, raw, _ = gated
        assert _as(app, TAILNET).get(f"/api/board?token={raw}").status_code == 200


class TestDelegateRefused:
    @pytest.mark.parametrize("method,path,body", [
        ("POST", "/api/board/issues/start", {"repo": "myrepo", "number": 1, "mode": "start"}),
        ("POST", "/api/board/issues/start", {"repo": "myrepo", "number": 1, "mode": "yolo"}),
        ("POST", "/api/claude-code/sessions/abc/input", {"text": "hi"}),
        ("GET", "/api/claude-code/sessions/abc/transcript", None),
        ("POST", "/api/board/chief/ensure", {}),
        ("POST", "/api/board/chief-plan/prune", {}),
        ("GET", "/api/config", None),
        ("POST", "/api/config", {"port": 1}),
        ("GET", "/api/tokens", None),
        ("POST", "/api/delegate-token", None),
        ("DELETE", "/api/delegate-token", None),
        ("POST", "/api/apps/x/start", None),
        ("GET", "/api/board/", None),
        ("GET", "/", None),
    ])
    def test_everything_off_the_list_is_403_even_on_the_tailnet(self, gated, method, path, body):
        app, _main, raw, filed = gated
        resp = _as(app, TAILNET).request(method, path, headers=_bearer(raw), json=body)
        assert resp.status_code == 403, (method, path, resp.text)
        assert resp.json()["detail"] == delegate_token.REFUSAL
        assert filed == []

    def test_off_or_wrong_is_the_ordinary_401(self, gated):
        app, main, raw, _ = gated
        client = _as(app, TAILNET)
        assert client.get("/api/board", headers=_bearer("wrong-value")).status_code == 401
        app.state.webapp_config.delegate_token = {}
        assert client.get("/api/board", headers=_bearer(raw)).status_code == 401
        assert client.get("/api/board", headers=_bearer(main)).status_code == 200


class TestUnchanged:
    def test_main_token_keeps_full_access(self, gated):
        app, main, _raw, _ = gated
        client = _as(app, TAILNET)
        assert client.get("/api/config", headers=_bearer(main)).status_code == 200
        assert client.get("/api/delegate-token", headers=_bearer(main)).status_code == 200

    def test_loopback_keeps_its_bypass_even_carrying_the_delegate_token(self, gated):
        app, _main, raw, _ = gated
        client = _as(app, LOOPBACK)
        assert client.get("/api/config", headers=_bearer(raw)).status_code == 200
        assert client.get("/api/config").status_code == 200


class TestSettingsControl:
    def test_regenerate_invalidates_the_old_value_at_once(self, gated):
        app, main, old, _ = gated
        client = _as(app, TAILNET)
        resp = client.post("/api/delegate-token", headers=_bearer(main))
        assert resp.status_code == 200
        new = resp.json()["token"]
        assert new and new != old and resp.json()["enabled"] is True
        assert client.get("/api/board", headers=_bearer(old)).status_code == 401
        assert client.get("/api/board", headers=_bearer(new)).status_code == 200
        # The hash persists; the raw value never does.
        from src.webapp_config import load_webapp_config
        on_disk = load_webapp_config().delegate_token
        assert delegate_token.matches(new, on_disk) and new not in on_disk.values()

    def test_turn_off(self, gated):
        app, main, raw, _ = gated
        client = _as(app, TAILNET)
        resp = client.delete("/api/delegate-token", headers=_bearer(main))
        assert resp.json() == {"enabled": False, "created_at": ""}
        assert client.get("/api/board", headers=_bearer(raw)).status_code == 401
        assert client.get("/api/delegate-token", headers=_bearer(main)).json()["enabled"] is False


class TestAudit:
    def test_every_request_is_logged_without_the_token(self, gated, caplog):
        app, _main, raw, _ = gated
        client = _as(app, TAILNET)
        with caplog.at_level(logging.INFO, logger="launcher.auth"):
            client.get("/api/board", headers=_bearer(raw))
            client.get("/api/config", headers=_bearer(raw))
        lines = [r.getMessage() for r in caplog.records if r.name == "launcher.auth"]
        assert any("GET /api/board → 200" in line for line in lines), lines
        assert any("GET /api/config → 403" in line for line in lines), lines
        assert not any(raw in line for line in lines)


# ------------------------------------------------------------- the filing route


class TestFileIssue:
    def test_main_token_files_without_the_delegate_footer(self, gated):
        app, main, _raw, filed = gated
        resp = _as(app, TAILNET).post(
            "/api/board/issues/file", headers=_bearer(main),
            json={"repo": "myrepo", "title": "A title", "body": "Body."},
        )
        assert resp.status_code == 200
        assert filed[0]["body"] == "Body."

    @pytest.mark.parametrize("body,status", [
        ({"repo": "myrepo", "title": ""}, 400),
        ({"repo": "myrepo", "title": "x" * 257}, 400),
        ({"repo": "myrepo", "title": "ok", "body": "x" * 20_001}, 400),
        ({"repo": "not-a-repo", "title": "ok"}, 404),
    ])
    def test_refuses_bad_input(self, gated, body, status):
        app, _main, raw, filed = gated
        resp = _as(app, TAILNET).post("/api/board/issues/file", headers=_bearer(raw), json=body)
        assert resp.status_code == status
        assert filed == []

    def test_a_gh_failure_is_a_502(self, gated, monkeypatch):
        app, _main, raw, _ = gated

        def broken(*_args):
            raise github_client.GhError("gh issue exited 1: not authenticated")

        monkeypatch.setattr(github_client, "create_issue", broken)
        resp = _as(app, TAILNET).post(
            "/api/board/issues/file", headers=_bearer(raw),
            json={"repo": "myrepo", "title": "ok"},
        )
        assert resp.status_code == 502


def test_create_issue_passes_the_body_on_stdin(monkeypatch):
    calls: list = []

    def fake_run(args, *, timeout=None, stdin_text=None):
        calls.append((list(args), stdin_text))
        return "https://github.com/owner/repo/issues/9\n"

    monkeypatch.setattr(github_client, "_run_gh", fake_run)
    url = github_client.create_issue("owner", "repo", "Title", "Line one\nline two")
    assert url == "https://github.com/owner/repo/issues/9"
    args, stdin_text = calls[0]
    assert args[:4] == ["issue", "create", "--repo", "owner/repo"]
    assert "--body-file" in args and args[args.index("--body-file") + 1] == "-"
    assert stdin_text == "Line one\nline two"
