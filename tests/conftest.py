"""Shared pytest fixtures for app-launcher.

Mirrors the pattern used by sister projects (voice-transcriber / photo-ocr):
build a fresh FastAPI ``create_app()`` against an isolated temp config dir,
with the expensive deps (session-host loopback client, audit log writer)
swapped for mocks. Tests run in-process via ``TestClient`` — no live tray,
no real session-host on :8446, no disk writes outside ``tmp_path``.

The live-tray Playwright suite lives separately under ``tests/e2e/`` and is
opt-in via ``pytest -m smoke``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture
def sample_webapp_config() -> dict:
    """Parse the committed sample once per test."""
    return json.loads(
        (PROJECT_ROOT / "config" / "webapp_config.sample.json").read_text(
            encoding="utf-8"
        )
    )


@pytest.fixture
def webapp_client(tmp_path: Path, monkeypatch) -> Iterator[tuple]:
    """Build a fresh launcher webapp wired to temp dirs + mocked deps.

    Yields ``(client, app, overrides)``:
      - ``client`` — fastapi.testclient.TestClient
      - ``app`` — the FastAPI instance (so tests can mutate ``app.state``)
      - ``overrides`` — dict of the mocks, so tests can configure return
        values / assert call args.

    Auth is disabled by default (``auth_token = ""``, ``auth_password = ""``).
    Auth tests opt back in by setting these on ``app.state.webapp_config``.
    """
    # The two on-disk configs the app reads on startup. Point both at temp
    # paths so a stray real config can never affect the test.
    tmp_apps_root = tmp_path / "scan_root"
    tmp_apps_root.mkdir()
    tmp_projects_dir = tmp_path / "projects_dir"
    tmp_projects_dir.mkdir()

    # webapp_config.json — empty file with valid defaults overlaid.
    tmp_webapp_cfg = tmp_path / "webapp_config.json"
    tmp_webapp_cfg.write_text(
        json.dumps(
            {
                "host": "127.0.0.1",
                "port": 8445,
                "projects_dir": str(tmp_projects_dir),
                "apps_scan_root": str(tmp_apps_root),
                "claude_model": "opus",
                "claude_effort": "high",
                "claude_verbose": True,
                "claude_debug": False,
                "auth_token": "",
                "auth_password": "",
                "session_host_port": 8446,
            }
        ),
        encoding="utf-8",
    )
    from src import webapp_config as webapp_cfg_mod
    monkeypatch.setattr(webapp_cfg_mod, "DEFAULT_CONFIG_PATH", tmp_webapp_cfg)

    # apps.json — start empty so registry tests own their fixtures.
    tmp_registry = tmp_path / "apps.json"
    from src import registry as registry_mod
    monkeypatch.setattr(registry_mod, "DEFAULT_REGISTRY_PATH", tmp_registry)

    # app_config.json — also redirect to tmp so create_app's load_app_config
    # doesn't read the real one. The launcher's app_config has very little
    # surface, so an empty file is fine; load_app_config defaults the rest.
    tmp_app_cfg = tmp_path / "config.json"
    tmp_app_cfg.write_text("{}", encoding="utf-8")
    from src import app_config as app_cfg_mod
    if hasattr(app_cfg_mod, "DEFAULT_CONFIG_PATH"):
        monkeypatch.setattr(app_cfg_mod, "DEFAULT_CONFIG_PATH", tmp_app_cfg)

    # Now import the server + routers. Important: import after monkeypatching
    # the config paths, but before patching session_client / audit (which are
    # module-level references inside each router that talks to them).
    from app.webapp import server as server_mod
    from app.webapp.routers import apps as apps_router
    from app.webapp.routers import sessions as sessions_router

    # Mock the session-host loopback client. Every route that talks to
    # :8446 goes through this module; after the issue-#26 split the
    # `session_client` reference lives in both routers/apps.py (launch)
    # and routers/sessions.py (list/stop/image), so patch both.
    from src import session_client as real_session_client
    session_mock = MagicMock()
    session_mock.list_sessions.return_value = []
    session_mock.stop.return_value = {"ok": True}
    session_mock.create_session.return_value = {
        "session_id": "test-session-1",
        "kind": "pty",
    }
    session_mock.upload_image.return_value = {"path": "stub.png"}
    session_mock.SessionHostError = real_session_client.SessionHostError
    monkeypatch.setattr(apps_router, "session_client", session_mock)
    monkeypatch.setattr(sessions_router, "session_client", session_mock)

    # Audit log writer — stub so no files land in webapp/sessions/ during
    # tests. The real audit module opens log files lazily. After the split
    # the `audit` import lives in routers/apps.py, routers/sessions.py,
    # and routers/webauthn.py — patch all three.
    audit_mock = MagicMock()
    from app.webapp.routers import webauthn as webauthn_router
    monkeypatch.setattr(apps_router, "audit", audit_mock)
    monkeypatch.setattr(sessions_router, "audit", audit_mock)
    monkeypatch.setattr(webauthn_router, "audit", audit_mock)

    # WebAuthnGate doesn't touch disk until configured (rp_id + origin set)
    # so default tests are safe. We still stub it for the few endpoints that
    # poke at .configured() to keep behaviour deterministic.
    webauthn_mock = MagicMock()
    webauthn_mock.configured.return_value = False
    monkeypatch.setattr(server_mod, "WebAuthnGate", lambda: webauthn_mock)

    # Build the app fresh. ``create_app()`` calls load_webapp_config /
    # load_app_config / load_registry — all redirected above.
    app = server_mod.create_app()

    # Auth off by default. Tests that want it on do:
    #     app.state.webapp_config.auth_token = "secret"
    #     app.state.webapp_config.auth_password = "hunter2"
    app.state.webapp_config.auth_token = ""
    app.state.webapp_config.auth_password = ""

    from fastapi.testclient import TestClient
    client = TestClient(app)

    overrides = {
        "session": session_mock,
        "audit": audit_mock,
        "webauthn": webauthn_mock,
        "tmp_registry_path": tmp_registry,
        "tmp_apps_scan_root": tmp_apps_root,
        "tmp_projects_dir": tmp_projects_dir,
        "tmp_webapp_cfg_path": tmp_webapp_cfg,
    }
    yield client, app, overrides
