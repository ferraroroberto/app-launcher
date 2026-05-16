"""Fixtures for the Playwright smoke suite.

v1 runs against a live tray the user already has up on https://127.0.0.1:8445.
The autouse `_require_live_tray` fixture skips the whole module with a clear
message if /healthz isn't reachable, so a forgotten tray fails fast instead
of hanging in browser.goto for 30 s.
"""

from __future__ import annotations

import json
import logging
import urllib3
from pathlib import Path
from typing import Iterator

import pytest
import requests
from playwright.sync_api import BrowserContext, Page

logger = logging.getLogger(__name__)

# The webapp uses a self-signed cert; silence the urllib3 noise from /healthz.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WEBAPP_CONFIG = _REPO_ROOT / "config" / "webapp_config.json"
_BASE_URL = "https://127.0.0.1:8445"
_TOKEN_KEY = "launcher.token"  # must match TOKEN_KEY in app/webapp/static/app.js


@pytest.fixture(scope="session")
def base_url() -> str:
    return _BASE_URL


@pytest.fixture(scope="session")
def webapp_config() -> dict:
    if not _WEBAPP_CONFIG.exists():
        pytest.skip(f"{_WEBAPP_CONFIG} missing — copy webapp_config.sample.json first")
    return json.loads(_WEBAPP_CONFIG.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def auth_token(webapp_config: dict) -> str:
    # Loopback bypasses the bearer middleware (server.py:267), so an empty
    # token is fine for local-only tests. We still seed it when present so
    # the SPA boot path mirrors a real phone session.
    return (webapp_config.get("auth_token") or "").strip()


@pytest.fixture(scope="session", autouse=True)
def _require_live_tray(base_url: str) -> None:
    try:
        res = requests.get(f"{base_url}/healthz", timeout=2, verify=False)
        res.raise_for_status()
    except Exception as exc:
        pytest.skip(
            f"Tray not running on 8445 ({exc.__class__.__name__}) — "
            "start tray.bat first, then re-run the suite."
        )


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict) -> dict:
    # Self-signed cert on 8445 — the SPA + service-worker won't load otherwise.
    return {**browser_context_args, "ignore_https_errors": True}


def _seed_token_init_script(token: str) -> str:
    # Seeded *before* the first navigation so app.js reads it on boot rather
    # than going through the ?token=… URL strip dance (which would leak the
    # token into Playwright trace URLs).
    safe = json.dumps(token)
    safe_key = json.dumps(_TOKEN_KEY)
    return f"window.localStorage.setItem({safe_key}, {safe});"


@pytest.fixture
def authed_page(
    context: BrowserContext, base_url: str, auth_token: str
) -> Iterator[Page]:
    if auth_token:
        context.add_init_script(_seed_token_init_script(auth_token))
    page = context.new_page()
    try:
        yield page
    finally:
        page.close()


@pytest.fixture
def unauthed_page(context: BrowserContext) -> Iterator[Page]:
    page = context.new_page()
    try:
        yield page
    finally:
        page.close()


# ---------------------------------------------------------------- session API
# Opt-in fixture: tests that need state in #sessionsList depend on this; other
# tests don't pay the 3-5 s launch + teardown cost. Target is `app-launcher`
# itself (self-launching is harmless — just spawns claude in this repo dir).

_LAUNCH_TARGET_ID = "app-launcher"


def _auth_headers(auth_token: str) -> dict:
    return {"Authorization": f"Bearer {auth_token}"} if auth_token else {}


@pytest.fixture
def launched_pty_session(base_url: str, auth_token: str) -> Iterator[str]:
    headers = _auth_headers(auth_token)
    try:
        res = requests.post(
            f"{base_url}/api/apps/{_LAUNCH_TARGET_ID}/launch",
            json={"mode": "pty"},
            headers=headers,
            verify=False,
            timeout=10,
        )
    except Exception as exc:
        pytest.skip(f"launch request failed: {exc.__class__.__name__}: {exc}")

    if res.status_code != 200:
        # 400 is the expected failure when `claude` isn't on PATH or the
        # project_dir is invalid — skip cleanly rather than fail the suite.
        pytest.skip(f"could not launch PTY session (HTTP {res.status_code}: {res.text[:200]})")

    body = res.json()
    sid = body.get("session", {}).get("session_id")
    if not sid:
        pytest.skip(f"launch response missing session_id: {body}")

    try:
        yield sid
    finally:
        # Force-kill teardown — `mode: "kill"` is unconditional (vs "quit"
        # which waits for claude to process /quit). Swallow exceptions so a
        # stuck claude doesn't mask the actual test failure.
        try:
            requests.post(
                f"{base_url}/api/claude-code/sessions/{sid}/stop",
                json={"mode": "kill", "close_window": True},
                headers=headers,
                verify=False,
                timeout=5,
            )
        except Exception as exc:
            logger.warning("⚠️  session %s teardown failed: %s", sid, exc)
