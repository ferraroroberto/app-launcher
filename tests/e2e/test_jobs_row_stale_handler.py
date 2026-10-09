"""Regression pin for app-launcher#1007 (Jobs row: handlers went stale).

Every per-row action handler in ``jobs-row.js`` closed over the ``job``
object captured when the row was built. The 4s poll then replaced every job
object (``jobs.js``: ``state.jobs = body.jobs || []``) and, when sort order
was unchanged, **reused the existing ``<li>``**: the buttons and their
listeners were never rebuilt.

So a job whose ``confirm`` / ``paused`` / ``schedule`` / ``params`` changed
server-side without changing sort order — from another device, a chain
action, an auto-pause — rendered fresh while its handlers still acted on the
stale copy. The two consequences this pins:

* a job newly flagged **"Require confirmation"** ran with **no confirmation**,
  because ``runJobNow`` reads ``job.confirm`` off the stale object (and the
  request then omits ``?confirmed=1``, so the server gate is not exercised
  either);
* **Edit** reopened with the stale schedule/params, so Save could silently
  clobber the concurrent change.

Since #1438 the bug is fixed by construction: the poll re-renders every row
from the fresh ``/api/jobs`` payload (no in-place patching) and the actions
are the kebab menu's ``.job-run-item`` / ``.job-edit-item`` (there is no
per-row Run button and no Edit mode). The confirmation is the vendored
``#confirmDialog`` sheet (``#confirmDialogOk``), not a native ``confirm()``.
The pin is kept behaviourally: the kebab is opened **before** the change
arrives, the menu reopens across the poll's re-render, and the item then
acts on the polled job.

Deliberately driven through a **real poll cycle** rather than a hand-built
job object: a test that calls the handler directly would pass against the
broken code.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import wait_until

pytestmark = pytest.mark.smoke

# Ceiling, not a sleep: the wait returns as soon as the polls land. It must
# cover a slow boot as well as the interval itself — main.js registers the
# poll's setInterval only after every boot fetch has settled (run
# concurrently since #1258, the slowest one sets the pace), and the test
# clicks the Jobs tab long before that finishes (#1138).
_POLL_WAIT_BUDGET_MS = 30_000


def _job(*, confirm: bool = False, args: str = "") -> dict:
    return {
        "id": "demo",
        "name": "Demo",
        "script_path": "E:/demo/run.py",
        "kind": "",
        "target_kind": "py",
        "schedule_chip": "",
        "next_run": None,
        "running": False,
        "stuck": False,
        "args": args,
        "confirm": confirm,
        "schedule": {"type": "none"},
        "params": [],
        "last_run": None,
        "stats": {
            "p50": None, "p95": None, "success_rate_30d": None,
            "completed_count": 0, "last7": [],
        },
    }


def _wire(page: Page, runs: list, state: dict) -> None:
    def _fulfill(route, body: dict) -> None:
        route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)
        )

    def _jobs(route) -> None:
        state["polls"] += 1
        _fulfill(route, {"jobs": [
            _job(confirm=state["confirm"], args=state["args"])
        ]})

    def _run(route) -> None:
        runs.append(route.request.url)
        _fulfill(route, {"started": True, "run_id": "r1"})

    page.route(re.compile(r".*/api/jobs/demo/run(\?.*)?$"), _run)
    page.route(re.compile(r".*/api/jobs(\?.*)?$"), _jobs)
    # A boot fetch that never answers (git-status, a fleet-wide scan) must
    # not hold the Jobs poll back: each poll arms on its own first fetch,
    # and Jobs has none at boot (#1258).
    page.route(re.compile(r".*/api/claude-code/git-status$"), lambda route: None)


def _wait_for_polls(page: Page, state: dict, target: int) -> None:
    """Wait until the mocked ``/api/jobs`` route has been hit ``target`` times.

    A bounded wait on the observable fact, not a fixed sleep (#1138): the
    old ``wait_for_timeout(2 * poll + 1s)`` missed the first poll whenever a
    slow WebKit boot delayed the interval's registration. ``page`` pumps
    Playwright's event loop, which is what runs the route handler that
    counts — a bare ``time.sleep`` would never see the count move.
    """
    waited = 0
    while state["polls"] < target and waited < _POLL_WAIT_BUDGET_MS:
        page.wait_for_timeout(250)
        waited += 250
    assert state["polls"] >= target, (
        f"the jobs poll never ran: {state['polls']} /api/jobs hits after "
        f"{_POLL_WAIT_BUDGET_MS} ms, wanted {target}"
    )


def _open_kebab(row) -> None:
    """Open the row's ⋯ menu; the menu reopens across the poll's re-render."""
    row.locator("button.job-menu-anchor").click()
    expect(row.locator(".job-run-item")).to_be_visible()


def test_run_handler_sees_a_confirm_flag_set_after_the_row_was_built(
    authed_page: Page, base_url: str
) -> None:
    runs: list = []
    state = {"confirm": False, "args": "", "polls": 0}
    _wire(authed_page, runs, state)

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#sessionsList", state="attached", timeout=5_000)
    authed_page.locator("#tabJobs").click()

    row = authed_page.locator("#jobsList li.job-row[data-id='demo']")
    expect(row).to_be_visible()
    # Open the menu while the job is not yet confirm-flagged.
    _open_kebab(row)

    # Flip the flag server-side, the way another device would, and let a real
    # poll deliver it. Two further polls: one to deliver the flip, one to be
    # sure it settled.
    state["confirm"] = True
    _wait_for_polls(authed_page, state, state["polls"] + 2)

    # The menu is still (or again) open after the re-render.
    run_item = row.locator(".job-run-item")
    expect(run_item).to_be_visible()
    run_item.click()

    confirm = authed_page.locator("#confirmDialog")
    expect(confirm).to_be_visible()
    expect(authed_page.locator("#confirmDialogMessage")).to_contain_text(
        "requires confirmation"
    )
    assert not runs, "the run was sent before the confirmation was accepted"
    authed_page.locator("#confirmDialogOk").click()

    wait_until(authed_page, lambda: bool(runs), "the run request")
    assert "confirmed=1" in runs[0], (
        f"run request {runs[0]!r} omitted ?confirmed=1 — the stale job "
        "decided the client-side gate, so the server gate was not exercised"
    )


def test_edit_handler_opens_the_polled_job_not_the_one_captured_at_render(
    authed_page: Page, base_url: str
) -> None:
    """The second consequence of the same stale closure (#1007): Edit
    reopened with the schedule/params captured when the row was built, so
    saving silently clobbered whatever had changed server-side meanwhile."""
    runs: list = []
    state = {"confirm": False, "args": "--old", "polls": 0}
    _wire(authed_page, runs, state)

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#sessionsList", state="attached", timeout=5_000)
    authed_page.locator("#tabJobs").click()

    row = authed_page.locator("#jobsList li.job-row[data-id='demo']")
    expect(row).to_be_visible()
    # Edit lives in the row's ⋯ menu; open it before the change arrives.
    _open_kebab(row)

    # Another device edits the job's args; a real poll delivers it.
    state["args"] = "--new"
    _wait_for_polls(authed_page, state, state["polls"] + 2)

    edit_btn = row.locator(".job-edit-item")
    expect(edit_btn).to_be_visible()
    edit_btn.click()
    args_input = authed_page.locator("#jobArgsInput")
    expect(args_input).to_be_visible()
    assert args_input.input_value() == "--new", (
        f"Edit opened with args {args_input.input_value()!r}; the polled job "
        "carries '--new'. Saving would have clobbered the concurrent change "
        "(#1007)."
    )
