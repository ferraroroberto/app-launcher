"""Fleet vendored-switch adoption (issue #479)."""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import open_settings_sheet

pytestmark = pytest.mark.smoke


def _assert_switch(page: Page, selector: str) -> None:
    switch = page.locator(selector)
    expect(switch).to_have_class(re.compile(r"^toggle(?: on)?$"))
    expect(switch).to_have_attribute("role", "switch")
    expect(switch.locator(".knob")).to_have_count(1)
    expect(switch.locator(".toggle-label")).to_have_count(1)


def _job(job_id: str, name: str, *, with_bool_param: bool = False) -> dict:
    return {
        "id": job_id,
        "name": name,
        "script_path": f"C:\\stub\\{job_id}.py",
        "target_kind": "py",
        "kind": "python",
        "kind_config": {},
        "args": "",
        "schedule": {"type": "none"},
        "schedule_chip": "",
        "next_run": None,
        "running": False,
        "stuck": False,
        "paused": False,
        "confirm": job_id == "alpha",
        "on_success": ["beta"] if job_id == "alpha" else [],
        "on_failure": [],
        "params": (
            [
                {
                    "name": "verbose",
                    "kind": "bool",
                    "flag": "--verbose",
                    "default": False,
                    "required": False,
                }
            ]
            if with_bool_param
            else []
        ),
        "last_run": None,
        "stats": {
            "p50": None,
            "p95": None,
            "success_rate_30d": None,
            "completed_count": 0,
            "last7": [],
        },
    }


def test_scan_rows_use_vendored_switch(authed_page: Page, base_url: str) -> None:
    payload = {
        "new": [
            {
                "id": "sample-app",
                "name": "Sample App",
                "kind": "webapp",
                "bat_path": "C:\\stub\\sample\\webapp.bat",
            }
        ]
    }
    authed_page.route(
        "**/api/apps/scan",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(payload)
        ),
    )

    authed_page.goto(base_url, wait_until="domcontentloaded")
    # "Scan for new apps" lives in the Folders sheet (#1435).
    open_settings_sheet(authed_page, "foldersSheet")
    authed_page.locator("#rescanBtn").click()

    toggle = authed_page.locator("#scanResults .scan-row .toggle")
    _assert_switch(authed_page, "#scanResults .scan-row .toggle")
    expect(toggle).to_have_attribute("aria-checked", "true")
    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "false")


@pytest.mark.iphone
def test_on_switch_track_is_the_accent_fill_in_both_themes(
    authed_page: Page, base_url: str
) -> None:
    """#1385 — a switch that is on draws `accent-fill`, never green. Pinned as
    "equals what `var(--accent-fill)` resolves to in that theme" rather than a
    literal, so it survives a token revalue; `to_have_css` auto-retries past
    the track's background transition."""
    payload = {"new": [{"id": "sample-app", "name": "Sample App", "kind": "webapp",
                        "bat_path": "C:\\stub\\sample\\webapp.bat"}]}
    authed_page.route(
        "**/api/apps/scan",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(payload)
        ),
    )
    authed_page.goto(base_url, wait_until="domcontentloaded")
    # "Scan for new apps" lives in the Folders sheet (#1435).
    open_settings_sheet(authed_page, "foldersSheet")
    authed_page.locator("#rescanBtn").click()
    toggle = authed_page.locator("#scanResults .scan-row .toggle")
    expect(toggle).to_have_attribute("aria-checked", "true")

    for theme in ("light", "dark"):
        authed_page.evaluate(
            "t => document.documentElement.setAttribute('data-theme', t)", theme
        )
        accent_fill = authed_page.evaluate(
            "() => { const p = document.createElement('i');"
            "p.style.background = 'var(--accent-fill)'; document.body.appendChild(p);"
            "const c = getComputedStyle(p).backgroundColor; p.remove(); return c; }"
        )
        assert accent_fill and accent_fill != "rgba(0, 0, 0, 0)", accent_fill
        expect(toggle).to_have_css("background-color", accent_fill)


def test_job_dialog_switches_use_vendored_component(
    authed_page: Page, base_url: str
) -> None:
    jobs = [_job("alpha", "Alpha", with_bool_param=True), _job("beta", "Beta")]
    authed_page.route(
        re.compile(r".*/api/jobs(?:\?.*)?$"),
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"jobs": jobs}),
        ),
    )

    authed_page.goto(base_url, wait_until="domcontentloaded")
    authed_page.locator("#tabJobs").click()
    authed_page.locator("#jobsEditBtn").click()
    # Edit moved into the row's ⋯ menu (#1130).
    authed_page.locator("#jobsList li[data-id='alpha'] [data-role='job-menu']").click()
    authed_page.locator("#jobsList li[data-id='alpha'] button[aria-label='Edit']").click()

    expect(authed_page.locator("#jobDialog")).to_be_visible()
    _assert_switch(authed_page, "#jobConfirmInput")
    expect(authed_page.locator("#jobConfirmInput")).to_have_attribute(
        "aria-checked", "true"
    )
    chain_toggle = "#jobOnSuccessList .job-chain-row .toggle"
    _assert_switch(authed_page, chain_toggle)
    expect(authed_page.locator(chain_toggle)).to_have_attribute(
        "aria-checked", "true"
    )

    authed_page.locator("#jobCancel").click()
    authed_page.locator("#jobsList li[data-id='alpha'] [data-role='run-btn']").click()

    expect(authed_page.locator("#jobRunDialog")).to_be_visible()
    _assert_switch(
        authed_page,
        "#jobRunDialogFields .toggle[data-param-name='verbose']",
    )
    _assert_switch(authed_page, "#jobRunDialogDryRun")


def test_static_boolean_controls_have_no_checkbox_markup(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(base_url, wait_until="domcontentloaded")
    selectors = (
        "#claudeVerbose",
        "#claudeDebug",
        "#antigravitySkipPerms",
        "#antigravitySandbox",
        "#copilotSkipPerms",
        "#jobsEditBtn",
        "#bootAutostartToggle",
        "#jobAlertOnFailureInput",
        "#jobConfirmInput",
        "#jobRunDialogDryRun",
    )
    for selector in selectors:
        _assert_switch(authed_page, selector)
    expect(authed_page.locator(".check-box")).to_have_count(0)

    authed_page.locator("#tabJobs").click()
    edit_control = authed_page.locator(".jobs-edit-control")
    expect(edit_control).to_contain_text("Edit mode")
    expect(edit_control.locator("#jobsEditBtn")).to_have_count(1)
