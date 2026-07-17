"""Settings-tab API-tokens panel (issue #72).

Browser-side contract for the mint/list/revoke panel:

  * The panel lives in the Settings pane; opening the tab lazily fetches
    the token list and the job choices for the scope <select>.
  * Mint POSTs ``{label, jobs: [<selected job>]}`` and surfaces the
    response's raw token in the show-once box.
  * Revoke DELETEs by id and the list re-renders without the row.

Backend contracts (hashing, scope enforcement over real HTTP, show-once
persistence) are covered by ``tests/test_webapp_api_tokens.py``; this
file pins the panel wiring with deterministic route mocks — same
"mock non-deterministic fetches before goto()" convention as the other
Jobs-surface e2e tests (the disposable webapp reads the REAL
config/jobs.json, so a mock is also what keeps this test hermetic).
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

_JOBS = {
    "jobs": [
        {
            "id": "demo-job",
            "name": "Demo job",
            "script_path": "C:\\stub\\demo.bat",
            "args": "",
            "schedule": {"type": "none"},
            "schedule_chip": "",
            "paused": False,
            "params": [],
            "last_run": None,
            "running": False,
            "stuck": False,
            "queue_depth": 0,
            "run_count": 0,
            "pinned_count": 0,
            "stats": {
                "p50": None,
                "p95": None,
                "success_rate_30d": None,
                "completed_count": 0,
                "last7": [],
            },
            "next_run": None,
            "next_run_epoch": None,
            "next_run_iso": None,
            "target_kind": "batch",
            "manual_run_allowed": True,
            "schedule_controls_allowed": True,
        }
    ]
}

_MINTED = {
    "id": "tok-abc123",
    "label": "Deck btn",
    "scope": {"jobs": ["demo-job"]},
    "created_at": "2026-07-17T10:00:00",
    "last_used_at": "",
}


def _wire(page: Page, state: Dict[str, Any]) -> None:
    """Route /api/jobs and /api/tokens deterministically.

    ``state["rows"]`` is the mutable token list the GET handler serves;
    mint/revoke handlers mutate it so the panel's re-fetch after each
    action renders the updated list, like the real backend would.
    """
    page.route(
        re.compile(r".*/api/jobs(\?.*)?$"),
        lambda route: route.fulfill(json=_JOBS),
    )

    def handle_tokens(route):
        method = route.request.method
        if method == "POST":
            body = json.loads(route.request.post_data or "{}")
            state["mint_body"] = body
            state["rows"] = state["rows"] + [dict(_MINTED, label=body["label"])]
            route.fulfill(
                json=dict(_MINTED, label=body["label"], token="raw-once-secret-42")
            )
            return
        route.fulfill(json={"tokens": state["rows"]})

    page.route(re.compile(r".*/api/tokens$"), handle_tokens)

    def handle_revoke(route):
        if route.request.method != "DELETE":
            route.fallback()
            return
        token_id = route.request.url.rstrip("/").split("/")[-1]
        state["revoked"] = token_id
        state["rows"] = [r for r in state["rows"] if r["id"] != token_id]
        route.fulfill(json={"revoked": token_id})

    page.route(re.compile(r".*/api/tokens/[^/]+$"), handle_revoke)


def _open_settings(page: Page, base_url: str) -> None:
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("#tabSettings").click()
    expect(page.locator("#paneSettings")).to_be_visible()


def test_mint_shows_token_once_and_lists_it(
    authed_page: Page, base_url: str
) -> None:
    state: Dict[str, Any] = {"rows": []}
    _wire(authed_page, state)
    _open_settings(authed_page, base_url)

    expect(authed_page.locator("#tokensPanel")).to_be_visible()
    expect(authed_page.locator("#tokensEmpty")).to_be_visible()
    # The scope select filled from /api/jobs on first open.
    expect(authed_page.locator("#tokenJobSelect option")).to_have_count(1)
    expect(authed_page.locator("#tokenJobSelect")).to_have_value("demo-job")

    authed_page.locator("#tokenLabelInput").fill("Deck btn")
    authed_page.locator("#tokenMintBtn").click()

    # Show-once box carries the raw token from the mint response.
    expect(authed_page.locator("#tokenMintResult")).to_be_visible()
    expect(authed_page.locator("#tokenMintValue")).to_have_value(
        "raw-once-secret-42"
    )
    # The POST body scoped the token to the selected job.
    assert state["mint_body"] == {"label": "Deck btn", "jobs": ["demo-job"]}
    # And the list re-rendered with the new row.
    expect(authed_page.locator("#tokensList .token-row")).to_have_count(1)
    expect(authed_page.locator("#tokensList")).to_contain_text("Deck btn")
    expect(authed_page.locator("#tokensList")).to_contain_text("job: demo-job")
    expect(authed_page.locator("#tokensEmpty")).to_be_hidden()


def test_mint_requires_label(authed_page: Page, base_url: str) -> None:
    state: Dict[str, Any] = {"rows": []}
    _wire(authed_page, state)
    _open_settings(authed_page, base_url)

    authed_page.locator("#tokenMintBtn").click()
    # No POST fired; the panel surfaced a toast instead.
    assert "mint_body" not in state
    expect(authed_page.locator("#tokenMintResult")).to_be_hidden()


def test_revoke_removes_row(authed_page: Page, base_url: str) -> None:
    rows: List[Dict[str, Any]] = [dict(_MINTED)]
    state: Dict[str, Any] = {"rows": rows}
    _wire(authed_page, state)
    _open_settings(authed_page, base_url)

    expect(authed_page.locator("#tokensList .token-row")).to_have_count(1)
    authed_page.locator("#tokensList .token-row button", has_text="Revoke").click()
    assert_revoked = authed_page.locator("#tokensList .token-row")
    expect(assert_revoked).to_have_count(0)
    expect(authed_page.locator("#tokensEmpty")).to_be_visible()
    assert state["revoked"] == "tok-abc123"
