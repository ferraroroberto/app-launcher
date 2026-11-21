"""The hub's PC health verdict beside every header's CPU reading.

The hub judges the machine; the launcher draws a dot on each tab's reading only for ``watch`` or
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


_IP = "192.0.2.44"
# headless WebKit's clipboard permission is not reliable, so writeText is captured.
_CLIPBOARD_MOCK = """
(() => {
  window.__copied = [];
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText: async (t) => { window.__copied.push(t); }, readText: async () => '' },
  });
})()
"""


def _assert_the_ip_row_copies_the_address_and_reads_unknown_without_one(page: Page, ip: dict) -> None:
    """The sheet's IP row: the address from ``/api/status``, a Copy that puts
    exactly that string on the clipboard, and ``Unknown`` with Copy disabled
    once a re-read (the sheet's open) finds no address. Leaves the sheet closed."""
    row = page.locator("#cpuSheetBody [data-row='ip']")
    expect(row.locator(".usage-sheet-value")).to_have_text(_IP)
    copy = row.get_by_role("button", name="Copy")
    expect(copy).to_be_enabled()
    assert row.evaluate("el => el.scrollWidth - el.clientWidth") <= 0, "the IP row spills"
    copy.click()
    expect(page.locator("#cpuSheetToast")).to_contain_text("IP address copied")
    assert page.evaluate("window.__copied") == [_IP]

    ip["value"] = None
    page.locator("#cpuSheetDone").click()
    _open_sheet(page)
    expect(row.locator(".usage-sheet-value")).to_have_text("Unknown")
    expect(row.get_by_role("button", name="Copy")).to_be_disabled()
    page.locator("#cpuSheetDone").click()


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
    ip = {"value": _IP}
    page.add_init_script(_CLIPBOARD_MOCK)

    def status(route):
        payload = route.fetch().json()
        payload["private_ip"] = ip["value"]
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route(re.compile(r".*/api/status$"), status)
    _boot(page, base_url)
    body = _open_sheet(page)
    _assert_a_long_process_label_wraps_and_the_figure_keeps_its_line(page, body)
    _assert_the_ip_row_copies_the_address_and_reads_unknown_without_one(page, ip)
    body = _open_sheet(page)

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


def _assert_the_dot_rides_every_tab_and_the_sheet_lists_the_port_holders(
    page: Page, state: dict
) -> None:
    """No Machine card sits on the Code or Board tab. The verdict is a dot on
    every header's reading: drawn for ``watch`` and ``act``, never for ``ok``
    or a verdict the page could not read ("Not confirmed" stays a sheet state).
    The sheet lists the port holders, the last exhaustion event and the hub
    link. Runs on the page the verdict test already loaded."""
    page.locator("#cpuSheetDone").click()
    expect(page.locator(".machine-health")).to_have_count(0)
    readings = page.locator(".cpu-reading")
    total = readings.count()
    assert total >= 5, f"expected a reading in every pane's header, found {total}"
    for verdict, machine, dot in (
        ("ok", _machine(3.2, 68.5), ""),
        ("watch", _machine(55.0, 40.0), "attention"),
        ("act", _machine(84.5, 40.0), "danger"),
        ("unknown", None, ""),
    ):
        state.update(status=200, body={
            "verdict": verdict, "why": "unreachable" if verdict == "unknown" else None,
            "reason": None, "fix": None, "machine": machine, "hub_url": None,
        })
        _refetch_health(page)
        expect(page.locator(f".cpu-reading[data-pc-health='{dot}']")).to_have_count(total)
    state.update(status=200, body={
        "verdict": "act", "why": None, "reason": "TCP dynamic ports at 85% in use", "fix": None,
        "machine": _machine(84.5, 40.0), "hub_url": "http://127.0.0.1:8000/admin/",
    })
    _refetch_health(page)
    # The Board's own reading carries the dot and opens the same sheet.
    page.locator("#tabBoard").click()
    expect(page.locator("#paneBoard")).to_be_visible()
    expect(page.locator(".machine-health")).to_have_count(0)
    board = page.locator("#paneBoard > .home-head .cpu-reading")
    expect(board).to_have_attribute("data-pc-health", "danger")
    board.click()
    body = page.locator("#cpuSheetBody")
    expect(body.locator("[data-row='ports']")).to_contain_text("85%")
    expect(body.locator("[data-row='commit']")).to_contain_text("40%")
    expect(body.locator("[data-row='holder-0']")).to_contain_text("example-app-webapp")
    expect(body.locator("[data-row='holder-0']")).to_contain_text("18400 ports")
    expect(body.locator("[data-row='holder-1']")).to_contain_text("other.exe")
    expect(body.locator("[data-row='last-event']")).to_be_visible()
    expect(body).to_contain_text("TCP ran out of ephemeral ports")
    expect(body.get_by_role("link", name=re.compile("Machines tab"))).to_have_attribute(
        "href", "http://127.0.0.1:8000/admin/")
    page.locator("#cpuSheetDone").click()


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

    _assert_the_dot_rides_every_tab_and_the_sheet_lists_the_port_holders(page, state)
    _assert_the_sheet_shows_each_disk_and_an_older_hub_shows_none(page, state)


def _assert_the_sheet_shows_each_disk_and_an_older_hub_shows_none(page: Page, state: dict) -> None:
    """One row per disk, labelled by drive letter, with busy percent, queue and
    latency, toned by the hub's state; a disk the hub could not judge says so,
    and a hub that sends no disks leaves the sheet as it was."""
    disks = [
        {"disk": "d2", "drives": ["C:"], "state": "alert", "busy_percent": 98.0,
         "queue": 7.5, "read_ms": 120.4, "write_ms": 310.0},
        {"disk": "d0", "drives": ["D:", "F:"], "state": "watch", "busy_percent": 82.0,
         "queue": 2.0, "read_ms": None, "write_ms": 40.0},
        {"disk": "d1", "drives": ["E:"], "state": "unknown", "busy_percent": None,
         "queue": None, "read_ms": None, "write_ms": None},
    ]
    machine = {**_machine(3.2, 40.0), "disks": disks}
    state.update(status=200, body={
        "verdict": "act", "why": None, "reason": "Disk C: is overloaded", "fix": None,
        "machine": machine, "hub_url": None,
    })
    _refetch_health(page)
    page.locator("#paneBoard > .home-head .cpu-reading").click()
    body = page.locator("#cpuSheetBody")
    first = body.locator("[data-row='disk-0']")
    expect(first).to_contain_text("C:")
    expect(first).to_contain_text("98% busy")
    expect(first).to_contain_text("queue 7.5")
    expect(body.locator("[data-row='disk-0-latency']")).to_have_text("read 120 ms · write 310 ms")
    expect(first.locator("strong")).to_have_attribute("data-tone", "danger")
    second = body.locator("[data-row='disk-1']")
    expect(second).to_contain_text("D:, F:")
    expect(body.locator("[data-row='disk-1-latency']")).to_have_text("write 40 ms")
    expect(second.locator("strong")).to_have_attribute("data-tone", "attention")
    third = body.locator("[data-row='disk-2']")
    expect(third).to_contain_text("Not confirmed")
    expect(third.locator("strong")).not_to_have_attribute("data-tone", re.compile("."))
    expect(body.locator("[data-row='disk-2-latency']")).to_have_count(0)
    state.update(status=200, body={
        "verdict": "ok", "why": None, "reason": None, "fix": None,
        "machine": _machine(3.2, 40.0), "hub_url": None,
    })
    _refetch_health(page)
    expect(body.locator("[data-row^='disk-']")).to_have_count(0)
    expect(body.locator("[data-row='ports']")).to_be_visible()
    page.locator("#cpuSheetDone").click()
