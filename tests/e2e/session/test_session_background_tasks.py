"""A session's ⋮ → Background tasks.

What a Claude session launched with ``run_in_background`` or ``Monitor``,
folded from its transcript server-side: Running first with its elapsed time,
then Finished N with the status Claude Code reported; a row opens the task's
output tail. Pinned against stubbed routes (every name and line synthetic):

* the item is in both ⋮ menus (the Coding tab's row, the session view's) for
  a Claude session and absent for any other agent;
* the sheet lists Running then Finished, a failed task carries a chip, and a
  row expands to its summary and output tail through ``?task=<id>``;
* a session with no tasks says so, and an unreadable transcript has its own
  sentence — never a blank sheet;
* the sheet fits 390px with 44px rows and nothing scrolls sideways.
"""
from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.ui import open_row_menu
from tests.e2e.fixtures.waits import OVERLAY_OPEN_MS, stable_read
from tests.e2e.session.test_session_mode_toggle import (
    _mock_git_status,
    _mock_sessions_list,
    _row,
    _session_row,
)

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_SID = "sid-background-tasks-1955"
_TRANSCRIPT = {
    "available": True, "source": "native", "reason": None, "session_id": _SID,
    "next_cursor": None, "tool_errors": "reported",
    "entries": [
        {"kind": "user", "timestamp": "2026-10-01T10:00:00Z", "text": "run the build",
         "truncated": False, "sidechain": False},
    ],
}


def _task(task_id: str, label: str, status: str, **extra) -> dict:
    return {
        "id": task_id, "kind": "bash", "label": label, "command": "", "status": status,
        "started_at": "2026-10-01T10:00:00Z", "finished_at": None, "elapsed_seconds": 125,
        "summary": "", "has_output": True, **extra,
    }


_TASKS = {
    "available": True, "reason": None, "session_id": _SID,
    "running": [
        _task("bRUN1", "Serve the docs", "running", command="python -m http.server 9000"),
        _task("bRUN2", "Watch the build", "running", kind="monitor", elapsed_seconds=40, has_output=False),
    ],
    "finished": [
        _task("bDONE", "Run the unit tests", "completed", finished_at="2026-10-01T10:02:05Z",
              summary='Background command "pytest -q" completed (exit code 0)'),
        _task("bFAIL", "Build the bundle with a deliberately long description " * 2, "failed",
              finished_at="2026-10-01T10:01:00Z", elapsed_seconds=60, summary="exit code 2"),
    ],
    "finished_total": 2,
}
_OUTPUT = {
    "available": True, "reason": None, "session_id": _SID, "task": "bRUN1",
    "text": "Serving HTTP on :: port 9000\nGET / 200\n", "size": 40, "truncated": False,
}
_EMPTY = {"available": True, "reason": None, "session_id": _SID,
          "running": [], "finished": [], "finished_total": 0}
_UNREADABLE = {"available": False, "reason": "no_transcript", "session_id": _SID,
               "entries": [], "next_cursor": None, "source": None}


def _mock(page: Page, *, agent: str = "claude", tasks: dict = _TASKS) -> list:
    """Stub the session list, its transcript and the tasks route; returns the
    list the route's request URLs land in."""
    _mock_git_status(page)
    _mock_sessions_list(page, [_session_row(_SID, kind="remote", agent=agent, title="Tasks demo")])
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/transcript(\?.*)?$"),
        lambda route: route.fulfill(status=200, content_type="application/json",
                                    body=_json.dumps(_TRANSCRIPT)),
    )
    urls: list = []

    def _tasks(route):
        urls.append(route.request.url)
        body = _OUTPUT if "task=" in route.request.url else tasks
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(body))

    page.route(
        re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/background-tasks(\?.*)?$"),
        _tasks,
    )
    return urls


def _open_session(page: Page, base_url: str) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base_url, wait_until="domcontentloaded")
    _row(page, _SID).locator(".session-open").click()
    expect(page.locator("#terminalOverlay")).to_be_visible(timeout=OVERLAY_OPEN_MS)


_FIT = """
(() => {
  const sheet = document.getElementById('bgTasksSheet');
  const rows = [...sheet.querySelectorAll('.bg-task-head')];
  if (!rows.length || !rows[0].getBoundingClientRect().height) return null;
  const de = document.documentElement;
  const body = document.getElementById('bgTasksBody');
  const card = sheet.querySelector('.detail-card');
  return {minRow: Math.min(...rows.map(x => x.getBoundingClientRect().height)),
          body: body.scrollWidth - body.clientWidth, card: card.scrollWidth - card.clientWidth,
          doc: de.scrollWidth - de.clientWidth};
})()
"""


def test_background_tasks_lists_running_then_finished_and_opens_an_output(
    authed_page: Page, base_url: str
) -> None:
    urls = _mock(authed_page)
    _open_session(authed_page, base_url)
    authed_page.locator("#terminalMenu").click()
    authed_page.locator(".terminal-menu").get_by_role("menuitem", name="Background tasks").click()

    sheet = authed_page.locator("#bgTasksSheet")
    expect(sheet).to_be_visible()
    groups = sheet.locator(".bg-tasks-group")
    expect(groups.locator(".bg-tasks-group-title")).to_have_text(["Running", "Finished 2"])
    running = groups.nth(0).locator(".bg-task")
    expect(running).to_have_count(2)
    expect(running.first.locator(".bg-task-title")).to_have_text("Serve the docs")
    expect(running.first.locator(".bg-task-meta")).to_have_text("Running 2m")
    finished = groups.nth(1).locator(".bg-task")
    expect(finished.nth(0).locator(".bg-task-meta")).to_have_text("completed · took 2m")
    # Only an exception earns a chip: the failed task has one, the completed one none.
    expect(finished.nth(0).locator(".chip")).to_have_count(0)
    expect(finished.nth(1).locator(".chip")).to_have_text("failed")
    assert len(urls) == 1 and "task=" not in urls[0], urls

    fit = stable_read(lambda: authed_page.evaluate(_FIT))
    assert fit["minRow"] >= 44, f"a task row is under the 44px floor: {fit}"
    assert fit["body"] <= 0 and fit["card"] <= 0 and fit["doc"] <= 0, f"sideways scroll at 390px: {fit}"

    # A row opens its command and output tail, read through ?task=<id>, once.
    running.first.locator("summary").click()
    expect(running.first.locator(".bg-task-command")).to_have_text("python -m http.server 9000")
    expect(running.first.locator(".bg-task-output")).to_have_text(_OUTPUT["text"])
    assert len(urls) == 2 and "task=bRUN1" in urls[1], urls
    running.first.locator("summary").click()
    running.first.locator("summary").click()
    expect(running.first.locator(".bg-task-output")).to_have_count(1)
    assert len(urls) == 2, urls

    # A task with no output file says so rather than asking for one.
    running.nth(1).locator("summary").click()
    expect(running.nth(1).locator(".bg-task-note")).to_have_text("No output was recorded for this task")
    assert len(urls) == 2, urls

    # Done lands back on the session view it opened over.
    authed_page.locator("#bgTasksDone").click()
    expect(sheet).to_be_hidden()
    expect(authed_page.locator("#terminalOverlay")).to_be_visible()


def test_background_tasks_empty_and_unreadable_states(authed_page: Page, base_url: str) -> None:
    _mock(authed_page, tasks=_EMPTY)
    _open_session(authed_page, base_url)
    authed_page.locator("#terminalMenu").click()
    authed_page.locator(".terminal-menu").get_by_role("menuitem", name="Background tasks").click()
    state = authed_page.locator("#bgTasksState")
    expect(state).to_have_text("No background tasks")
    expect(authed_page.locator("#bgTasksBody .bg-task")).to_have_count(0)
    authed_page.locator("#bgTasksClose").click()
    expect(authed_page.locator("#bgTasksSheet")).to_be_hidden()

    authed_page.unroute(re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/background-tasks(\?.*)?$"))
    authed_page.route(
        re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/background-tasks(\?.*)?$"),
        lambda route: route.fulfill(status=200, content_type="application/json",
                                    body=_json.dumps(_UNREADABLE)),
    )
    authed_page.locator("#terminalMenu").click()
    authed_page.locator(".terminal-menu").get_by_role("menuitem", name="Background tasks").click()
    expect(state).to_have_text("This session has no transcript to read yet")


def test_the_item_is_in_the_row_menu_for_claude_only(authed_page: Page, base_url: str) -> None:
    _mock(authed_page)
    authed_page.set_viewport_size({"width": 390, "height": 844})
    authed_page.goto(base_url, wait_until="domcontentloaded")
    _row(authed_page, _SID).locator(".session-kebab").click()
    open_row_menu(authed_page).locator("[data-item=session-tasks-btn]").click()
    expect(authed_page.locator("#bgTasksSheet")).to_be_visible()
    expect(authed_page.locator("#bgTasksBody .bg-task")).to_have_count(4)
    authed_page.locator("#bgTasksDone").click()


def test_another_agent_has_no_item_in_either_menu(authed_page: Page, base_url: str) -> None:
    _mock(authed_page, agent="codex")
    authed_page.set_viewport_size({"width": 390, "height": 844})
    authed_page.goto(base_url, wait_until="domcontentloaded")
    _row(authed_page, _SID).locator(".session-kebab").click()
    menu = open_row_menu(authed_page)
    expect(menu.locator("[data-item=session-chat-btn]")).to_have_count(1)
    expect(menu.locator("[data-item=session-tasks-btn]")).to_have_count(0)
    authed_page.keyboard.press("Escape")

    _row(authed_page, _SID).locator(".session-open").click()
    expect(authed_page.locator("#terminalOverlay")).to_be_visible(timeout=OVERLAY_OPEN_MS)
    authed_page.locator("#terminalMenu").click()
    menu = authed_page.locator(".terminal-menu")
    expect(menu.get_by_role("menuitem", name="Rename")).to_be_visible()
    expect(menu.get_by_role("menuitem", name="Background tasks")).to_have_count(0)
