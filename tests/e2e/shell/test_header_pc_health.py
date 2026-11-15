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

pytestmark = pytest.mark.smoke

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


@pytest.mark.iphone
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
    # The machine row keeps one tap-sized line at the phone's width.
    row = stable_eval(
        page.locator(_ROWS[0]),
        "(el) => ({ spill: el.scrollWidth - el.clientWidth, h: el.getBoundingClientRect().height })",
    )
    assert row["spill"] <= 0 and row["h"] >= 44, row

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
    _refetch_health(page)
    expect(chip).to_have_attribute("data-pc-health", "attention")
    assert chip.evaluate("el => getComputedStyle(el, '::after').backgroundColor") != danger


def _assert_a_long_process_label_wraps_and_the_figure_keeps_its_line(page: Page, body) -> None:
    """Formerly ``test_a_long_process_label_wraps_and_the_figure_keeps_its_line``.

    The sheet's value column never shrinks: a 60-character label takes the
    rest of the row and wraps, and ``0.7 cores`` stays on one line. Read at
    two phone widths on the open sheet, which reflows on the resize."""
    row = body.locator("[data-row='top-0']")
    expect(row).to_contain_text("0.7 cores")

    for width in (320, 390):
        page.set_viewport_size({"width": width, "height": 844})
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
        assert geometry["valueH"] <= geometry["oneLineH"] + 1, f"the figure wrapped at {width}px"
        assert geometry["valueW"] >= geometry["oneLineW"] - 1, f"the figure was squeezed at {width}px"
        assert geometry["labelH"] > geometry["valueH"], f"the long label did not wrap at {width}px"
        assert geometry["spill"] <= 0, f"the row spills by {geometry['spill']}px at {width}px"
        assert geometry["valueRight"] <= geometry["rowRight"] + 0.5


_OWNED = {
    "verdict": "watch", "why": None, "reason": "hwinfo-plugin.exe at 349 k handles",
    "fix": "Restart hwinfo-plugin.exe", "owner": "Stream Deck · HWiNFO plugin",
    "restart_id": "streamdeck-hwinfo",
}

_WAVELINK = {
    "verdict": "watch", "why": None, "reason": "WaveLink.exe at 340 k handles",
    "fix": "Restart WaveLink.exe", "owner": "Elgato Wave Link", "restart_id": "wavelink",
}


def _assert_a_restart_that_starts_the_app_again_says_whether_it_came_back(
    page: Page, body, state: dict
) -> None:
    """Formerly ``test_a_restart_that_starts_the_app_again_says_whether_it_came_back``.

    Three restarts from one sheet, one per relaunch outcome, in an order
    where each toast reads differently from the one before it. Its route is
    registered last, so it answers the Wave Link restart ahead of the
    catch-all restart route."""
    relaunches = ["launched", "not confirmed", "supervisor"]
    page.route(
        re.compile(r".*/api/system/restart/wavelink$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"restart_id": "wavelink", "stopped": ["WaveLink.exe"], "errors": [],
                             "relaunch": relaunches.pop(0)}),
        ),
    )
    state["body"] = _WAVELINK
    _refetch_health(page)
    expect(body.locator("[data-row='owner']")).to_contain_text("Elgato Wave Link")
    toast = page.locator("#cpuSheetToast")
    # Only a failure tints; a success toast stays neutral (design.md `toast`).
    error_class = re.compile(r"\berror\b")
    for words, failed in (("Running again", False), ("did not come back", True),
                          ("Running again", False)):
        body.get_by_role("button", name="Restart").click()
        page.locator("#confirmDialogOk").click()
        expect(toast).to_contain_text("Stopped WaveLink.exe")
        expect(toast).to_contain_text(words)
        if failed:
            expect(toast).to_have_class(error_class)
        else:
            expect(toast).not_to_have_class(error_class)
    assert relaunches == [], f"not every restart was posted: {relaunches}"


@pytest.mark.iphone
def test_the_sheet_names_the_owner_and_restarts_only_after_a_confirm(
    authed_page: Page, base_url: str
) -> None:
    """The sheet's owner row and its confirm-gated restart, on one open sheet
    with the long-label row read first and the Wave Link relaunch outcomes
    after it."""
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844})
    reading = _reading("normal", 60.0)
    reading["top"] = [
        {"name": _LONG_LABEL, "cores": 0.7},
        {"name": "node (server)", "cores": 1.8},
    ]
    _route_load(page, {"body": reading}, [])
    posts: list[str] = []
    state = {"body": _OWNED}

    def health(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(state["body"]))

    def restart(route):
        posts.append(route.request.url)
        state["body"] = _verdict("ok")
        route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"restart_id": "streamdeck-hwinfo", "stopped": ["hwinfo-plugin.exe"], "errors": []}),
        )

    page.route(re.compile(r".*/api/system/health$"), health)
    page.route(re.compile(r".*/api/system/restart/.*"), restart)
    _boot(page, base_url)
    body = _open_sheet(page)
    _assert_a_long_process_label_wraps_and_the_figure_keeps_its_line(page, body)

    expect(body.locator("[data-row='owner']")).to_contain_text("Stream Deck · HWiNFO plugin")
    restart_btn = body.get_by_role("button", name="Restart")
    expect(restart_btn).to_be_visible()

    # Cancelling the confirm posts nothing.
    restart_btn.click()
    confirm = page.locator("#confirmDialog")
    expect(confirm).to_be_visible()
    page.locator("#confirmDialogClose").click()
    expect(confirm).to_be_hidden()
    assert posts == []

    # Confirming posts the id alone, then the row is read again.
    restart_btn.click()
    page.locator("#confirmDialogOk").click()
    expect(page.locator("#cpuSheetToast")).to_contain_text("Stopped hwinfo-plugin.exe")
    expect(body.locator("[data-row='health']")).to_contain_text("OK")
    expect(restart_btn).to_have_count(0)
    assert len(posts) == 1 and posts[0].endswith("/api/system/restart/streamdeck-hwinfo")

    _assert_a_restart_that_starts_the_app_again_says_whether_it_came_back(page, body, state)
    assert len(posts) == 1, f"a Wave Link restart reached the hwinfo route: {posts}"


def _refetch_health(page: Page) -> None:
    """Coming back to the page reads the verdict again (``fetchSystemLoad``
    on ``visibilitychange``); an open sheet re-renders from it."""
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")


_ROWS = ("#paneClaude .machine-health", "#paneBoard .machine-health")


def _machine(ports, commit) -> dict:
    return {
        "host": "example-host", "ports_percent": ports, "commit_percent": commit,
        "holders": [
            {"name": "example-app-webapp", "pid": 4242, "value": 18400, "tcp": 18000, "udp": 400},
            {"name": "other.exe", "pid": 977, "value": 9100, "tcp": 9100, "udp": 0},
        ],
        "memory_holders": [], "critical": [],
        "last_event": {"ts": 1_700_000_000, "event_id": 4231, "protocol": "tcp",
                       "reason": "TCP ran out of ephemeral ports"},
    }


def _assert_the_machine_row_reads_each_verdict_and_the_sheet_lists_the_port_holders(
    page: Page, state: dict
) -> None:
    """The Code and Board rows read ok, watch, act and unknown (a verdict
    the page could not read is the words "Not confirmed", never a percent), and
    the sheet lists the top port holders, the last exhaustion event and the hub
    link. Runs on the page the verdict test already loaded."""
    page.locator("#cpuSheetDone").click()
    code = page.locator(_ROWS[0])
    for verdict, machine, words, tone in (
        ("ok", _machine(3.2, 68.5), "OK · Commit 69%", "ok"),
        ("watch", _machine(55.0, 40.0), "Watch · Ports 55%", "attention"),
        ("act", _machine(84.5, 40.0), "Needs action · Ports 85%", "danger"),
        ("unknown", None, "Not confirmed", "none"),
    ):
        state.update(status=200, body={
            "verdict": verdict, "why": "unreachable" if verdict == "unknown" else None,
            "reason": None, "fix": None, "machine": machine, "hub_url": None,
        })
        _refetch_health(page)
        expect(code).to_have_text(re.compile(r"Machine\s*" + re.escape(words)))
        expect(code).to_have_attribute("data-tone", tone)
    assert "%" not in code.inner_text()
    state.update(status=200, body={
        "verdict": "act", "why": None, "reason": "TCP dynamic ports at 85% in use", "fix": None,
        "machine": _machine(84.5, 40.0), "hub_url": "http://127.0.0.1:8000/admin/",
    })
    _refetch_health(page)
    expect(code).to_contain_text("Needs action")
    code.click()
    body = page.locator("#cpuSheetBody")
    expect(body.locator("[data-row='holder-0']")).to_contain_text("example-app-webapp")
    expect(body.locator("[data-row='holder-0']")).to_contain_text("18400 ports")
    expect(body.locator("[data-row='holder-1']")).to_contain_text("other.exe")
    expect(body.locator("[data-row='last-event']")).to_be_visible()
    expect(body).to_contain_text("TCP ran out of ephemeral ports")
    expect(body.get_by_role("link", name=re.compile("Machines tab"))).to_have_attribute(
        "href", "http://127.0.0.1:8000/admin/")
    page.locator("#cpuSheetDone").click()
    # The same row sits under the Board's lane toolbar.
    page.locator("#tabBoard").click()
    board = page.locator(_ROWS[1])
    expect(board).to_be_visible()
    expect(board).to_contain_text("Needs action · Ports 85%")


def test_the_sheet_reads_each_verdict_the_hub_sends(authed_page: Page, base_url: str) -> None:
    """One page load reads every verdict the sheet draws as text alone: the
    health stub's reply changes, the page reads it again, and the open sheet
    redraws. Each step waits on words only its own verdict draws."""
    page = authed_page
    _route_load(page, {"body": _reading("normal", 20.0)}, [])
    state: dict = {"status": 200, "body": _verdict("ok")}
    page.route(
        re.compile(r".*/api/system/health$"),
        lambda route: route.fulfill(
            status=state["status"], content_type="application/json", body=json.dumps(state["body"])
        ),
    )
    _boot(page, base_url)
    chip = page.locator(_CHIP)

    # OK draws no dot, and the sheet says OK.
    expect(chip).to_have_text("20%")
    expect(chip).to_have_attribute("data-pc-health", "")
    sheet_body = _open_sheet(page)
    health = sheet_body.locator("[data-row='health']")
    expect(health).to_contain_text("OK")

    # A verdict the launcher cannot read is Not confirmed, never OK, and
    # draws no dot either.
    for status, body, words in (
        (200, _verdict("unknown", "no sample yet", why="hub"), "no sample yet"),
        (200, _verdict("unknown", why="unreachable"), "Couldn't reach the hub"),
        (200, _verdict("unknown", why="disabled"), "not set up"),
        (500, {"detail": "boom"}, "Couldn't reach the launcher"),
    ):
        state.update(status=status, body=body)
        _refetch_health(page)
        expect(sheet_body).to_contain_text(words)
        expect(health).to_contain_text("Not confirmed")
        expect(health).not_to_contain_text("OK")
        expect(chip).to_have_attribute("data-pc-health", "")

    # An owner without a restart id gets no Restart button: only an
    # allowlisted process does, and the server decides which.
    state.update(status=200, body={**_OWNED, "restart_id": None})
    _refetch_health(page)
    expect(sheet_body.locator("[data-row='owner']")).to_be_visible()
    expect(sheet_body.get_by_role("button", name="Restart")).to_have_count(0)

    # The night restart's own line; one that did not come back says so.
    for outcome, tail in (("launched", ""), ("not confirmed", ", but it did not come back")):
        auto = {"owner": "Elgato Wave Link", "at": "02:14", "outcome": outcome}
        state.update(status=200, body={**_verdict("ok"), "auto_restarted": auto})
        _refetch_health(page)
        note = sheet_body.locator(".cpu-sheet-note", has_text="Restarted automatically")
        expect(note).to_have_text("Restarted automatically at 02:14 · Elgato Wave Link" + tail)

    _assert_the_machine_row_reads_each_verdict_and_the_sheet_lists_the_port_holders(page, state)
