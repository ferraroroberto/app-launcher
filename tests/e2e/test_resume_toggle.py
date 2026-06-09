"""Resume toggle regression (issues #151, #157).

The ↺ Resume toggle on the Coding options card must, when checked, make
the next agent-icon tap POST ``resume: true``. Resume is orthogonal to
Detached (issue #157): Resume alone streams a pty (no ``mode``); Detached
+ Resume sends ``mode: remote`` so the native picker renders in the
detached console. This is the client-side contract; the server-side splice
(resume token, codex flag-dropping, agy --continue, Life OS skill-prompt
drop) is covered by the non-browser suites.

Hermetic: ``/api/agents``, ``/api/apps`` and the launch endpoint are
route-mocked so the test needs no installed agent and spawns no process.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke


def _install_mocks(page: Page) -> None:
    """Route-mock the Coding tab's data + launch so the test is hermetic.

    The launch endpoint is stubbed (no `session` in the reply, so apps.js
    skips openTerminal and no WebSocket is opened); tests read the POST
    body via ``page.expect_request`` rather than a shared capture.
    """
    page.route(
        "**/api/agents",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {"agents": [{"id": "claude", "label": "Claude Code",
                             "available": True}]}
            ),
        ),
    )
    page.route(
        "**/api/apps",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "scan_root": "X",
                    "apps": [
                        {"id": "demo", "name": "demo", "kind": "claude-code",
                         "project_dir": "X", "repo_url": None}
                    ],
                }
            ),
        ),
    )

    # No `session` in the reply → apps.js skips openTerminal (no WS).
    # Registered after the broad /api/apps mock so it takes precedence for
    # the .../launch sub-path (Playwright checks newest route first).
    page.route(
        "**/api/apps/*/launch",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {"launched": "demo", "name": "demo", "kind": "claude-code",
                 "agent": "claude", "mode": "pty", "session": None}
            ),
        ),
    )


def _open_coding(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector(
        '#claudeList .coding-item[data-id="demo"] button.agent-btn[data-agent="claude"]',
        timeout=5_000,
    )


def test_resume_toggle_present(authed_page: Page, base_url: str) -> None:
    _install_mocks(authed_page)
    _open_coding(authed_page, base_url)
    # The toggle lives in the (collapsed) options <summary>, so its label
    # is present and its checkbox starts unchecked.
    expect(authed_page.locator("#claudeResume")).to_be_attached()
    expect(authed_page.locator("#claudeResume")).not_to_be_checked()


def test_resume_only_launch_streams_pty(
    authed_page: Page, base_url: str
) -> None:
    _install_mocks(authed_page)
    _open_coding(authed_page, base_url)

    # Resume alone (Detached off) → streamed pty: resume=true, no remote mode.
    authed_page.locator("label.detached-toggle:has(#claudeResume)").click()
    expect(authed_page.locator("#claudeResume")).to_be_checked()

    with authed_page.expect_request("**/api/apps/*/launch") as req_info:
        authed_page.locator(
            '#claudeList .coding-item[data-id="demo"] '
            'button.agent-btn[data-agent="claude"]'
        ).click()

    payload = req_info.value.post_data_json
    assert payload.get("resume") is True
    assert payload.get("mode") != "remote"
    assert payload.get("agent") == "claude"


def test_resume_with_detached_launches_remote_console(
    authed_page: Page, base_url: str
) -> None:
    _install_mocks(authed_page)
    _open_coding(authed_page, base_url)

    # Detached + Resume → remote console: resume=true AND mode=remote (#157).
    authed_page.locator("label.detached-toggle:has(#claudeDetached)").click()
    authed_page.locator("label.detached-toggle:has(#claudeResume)").click()
    expect(authed_page.locator("#claudeResume")).to_be_checked()

    with authed_page.expect_request("**/api/apps/*/launch") as req_info:
        authed_page.locator(
            '#claudeList .coding-item[data-id="demo"] '
            'button.agent-btn[data-agent="claude"]'
        ).click()

    payload = req_info.value.post_data_json
    assert payload.get("resume") is True
    assert payload.get("mode") == "remote"
    assert payload.get("agent") == "claude"
