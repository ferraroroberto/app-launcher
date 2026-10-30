"""The hub's PC health verdict beside the Code header's CPU reading.

The hub judges the machine; the launcher draws a dot on the reading only for ``watch`` or
``act``, and the PC load sheet carries the reason and fix. A verdict the
launcher cannot read is ``Not confirmed`` in the sheet, never ``OK``.
``/api/system/load`` and ``/api/system/health`` are route-mocked with invented
data before ``goto()``.

The same sheet's rows keep the figure on one line: a 60-character process
label wraps in its own cell instead of squeezing ``0.7 cores`` a letter per line.
"""
from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.waits import stable_eval
from tests.e2e.shell.test_header_cpu_reading import _boot, _reading, _route_load

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_LONG_LABEL = "python (run_acceptance_suite_for_the_launcher) ×2 · app-launcher-wt"
_CHIP = "#paneClaude > .home-head .cpu-reading"


def _route_health(page: Page, body, status: int = 200) -> None:
    page.route(
        re.compile(r".*/api/system/health$"),
        lambda route: route.fulfill(
            status=status, content_type="application/json", body=json.dumps(body)
        ),
    )


def _verdict(verdict: str, reason=None, fix=None, why=None) -> dict:
    return {"verdict": verdict, "why": why, "reason": reason, "fix": fix}


def _open_sheet(page: Page):
    page.locator("#paneClaude > .home-head .cpu-reading").click()
    sheet = page.locator("#cpuSheet")
    expect(sheet).to_be_visible()
    return sheet.locator("#cpuSheetBody")


def test_ok_draws_no_dot_and_the_sheet_says_ok(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _route_load(page, {"body": _reading("normal", 20.0)}, [])
    _route_health(page, _verdict("ok"))
    _boot(page, base_url)
    expect(page.locator("#paneClaude > .home-head .cpu-reading")).to_have_text("20%")
    expect(page.locator(_CHIP)).to_have_attribute("data-pc-health", "")
    expect(_open_sheet(page).locator("[data-row='health']")).to_contain_text("OK")


def test_watch_and_act_draw_a_toned_dot_with_the_reason_and_fix(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    _route_load(page, {"body": _reading("normal", 20.0)}, [])
    _route_health(page, _verdict("act", "Camera Hub holds 4M handles", "Restart Camera Hub"))
    _boot(page, base_url)

    chip = page.locator(_CHIP)
    expect(chip).to_have_attribute("data-pc-health", "danger")
    expect(chip).to_have_text("20%")
    expect(chip).to_have_attribute("aria-label", re.compile(r"PC health: needs action, Camera Hub holds 4M handles"))
    # A dot on the reading's corner: the header line keeps its width.
    head = stable_eval(
        page.locator("#paneClaude > .home-head"),
        "(head) => ({ height: head.getBoundingClientRect().height, overflow: head.scrollWidth - head.clientWidth })",
    )
    assert head["height"] <= 60 and head["overflow"] <= 0, head
    danger = chip.evaluate("el => getComputedStyle(el, '::after').backgroundColor")

    # Tapping the reading opens the PC load sheet on the verdict.
    chip.click()
    body = page.locator("#cpuSheetBody")
    expect(body.locator("[data-row='health']")).to_contain_text("Needs action")
    expect(body.locator("[data-row='health'] .usage-sheet-value")).to_have_attribute("data-tone", "danger")
    expect(body).to_contain_text("Camera Hub holds 4M handles")
    expect(body).to_contain_text("Suggested: Restart Camera Hub")
    page.locator("#cpuSheetDone").click()

    # Watch is its own tone.
    page.unroute(re.compile(r".*/api/system/health$"))
    _route_health(page, _verdict("watch", "Handles are climbing"))
    page.reload(wait_until="domcontentloaded")
    expect(chip).to_have_attribute("data-pc-health", "attention")
    assert chip.evaluate("el => getComputedStyle(el, '::after').backgroundColor") != danger


@pytest.mark.parametrize(
    "body, status, words",
    [
        (_verdict("unknown", "no sample yet", why="hub"), 200, "no sample yet"),
        (_verdict("unknown", why="unreachable"), 200, "Couldn't reach the hub"),
        (_verdict("unknown", why="disabled"), 200, "not set up"),
        ({"detail": "boom"}, 500, "Couldn't reach the launcher"),
    ],
)
def test_a_verdict_the_launcher_cannot_read_is_not_confirmed_never_ok(
    authed_page: Page, base_url: str, body: dict, status: int, words: str
) -> None:
    page = authed_page
    _route_load(page, {"body": _reading("normal", 20.0)}, [])
    _route_health(page, body, status)
    _boot(page, base_url)
    expect(page.locator("#paneClaude > .home-head .cpu-reading")).to_have_text("20%")
    expect(page.locator(_CHIP)).to_have_attribute("data-pc-health", "")
    sheet_body = _open_sheet(page)
    health = sheet_body.locator("[data-row='health']")
    expect(health).to_contain_text("Not confirmed")
    expect(health).not_to_contain_text("OK")
    expect(sheet_body).to_contain_text(words)


@pytest.mark.parametrize("width", [320, 390])
def test_a_long_process_label_wraps_and_the_figure_keeps_its_line(
    authed_page: Page, base_url: str, width: int
) -> None:
    """The sheet's value column never shrinks: a 60-character label takes the
    rest of the row and wraps, and ``0.7 cores`` stays on one line."""
    page = authed_page
    page.set_viewport_size({"width": width, "height": 844})
    reading = _reading("normal", 60.0)
    reading["top"] = [
        {"name": _LONG_LABEL, "cores": 0.7},
        {"name": "node (server)", "cores": 1.8},
    ]
    _route_load(page, {"body": reading}, [])
    _route_health(page, _verdict("ok"))
    _boot(page, base_url)
    body = _open_sheet(page)
    row = body.locator("[data-row='top-0']")
    expect(row).to_contain_text("0.7 cores")

    geometry = stable_eval(
        row,
        """(row) => {
          const label = row.firstElementChild.getBoundingClientRect();
          const value = row.lastElementChild.getBoundingClientRect();
          const probe = document.createElement('span');
          probe.style.cssText = 'position:absolute;visibility:hidden;white-space:nowrap';
          probe.className = row.lastElementChild.className;
          probe.textContent = row.lastElementChild.textContent;
          row.appendChild(probe);
          const oneLine = probe.getBoundingClientRect();
          probe.remove();
          return { labelH: label.height, valueH: value.height, valueW: value.width,
                   oneLineH: oneLine.height, oneLineW: oneLine.width,
                   spill: row.scrollWidth - row.clientWidth,
                   rowRight: row.getBoundingClientRect().right,
                   valueRight: value.right };
        }""",
    )
    assert geometry["valueH"] <= geometry["oneLineH"] + 1, "the figure wrapped"
    assert geometry["valueW"] >= geometry["oneLineW"] - 1, "the figure was squeezed"
    assert geometry["labelH"] > geometry["valueH"], "the long label did not wrap"
    assert geometry["spill"] <= 0, f"the row spills by {geometry['spill']}px"
    assert geometry["valueRight"] <= geometry["rowRight"] + 0.5
