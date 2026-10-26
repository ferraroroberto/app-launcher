"""Resume toggle regression (issues #151, #157).

The Resume switch in the Projects card's launch toolbar (#1434; it began on
the Coding options card) must, when checked, make
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
import re

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
    # The launch toolbar's role="switch" button is present and starts
    # unchecked (issue #355).
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


_RESOLVE = (
    "name => { const p = document.createElement('span');"
    "p.style.color = `var(${name})`; document.body.appendChild(p);"
    "const c = getComputedStyle(p).color; p.remove(); return c; }"
)


@pytest.mark.iphone
def test_launch_toolbar_switches_are_vendored_and_accent_when_on(
    authed_page: Page, base_url: str
) -> None:
    """#1434 — the Code launch toolbar's Detached and Resume are the vendored
    switch (track + thumb, no box), each beside its word, and the word is
    part of the tap: tapping "Detached" flips it. On, the track is the
    accent fill, never success (design.md, fleet-config#1200). The colours
    are resolved from the page's own tokens in light and dark, so the
    assertion survives a token revalue. Auto-retrying `to_have_css` (#680).
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

    toolbar = authed_page.locator("#codingLaunchToolbar .launch-toolbar")
    for sel, word in (("#claudeDetached", "Detached"), ("#claudeResume", "Resume")):
        sw = toolbar.locator(sel)
        expect(sw).to_have_class(re.compile(r"\btoggle\b"))
        expect(sw).to_have_attribute("role", "switch")
        expect(sw.locator("xpath=..")).to_contain_text(word)

    detached = toolbar.locator("#claudeDetached")
    expect(detached).not_to_be_checked()
    toolbar.locator(".launch-switch-label", has_text="Detached").click()
    expect(detached).to_be_checked()

    for theme in ("light", "dark"):
        authed_page.evaluate(
            "t => document.documentElement.setAttribute('data-theme', t)", theme
        )
        fill = authed_page.evaluate(_RESOLVE, "--accent-fill")
        success = authed_page.evaluate(_RESOLVE, "--success")
        assert fill != success, f"{theme}: the probe cannot tell the accent from success"
        expect(detached).to_have_css("background-color", fill)


@pytest.mark.iphone
def test_life_launch_options_live_in_the_model_menu(
    authed_page: Page, base_url: str
) -> None:
    """#1503 decision 6: Life's Detached and Resume moved into the model
    combo's menu, under the model list. They are still the vendored switch
    beside its word, accent when on (never success), and toggle in place
    without closing the menu; a model pick is still one tap that closes it.
    Closed, the combo's label names any switch that is on. Resolved from the
    page's own tokens in light and dark; auto-retrying `to_have_css` (#680)."""
    _install_mocks(authed_page)
    authed_page.add_init_script(
        "document.addEventListener('DOMContentLoaded', () => {"
        "  const st = document.createElement('style');"
        "  st.textContent = '*, *::before, *::after "
        "{ transition: none !important; animation: none !important; }';"
        "  document.head.appendChild(st);"
        "});"
    )
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()
    toolbar = authed_page.locator("#lifeOsLaunchToolbar .launch-toolbar")
    expect(toolbar).to_be_visible()
    expect(toolbar.locator(".launch-toolbar-switches")).to_have_count(0)
    expect(authed_page.locator("#paneLifeOS .detached-toggle")).to_have_count(0)

    combo = authed_page.locator("#lifeOsModelCombo")
    trigger = authed_page.locator("#lifeOsModelBtn")
    menu = authed_page.locator("#lifeOsModelMenu")
    expect(trigger).to_have_attribute("aria-haspopup", "dialog")
    options = menu.locator(".model-combo-list[role='listbox'] [role='option']")
    expect(options.nth(1)).to_be_attached()  # boot config landed: it would close an open menu
    trigger.click()
    expect(menu).to_be_visible()
    expect(menu).to_have_attribute("role", "dialog")
    expect(options.first).to_be_visible()
    for sel, word in (("#lifeOsDetached", "Detached"), ("#lifeOsResume", "Resume")):
        sw = menu.locator(".launch-menu-options[role='group'] " + sel)
        expect(sw).to_have_class(re.compile(r"\btoggle\b"))
        expect(sw).to_have_attribute("role", "switch")
        expect(sw.locator("xpath=..")).to_contain_text(word)
        row = sw.locator("xpath=..").bounding_box()
        assert row and row["height"] >= 44, (sel, row)
    expect(menu.locator(".launch-menu-hint")).to_contain_text("a resumed one keeps its own")

    detached = menu.locator("#lifeOsDetached")
    expect(detached).not_to_be_checked()
    menu.locator(".launch-switch-label", has_text="Detached").click()
    expect(detached).to_be_checked()
    expect(menu).to_be_visible()   # a switch toggles in place
    for theme in ("light", "dark"):
        authed_page.evaluate(
            "t => document.documentElement.setAttribute('data-theme', t)", theme
        )
        fill = authed_page.evaluate(_RESOLVE, "--accent-fill")
        success = authed_page.evaluate(_RESOLVE, "--success")
        assert fill != success, f"{theme}: the probe cannot tell the accent from success"
        expect(detached).to_have_css("background-color", fill)
        # The off switch keeps its own track, not the menu buttons' blank one.
        expect(menu.locator("#lifeOsResume")).not_to_have_css(
            "background-color", "rgba(0, 0, 0, 0)")

    # A model pick is one tap, closes the menu, and the label keeps its note.
    pick = options.nth(1)
    value = pick.get_attribute("data-value")
    pick.click()
    expect(menu).to_be_hidden()
    expect(combo).to_have_attribute("data-value", value)
    expect(trigger).to_have_attribute("data-note", "Detached")
    after = trigger.evaluate("el => getComputedStyle(el, '::after').content")
    assert "Detached" in after, after

    trigger.click()
    menu.locator("#lifeOsResume").click()
    expect(trigger).to_have_attribute("data-note", "Detached · Resume")
    menu.locator("#lifeOsDetached").click()
    menu.locator("#lifeOsResume").click()
    expect(trigger).not_to_have_attribute("data-note", re.compile(".*"))
    assert trigger.evaluate("el => getComputedStyle(el, '::after').content") in ("none", "normal")


# Code › Projects' launch toolbar as mountLaunchToolbar built it before
# `switchesInMenu` existed, captured on main (cc222f5) by running
# _CODE_TOOLBAR there. Code does not pass the option, so it must not change.
_CODE_TOOLBAR = """async () => {
  const { mountLaunchToolbar } = await import('/static/launch-toolbar.js');
  const host = document.createElement('div');
  mountLaunchToolbar(host, {
    ids: { combo: 'gc', trigger: 'gt', menu: 'gm', detached: 'gd', resume: 'gr' },
    model: { value: 'claude:opus', label: 'Claude · Opus', title: 'Model', menuLabel: 'Launch model' },
    detachedTitle: 'Detached title', resumeTitle: 'Resume title',
  });
  return host.innerHTML;
}"""

_CODE_TOOLBAR_GOLDEN = (
    "<div class=\"launch-toolbar\"><div class=\"launch-toolbar-head\"><span class=\"model-combo\" id="
    "\"gc\" data-value=\"claude:opus\"><button class=\"model-combo-trigger\" type=\"button\" id=\"gt\" ar"
    "ia-haspopup=\"listbox\" aria-expanded=\"false\" aria-controls=\"gm\" title=\"Model\">Claude \u00b7 Opus"
    "</button><span class=\"model-combo-menu\" id=\"gm\" role=\"listbox\" aria-label=\"Launch model\" h"
    "idden=\"\"><button type=\"button\" role=\"option\" data-value=\"claude:opus\" aria-selected=\"true\""
    ">Claude \u00b7 Opus</button></span></span></div><div class=\"launch-toolbar-switches\"><label cla"
    "ss=\"launch-switch\"><span class=\"launch-switch-label\">Detached</span><button type=\"button\" "
    "aria-label=\"Launch detached\" role=\"switch\" class=\"toggle\" aria-checked=\"false\" id=\"gd\" tit"
    "le=\"Detached title\"><span class=\"knob\"></span><span class=\"toggle-label\">OFF</span></butto"
    "n></label><label class=\"launch-switch\"><span class=\"launch-switch-label\">Resume</span><but"
    "ton type=\"button\" aria-label=\"Resume session\" role=\"switch\" class=\"toggle\" aria-checked=\"f"
    "alse\" id=\"gr\" title=\"Resume title\"><span class=\"knob\"></span><span class=\"toggle-label\">OF"
    "F</span></button></label></div></div>"
)


def test_a_toolbar_without_switches_in_menu_is_byte_identical(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    assert authed_page.evaluate(_CODE_TOOLBAR) == _CODE_TOOLBAR_GOLDEN
