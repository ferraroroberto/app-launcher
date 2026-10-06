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

    The launch endpoint is stubbed (no `session` in the reply, so apps.js's
    launchApp skips handleLaunchResponse → openTerminal and no WebSocket is
    opened); tests read the POST body via ``page.expect_request`` rather
    than a shared capture.
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

    # No `session` in the reply → launchApp (apps.js) skips
    # handleLaunchResponse, so openTerminal never runs (no WS).
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
    # Projects is collapsed by default (#383 review round) — expand it so
    # the tile buttons are clickable.
    page.locator("details.projects-card").evaluate("el => { el.open = true; }")
    page.wait_for_selector(
        '#claudeList .coding-item[data-id="demo"] button.action-row-main[data-agent="claude"]',
        timeout=5_000,
    )


def test_resume_toggle_present(authed_page: Page, base_url: str) -> None:
    _install_mocks(authed_page)
    _open_coding(authed_page, base_url)
    # The toggle lives in the (collapsed) options <summary>, so its
    # role="switch" button is present and starts unchecked (issue #355).
    expect(authed_page.locator("#claudeResume")).to_be_attached()
    expect(authed_page.locator("#claudeResume")).not_to_be_checked()


def test_resume_only_launch_streams_pty(
    authed_page: Page, base_url: str
) -> None:
    _install_mocks(authed_page)
    _open_coding(authed_page, base_url)

    # Resume alone (Detached off) → streamed pty: resume=true, no remote mode.
    authed_page.locator("#claudeResume").click()
    expect(authed_page.locator("#claudeResume")).to_be_checked()

    with authed_page.expect_request("**/api/apps/*/launch") as req_info:
        authed_page.locator(
            '#claudeList .coding-item[data-id="demo"] '
            'button.action-row-main[data-agent="claude"]'
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
    authed_page.locator("#claudeDetached").click()
    authed_page.locator("#claudeResume").click()
    expect(authed_page.locator("#claudeResume")).to_be_checked()

    with authed_page.expect_request("**/api/apps/*/launch") as req_info:
        authed_page.locator(
            '#claudeList .coding-item[data-id="demo"] '
            'button.action-row-main[data-agent="claude"]'
        ).click()

    payload = req_info.value.post_data_json
    assert payload.get("resume") is True
    assert payload.get("mode") == "remote"
    assert payload.get("agent") == "claude"


@pytest.mark.iphone
def test_checked_toggle_carries_no_accent_border_or_tint(
    authed_page: Page, base_url: str
) -> None:
    """#1070 — the on-state is an opacity + glyph-colour step, not a box.

    Detached/Resume used to flip `border-color` to `var(--accent)` and paint
    an accent-tinted background when checked, which made them the only
    accent-boxed controls in the Projects header — next to a borderless
    model combo and a `.button-ghost` favourites filter, so the box goes
    and the on-state is a glyph-colour step (the accent since #1396, which
    pins the colour; green was the earlier reading of design.md).

    Pinned as "the border does not change and the fill stays transparent"
    rather than against a literal colour, so the assertion holds in both
    themes and survives a token revalue. Auto-retrying `to_have_css`
    throughout (#680) — the expected border value is read once from the
    *off* state, which is the control's own resting truth.
    """
    _install_mocks(authed_page)
    # `.detached-toggle` animates border-color and background over 0.15s.
    # An assertion made right after the click can sample a mid-transition
    # value that still equals the resting one and pass for the wrong
    # reason — measured: against pre-fix CSS the border assertion below
    # passed on its first poll while the fill was still interpolating.
    # Killing transitions makes both reads the settled truth.
    authed_page.add_init_script(
        "document.addEventListener('DOMContentLoaded', () => {"
        "  const st = document.createElement('style');"
        "  st.textContent = '*, *::before, *::after "
        "{ transition: none !important; animation: none !important; }';"
        "  document.head.appendChild(st);"
        "});"
    )
    _open_coding(authed_page, base_url)

    toggle = authed_page.locator("#claudeDetached")
    expect(toggle).not_to_be_checked()
    resting_border = toggle.evaluate(
        "el => getComputedStyle(el).borderTopColor"
    )
    # The Projects header is static markup, not a polled re-render, so this
    # one read cannot straddle a rebuild; every assertion below still uses
    # the auto-retrying form.
    assert resting_border, "could not read the toggle's resting border colour"

    toggle.click()
    expect(toggle).to_be_checked()
    # The box is unchanged by the flip …
    expect(toggle).to_have_css("border-top-color", resting_border)
    expect(toggle).to_have_css("background-color", "rgba(0, 0, 0, 0)")


@pytest.mark.iphone
def test_on_glyph_switches_use_the_accent_not_success(
    authed_page: Page, base_url: str
) -> None:
    """#1396 — the fleet design standard: a `role="switch"`'s on-state is the
    app's accent, and `success` is never a switch's on-colour. The Detached
    and Resume glyph switches (Coding and Life OS rows) kept the green glyph
    #1070 gave them; the track-and-thumb switches had already moved.

    The expected colours are resolved from the tokens in the page's own
    theme (a probe element), in light and in dark, so the assertion survives
    a token revalue. Auto-retrying `to_have_css` throughout (#680). Life OS's
    pair sits in a hidden pane, which computed style still reads.
    """
    _install_mocks(authed_page)
    authed_page.add_init_script(
        "document.addEventListener('DOMContentLoaded', () => {"
        "  const st = document.createElement('style');"
        "  st.textContent = '*, *::before, *::after "
        "{ transition: none !important; animation: none !important; }';"
        "  document.head.appendChild(st);"
        "});"
    )
    _open_coding(authed_page, base_url)
    resolve = (
        "name => { const p = document.createElement('span');"
        "p.style.color = `var(${name})`; document.body.appendChild(p);"
        "const c = getComputedStyle(p).color; p.remove(); return c; }"
    )
    ids = ("#claudeDetached", "#claudeResume", "#lifeOsDetached", "#lifeOsResume")
    for theme in ("light", "dark"):
        authed_page.evaluate(
            "t => document.documentElement.setAttribute('data-theme', t)", theme
        )
        accent = authed_page.evaluate(resolve, "--accent")
        success = authed_page.evaluate(resolve, "--success")
        assert accent != success, f"{theme}: the probe cannot tell the accent from success"
        for sel in ids:
            toggle = authed_page.locator(sel)
            toggle.evaluate("el => el.setAttribute('aria-checked', 'false')")
            off_colour = toggle.evaluate("el => getComputedStyle(el).color")
            assert off_colour not in (accent, success), (
                f"{theme} {sel} off already shows an on-colour: {off_colour}"
            )
            toggle.evaluate("el => el.setAttribute('aria-checked', 'true')")
            expect(toggle).to_have_css("color", accent)
