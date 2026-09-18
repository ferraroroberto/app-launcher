"""Regression pin for app-launcher#1007 (Jobs row: handlers went stale).

Every per-row action handler in ``jobs-row.js`` closed over the ``job``
object captured when the row was built. The 4s poll then replaces every job
object (``jobs.js``: ``state.jobs = body.jobs || []``) and, when sort order
is unchanged, **reuses the existing ``<li>``**: ``patchRowsInPlace`` →
``patchRowNodes`` patches only the status dot, the meta line, the run
button's rendered state and the chips. The buttons and their listeners are
never rebuilt.

So a job whose ``confirm`` / ``paused`` / ``schedule`` / ``params`` changed
server-side without changing sort order — from another device, a chain
action, an auto-pause — rendered fresh while its handlers still acted on the
stale copy. The two consequences this pins:

* a job newly flagged **"Require confirmation"** ran with **no dialog**,
  because ``runJobNow`` reads ``job.confirm`` off the stale object (and the
  request then omits ``?confirmed=1``, so the server gate is not exercised
  either);
* **Edit** reopened with the stale schedule/params, so Save could silently
  clobber the concurrent change.

Deliberately driven through a **real poll cycle** rather than a hand-built
job object: the bug only exists because of what the poll does to a reused
row, so a test that calls the handler directly would pass against the broken
code. The row's identity is tagged before the flip and re-checked after, to
prove the in-place patch path ran and not a full re-render (which would
rebuild the listeners and hide the bug).
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

_JOBS_POLL_MS = 4000


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


def test_run_handler_sees_a_confirm_flag_set_after_the_row_was_built(
    authed_page: Page, base_url: str
) -> None:
    runs: list = []
    dialogs: list = []
    state = {"confirm": False, "args": "", "polls": 0}
    _wire(authed_page, runs, state)

    def _accept(dialog) -> None:
        dialogs.append(dialog.message)
        dialog.accept()

    authed_page.on("dialog", _accept)

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#sessionsList", state="attached", timeout=5_000)
    authed_page.locator("#tabJobs").click()

    row = authed_page.locator("#jobsList li.app-item[data-id='demo']")
    expect(row).to_be_visible()
    run_btn = row.locator("[data-role='run-btn']")
    expect(run_btn).to_have_count(1)

    # Tag this exact <li> so we can prove the poll reused it.
    row.evaluate("el => { el.dataset.pinTag = 'original'; }")

    # Flip the flag server-side, the way another device would, and let a real
    # poll deliver it. Sort order is unchanged, so the row is patched in place.
    state["confirm"] = True
    polls_before = state["polls"]
    # Two further polls: one to deliver the flip, one to be sure it settled.
    authed_page.wait_for_timeout(_JOBS_POLL_MS * 2 + 1_000)
    assert state["polls"] > polls_before, "the jobs poll never ran"

    # The row survived the poll — the in-place patch path ran, which is the
    # only path where the bug exists.
    expect(row).to_have_attribute("data-pin-tag", "original")

    run_btn.click()

    assert dialogs, (
        "tapping Run raised no confirm() dialog: the handler acted on the "
        "job captured when the row was built, not the polled one that "
        "carries confirm=true (#1007)"
    )
    assert "requires confirmation" in dialogs[0], dialogs[0]
    assert runs, "no run request was sent"
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
    # The edit/dry-run/remove buttons only render in Edit mode.
    authed_page.add_init_script("localStorage.setItem('launcher.editMode', '1')")

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#sessionsList", state="attached", timeout=5_000)
    authed_page.locator("#tabJobs").click()

    row = authed_page.locator("#jobsList li.app-item[data-id='demo']")
    expect(row).to_be_visible()
    edit_btn = row.locator("button[aria-label='Edit']")
    expect(edit_btn).to_have_count(1)
    row.evaluate("el => { el.dataset.pinTag = 'original'; }")

    # Another device edits the job's args; a real poll delivers it.
    state["args"] = "--new"
    polls_before = state["polls"]
    authed_page.wait_for_timeout(_JOBS_POLL_MS * 2 + 1_000)
    assert state["polls"] > polls_before, "the jobs poll never ran"
    expect(row).to_have_attribute("data-pin-tag", "original")

    edit_btn.click()
    args_input = authed_page.locator("#jobArgsInput")
    expect(args_input).to_be_visible()
    assert args_input.input_value() == "--new", (
        f"Edit opened with args {args_input.input_value()!r}; the polled job "
        "carries '--new'. Saving would have clobbered the concurrent change "
        "(#1007)."
    )
