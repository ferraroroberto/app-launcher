"""Life OS tab e2e: the terminal token on launch and the Telegram channel profiles."""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.ui import life_launch_switch, open_settings_sheet
from tests.e2e.fixtures.waits import wait_until

from tests.e2e.life_os._support import _mock_recap, _mock_skills, _skill_menu_item


pytestmark = pytest.mark.smoke


# ---- #1036: both launch routes are passkey-gated, so the client must send
# X-Terminal-Token. Loopback and the e2e autoboot bypass the server gate, so a
# missing header would pass every server-facing test here — this pins the
# client half directly (the #997 failure mode) by making ensureTerminalToken()
# believe the gate is configured and the connection is not loopback, seeding a
# cached token, and asserting it rides the launch POST.
_SEEDED_TERMINAL_TOKEN = "synthetic-terminal-token"


def _pretend_passkey_gate(page: Page) -> None:
    page.add_init_script(
        "localStorage.setItem('launcher.tt', %s);"
        "localStorage.setItem('launcher.tt.exp', String(Date.now() + 3600000));"
        % _json.dumps(_SEEDED_TERMINAL_TOKEN)
    )
    page.route(
        re.compile(r".*/api/webauthn/status$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"configured": True, "credentials": 1}),
        ),
    )

    def _status_over_tailnet(route):
        body = route.fetch().json()
        body["terminal"] = {"reachable": True, "reason": "tailnet"}
        route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)
        )

    page.route(re.compile(r".*/api/status$"), _status_over_tailnet)


@pytest.mark.parametrize("target", ["skill", "recap"])
def test_life_os_launch_sends_terminal_token(
    authed_page: Page, base_url: str, target: str
) -> None:
    """Regression for #1036: tapping a Life OS launch (a skill tile's 🚀 or
    the weekly-recap 🚀) sends the passkey terminal token, as the Board's
    issue-start does. Without it a phone behind a
    configured WebAuthn gate gets a 401 and the login overlay (cf. #997).
    The real check is still a tap on a phone with the gate configured."""
    _mock_skills(authed_page)
    _mock_recap(authed_page, staleness="fresh", age_days=1.0)
    _pretend_passkey_gate(authed_page)

    captured: dict = {}

    def _capture(route):
        captured["headers"] = route.request.headers
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "launched": "x", "name": "x",
                "session": {"session_id": "x", "kind": "remote"},
            }),
        )

    path = (
        r".*/api/life-os/skills/journal-daily/launch$" if target == "skill"
        else r".*/api/life-os/recap/launch$"
    )
    authed_page.route(re.compile(path), _capture)

    with authed_page.expect_response(re.compile(r".*/api/webauthn/status$")):
        authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()
    expect(authed_page.locator("#lifeOsList li.lifeos-item").first).to_be_visible(
        timeout=5_000
    )
    # Detached → remote, so the mocked response opens no terminal overlay.
    life_launch_switch(authed_page, "#lifeOsDetached")
    if target == "skill":
        _skill_menu_item(
            authed_page, "journal-daily", "[data-item=lifeos-start-new-btn]"
        ).click()
    else:
        expect(authed_page.locator("#lifeOsRecap")).to_be_visible()
        authed_page.locator("#lifeOsRecapLaunch").click()

    wait_until(authed_page, lambda: "headers" in captured, f"the {target} launch POST")
    assert "headers" in captured, f"{target} launch POST was never intercepted"
    assert captured["headers"].get("x-terminal-token") == _SEEDED_TERMINAL_TOKEN, (
        f"{target} launch sent no X-Terminal-Token — behind a configured "
        "passkey gate this is a 401 + login overlay on the phone (#1036, #997)"
    )


# ------------------------------------------------ Telegram channels (#1366)
_FAKE_SETUP = {
    "file_present": True, "life_os_found": True, "bun": True, "plugin": True,
    "skills": [
        {"id": "journal-daily", "name": "Journal Daily"},
        {"id": "sparring-work", "name": "Sparring Work"},
    ],
}


def _mock_channels(page: Page, body: dict, launches: list) -> None:
    page.route(
        re.compile(r".*/api/life-os/channels$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body),
        ),
    )

    def _launch(route):
        launches.append({
            "url": route.request.url,
            "body": _json.loads(route.request.post_data or "{}"),
        })
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "launched": "journal-daily", "profile": "health",
                "session": {"session_id": "s-health", "kind": "remote"},
            }),
        )

    page.route(re.compile(r".*/api/life-os/channels/[^/]+/launch$"), _launch)


def test_settings_channels_card_reports_each_profile(
    authed_page: Page, base_url: str
) -> None:
    """With profiles, the Settings sheet lists each one as Ready or Needs setup
    and names what is missing; Re-check re-reads the server (#1369). Since
    #1439 it is the only place a broken profile or a problem is reported."""
    _mock_skills(authed_page)
    body = {
        "profiles": [
            {"id": "health", "label": "Health", "skill": "journal-daily",
             "skill_found": True, "env_present": True, "running": False,
             "session_id": ""},
            {"id": "school", "label": "School", "skill": "nope",
             "skill_found": False, "env_present": False, "running": False,
             "session_id": ""},
        ],
        "problems": ["profile 'house': state_dir does not exist"],
        "setup": _FAKE_SETUP,
    }
    _mock_channels(authed_page, body, [])
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()
    expect(
        authed_page.locator("#lifeOsList li[data-id='journal-daily'] .action-row-meta")
    ).to_contain_text("Telegram", timeout=5_000)
    # The broken profile and the problem are Settings' alone (#1439).
    expect(authed_page.locator("#paneLifeOS")).not_to_contain_text("School")
    expect(authed_page.locator("#paneLifeOS")).not_to_contain_text("house")
    authed_page.locator("#paneLifeOS > .home-head .settings-open-btn").click()
    expect(authed_page.locator("#paneSettings")).to_be_visible()
    # Closed by default, like its sibling Settings sheets: not open until its
    # row is tapped.
    expect(authed_page.locator("#channelsSheet")).not_to_have_attribute("open", "")
    open_settings_sheet(authed_page, "channelsSheet")
    rows = authed_page.locator("#channelProfileChecks .channel-check")
    expect(rows).to_have_count(2)
    # A ready profile reads as a plain value, no chip; only the exception
    # wears one, in attention (#1506).
    expect(rows.nth(0).locator(".channel-check-value")).to_have_text("Ready")
    expect(rows.nth(0).locator(".channel-check-chip")).to_have_count(0)
    expect(rows.nth(1).locator(".channel-check-chip")).to_have_text("Needs setup")
    expect(rows.nth(1).locator(".channel-check-chip")).to_have_class(
        re.compile(r"\bchannel-check-chip-attention\b")
    )
    expect(rows.nth(1)).to_contain_text("skill “nope” not found")
    expect(rows.nth(1)).to_contain_text("no .env")
    expect(authed_page.locator("#channelProfileProblems")).to_contain_text("house")
    # Names and booleans only: no drive path, no token line anywhere in the card.
    expect(authed_page.locator("#channelsSheet")).not_to_contain_text(
        re.compile(r"[A-Za-z]:[\\/]|TELEGRAM_BOT_TOKEN=\d")
    )
    body["profiles"][1].update({"skill_found": True, "env_present": True})
    authed_page.locator("#channelRecheck").click()
    expect(rows.nth(1).locator(".channel-check-value")).to_have_text("Ready")


def test_channel_session_carries_its_label_in_the_coding_list(
    authed_page: Page, base_url: str
) -> None:
    """A telegram:<profile> session reads "Telegram · Health" on its Coding
    row, so a chat-bound session is recognisable at a glance (#1366)."""
    session = {
        "session_id": "s-tg", "kind": "pty", "agent": "claude",
        "label": "telegram:health", "project_dir": "E:/automation/life-os",
        "name": "journal-daily", "flags": "", "started_at": "2026-07-19T06:30:00Z",
        "alive": True, "rows": 40, "cols": 120, "live_title": "",
        "prompt_title": "", "manual_title": "", "output_chars": 10,
    }
    authed_page.route(
        re.compile(r".*/api/claude-code/sessions$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"sessions": [session]}),
        ),
    )
    # Channel sessions are hidden from the list by default (#1384); this test is
    # about the row they wear when shown, so the setting is off here.
    def _show_channels(route):
        resp = route.fetch()
        body = resp.json()
        body["hide_channel_sessions"] = False
        route.fulfill(response=resp, json=body)

    authed_page.route(re.compile(r".*/api/config$"), _show_channels)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    tag = authed_page.locator(".session-item .session-channel-tag")
    expect(tag).to_have_text("Telegram · Health", timeout=10_000)
