"""Smoke tests for the launcher webapp (issue #22).

Tight by design: ~6 checks that catch the bugs we actually hit (JS
exceptions on boot, empty config form, broken tab switch, wrong stop
buttons per session kind, sign-in sheet not opening). Expand
iteratively in follow-up issues; do NOT turn this file into a regression
net for every feature.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._surface import surface
from tests.e2e.fixtures.ui import open_agent_sheet, open_row_menu

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]


def _navigate_collecting_errors(page: Page, base_url: str) -> list[str]:
    """Open the SPA and capture any uncaught JS errors during boot."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    # #sessionsList is rendered server-side in index.html; waiting for it
    # confirms the static document parsed without an early script crash.
    page.wait_for_selector("#sessionsList", state="attached", timeout=5_000)
    return errors


_HIDE = """() => {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
  document.dispatchEvent(new Event('visibilitychange'));
}"""


def _assert_sessions_panel_renders(page: Page) -> None:
    """Formerly ``test_sessions_panel_renders``.

    The panel is valid when it has rows OR the empty-state paragraph (with
    its copy) is showing. Both halves are evaluated atomically in one retried
    wait_for_function, so the 5 s fetchSessions poll re-rendering between a
    row count and the empty-state read can never split the check (#39)."""
    expect(page.locator("#sessionsList")).to_be_attached()
    page.wait_for_function(
        """() => {
          const list = document.getElementById('sessionsList');
          const empty = document.getElementById('sessionsEmpty');
          if (!list || !empty) return false;
          if (list.querySelectorAll('li.session-item').length > 0) return true;
          const msg = empty.querySelector('.empty-state-message');
          return !empty.hidden && !!msg &&
            msg.textContent.trim() === 'No sessions launched from here yet.';
        }""",
        timeout=10_000,
    )


def _assert_tabs_switch(page: Page) -> None:
    """Formerly ``test_tabs_switch``: each tab shows its own pane and hides
    the others, and the active tab carries ``active``."""
    pane_claude = page.locator("#paneClaude")
    pane_apps = page.locator("#paneApps")
    pane_jobs = page.locator("#paneJobs")

    expect(pane_claude).to_be_visible()
    expect(pane_apps).to_be_hidden()
    expect(pane_jobs).to_be_hidden()

    page.locator("#tabApps").click()
    expect(pane_apps).to_be_visible()
    expect(pane_claude).to_be_hidden()
    expect(pane_jobs).to_be_hidden()
    # Substring match on className so future class reorders don't false-fail.
    expect(page.locator("#tabApps")).to_have_class(re.compile(r"\bactive\b"))

    page.locator("#tabJobs").click()
    expect(pane_jobs).to_be_visible()
    expect(pane_claude).to_be_hidden()
    expect(pane_apps).to_be_hidden()
    expect(page.locator("#tabJobs")).to_have_class(re.compile(r"\bactive\b"))

    page.locator("#tabClaude").click()
    expect(pane_claude).to_be_visible()
    expect(pane_apps).to_be_hidden()
    expect(pane_jobs).to_be_hidden()


def _assert_load_posts_a_row_naming_the_last_boot_request(page: Page) -> None:
    """Formerly ``test_perf_timing.py::test_load_posts_a_row_naming_the_last_boot_request``.

    ``static/perf-timing.js`` records the load once boot's fetches have
    settled and sends it when the page hides. The row carries the document
    legs and names the boot request that landed last, which is the leg a slow
    relaunch is read by (``docs/perf-timings.md``). Last, because it hides
    the page."""
    # Boot's fan-out has settled once the network is idle; the load row is
    # queued synchronously right after it.
    page.wait_for_load_state("networkidle")
    with page.expect_request(re.compile(r".*/api/perf/timings$")) as sent:
        page.evaluate(_HIDE)
    batch = json.loads(sent.value.post_data or "{}")
    rows = [row for row in batch.get("rows", []) if row.get("kind") == "load"]
    assert len(rows) == 1, f"expected one load row, got {batch!r}"
    row = rows[0]
    assert row["last_path"].startswith("/api/"), row
    assert "?" not in row["last_path"], row
    assert row["boot"] is not None and row["boot"] > 0, row
    assert row["ttfb"] is not None, row
    assert batch["device_class"] in {"phone", "tablet", "desktop"}


def test_page_loads_without_console_errors(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    """Boot with no JS error, the sessions panel, the tab switch and (on
    Chromium, where it always ran) the load's timing row, on one page load."""
    authed_page.route(
        re.compile(r".*/api/perf/timings$"),
        lambda route: route.fulfill(status=200, content_type="application/json", body='{"accepted": 1}'),
    )
    errors = _navigate_collecting_errors(authed_page, base_url)
    # Give the boot script a beat to settle: fetchConfig, sessions poll,
    # listeners poll. Anything thrown during that fans out as pageerror.
    authed_page.wait_for_timeout(500)
    assert errors == [], f"JS errors during boot:\n  - " + "\n  - ".join(errors)
    if base_url.startswith("https") and browser_name == "chromium":
        # A cold boot is ~130 requests; over HTTP/1.1 they queue behind six
        # connections, so the page must arrive over HTTP/2.
        protocol = authed_page.evaluate(
            "performance.getEntriesByType('navigation')[0].nextHopProtocol"
        )
        assert protocol == "h2", f"page served over {protocol!r}, not HTTP/2"

    _assert_sessions_panel_renders(authed_page)
    _assert_tabs_switch(authed_page)
    if browser_name == "chromium":
        _assert_load_posts_a_row_naming_the_last_boot_request(authed_page)


def test_coding_options_populated(authed_page: Page, base_url: str) -> None:
    # The first /api/config request fails at the network layer, the way a
    # loopback socket error on a loaded box (#1243) or a dropped connection on
    # the phone does. boot() used to stop there for good, so nothing that
    # config feeds (these options, the session list, every poll) ever
    # rendered (issue #1230). It must retry and populate the options anyway.
    config_calls: list = []

    def _fail_first_config(route) -> None:
        config_calls.append(route.request.url)
        if len(config_calls) == 1:
            route.abort("failed")
        else:
            route.continue_()

    authed_page.route(re.compile(r".*/api/config$"), _fail_first_config)
    _navigate_collecting_errors(authed_page, base_url)
    # The agent options live in Settings > Launch defaults > Claude Code
    # (#1435); the controls are only visible once that sheet is open.
    # renderClaudeOptions() runs after /api/config resolves regardless.
    open_agent_sheet(authed_page, "claude")
    authed_page.wait_for_function(
        "() => document.querySelectorAll('#claudeModelMenu > [role=option]').length > 0",
        timeout=5_000,
    )
    # Effort is a select-native and Permission a vendored segmented control
    # (#1133, #1586): six effort levels pass segmented's five-option cap.
    authed_page.wait_for_selector("select#claudeEffort > option", state="attached", timeout=5_000)
    authed_page.wait_for_selector("#claudePermission > button.segmented-item", timeout=5_000)
    model_count = authed_page.locator("#claudeModelMenu > [role='option']").count()
    effort_count = authed_page.locator("select#claudeEffort > option").count()
    perm_count = authed_page.locator("#claudePermission > button.segmented-item").count()
    assert model_count >= 1, f"#claudeModel rendered no buttons (got {model_count})"
    assert effort_count >= 1, f"#claudeEffort rendered no options (got {effort_count})"
    assert perm_count == 2, f"#claudePermission expected 2 segmented items (got {perm_count})"
    assert len(config_calls) >= 2, f"boot never retried /api/config: {config_calls}"


def test_pty_session_renders_with_both_stop_buttons(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """End-to-end: the launched PTY session shows up with both stop buttons.

    The `launched_pty_session` fixture POSTs /api/apps/.../launch (mode=pty)
    before the test and force-kills the session in teardown — so this test
    no longer depends on the user having something running.

    Every session row (if any) is checked for the same shape: no action rail,
    no gear, no stop button, a visible › chevron and a tappable row button
    (#1025). The launched session must be among the full-control rows, which
    carry no kind chip since #1433 (only a detached row says what it is).
    """
    _navigate_collecting_errors(authed_page, base_url)
    # The session was launched before navigation, so boot()'s initial
    # fetchSessions should already include it; the SPA also re-polls on a
    # 5 s timer. Wait out one poll cycle as a fallback (no manual refresh
    # button to force an immediate fetch any more).
    pty_rows = authed_page.locator(
        '#sessionsList li.session-item:has(.session-open[data-kind="pty"])'
    )
    expect(pty_rows.first).to_be_visible(timeout=8_000)

    rows = authed_page.locator("#sessionsList li.session-item")
    count = rows.count()
    assert count >= 1, "fixture launched a session but #sessionsList is empty"

    saw_pty = False
    for i in range(count):
        row = rows.nth(i)
        kind = row.locator(".session-open").get_attribute("data-kind") or ""
        # #1025: the row's one control is the kebab, which opens its action
        # menu. No gear glyph, no chevron, and no Stop button loose on the
        # row itself — Stop lives inside the menu. The stop path itself is
        # pinned by test_stop_unify_and_terminal_kill and test_coding_chief;
        # this is the row's shape.
        assert row.locator(".session-kebab").count() == 1, (
            f"row {i} ({kind}): a session row must carry its kebab"
        )
        assert row.locator(".session-gear").count() == 0, (
            f"row {i} ({kind}): stray actions gear"
        )
        assert row.locator(".session-chevron").count() == 0, (
            f"row {i} ({kind}): stray chevron"
        )
        assert row.locator(".row-actions > [data-item=action-stop-close]").count() == 0, (
            f"row {i} ({kind}): stray Stop button loose on the row"
        )
        # Every row is a real button since #1025 — including a detached row
        # of an agent with no transcript reader, which used to render inert.
        expect(row.locator("button.session-open")).to_have_count(1)
        # #1238 J-10: the context line names the project folder, not the full
        # Windows path that wrapped to four lines on a phone; the full path is
        # the row's hover hint and the menu's Copy path.
        full = row.locator("button.session-open").get_attribute("title") or ""
        folder = re.split(r"[\\/]", full.rstrip("\\/"))[-1]
        # #1433: "folder · uptime", the folder first so it is the part a
        # narrow row keeps.
        meta = row.locator("button.session-open .srow-meta-text").inner_text()
        assert folder and meta.startswith(folder) and not re.search(r"[\\/]", meta), (
            f"row {i} ({kind}): context line {meta!r} should start with the "
            f"folder of {full!r} and carry no path"
        )
        # The normal case says nothing (#1433): no "full control" pill, and
        # only a detached row carries the neutral "detached" chip.
        expect(row).not_to_contain_text("full control")
        detached_chips = row.locator(".chip.session-detached").count()
        if kind == "remote":
            assert detached_chips == 1, f"row {i}: a detached row must say so"
        elif kind == "pty":
            assert detached_chips == 0, f"row {i}: a full-control row carries no kind chip"
            saw_pty = True
        else:
            pytest.fail(f"row {i}: unrecognised session kind {kind!r}")

    assert saw_pty, "launched PTY session did not surface as a full-control row"
    surface(authed_page, "code.tab")

    launched = authed_page.locator(
        f'#sessionsList li.session-item[data-session-id="{launched_pty_session}"]'
    )
    launched.locator(".session-kebab").click()
    expect(open_row_menu(authed_page).locator("[data-item=session-copy-path-btn]")).to_be_visible()
    authed_page.keyboard.press("Escape")


# A stuck job with five recent runs, served beside the parameterised one.
_STUCK_JOB = {
    "id": "demo",
    "name": "Demo",
    "target_kind": "py",
    "schedule_chip": "daily 06:00",
    "next_run": None,
    "running": False,
    "stuck": True,
    "last_run": {
        "run_id": "20260524T080000", "status": "running",
        "started_at": "2026-05-24T08:00:00", "duration_seconds": None,
    },
    "stats": {
        "p50": 4.2, "p95": 11.7,
        "success_rate_30d": 0.75, "completed_count": 6,
        "last7": [
            {"status": "success", "run_id": "a"},
            {"status": "failed",  "run_id": "b"},
            {"status": "success", "run_id": "c"},
            {"status": "success", "run_id": "d"},
            {"status": "running", "run_id": "e"},
        ],
    },
}


def _assert_jobs_row_renders_sparkline_and_stuck_chip(page: Page) -> None:
    """Formerly ``test_jobs_row_renders_sparkline_and_duration_chip`` (#66).

    The row (``li.job-row`` since #1438) carries the sparkline and a "stuck"
    exception chip on its meta line; the percentiles and the last-run
    sentence are in the job sheet opened by tapping the row. The jobs list is
    route-mocked, so the live polling renders it through the production code
    path."""
    row = page.locator("#jobsList li.job-row[data-id='demo']")
    expect(row).to_be_visible()
    # Sparkline: 5 dots, the failed one carries .down, running carries .live.
    spark = row.locator("[data-role='sparkline']")
    expect(spark).to_be_visible()
    dot_count = spark.locator(".job-spark-dot").count()
    assert dot_count == 5, f"expected 5 sparkline dots, got {dot_count}"
    assert spark.locator(".job-spark-dot.down").count() == 1
    assert spark.locator(".job-spark-dot.live").count() == 1
    expect(row.locator(".job-stuck-chip")).to_have_text("stuck")
    row.locator("button.action-row-main").click()
    details = page.locator("#jobSheetBody [data-role='job-details']")
    expect(details).to_be_visible()
    expect(details).to_contain_text("p50")
    expect(details).to_contain_text("p95")
    # Stuck marker shows up in the last-run line.
    expect(details).to_contain_text("stuck")


def test_parameterised_job_run_dialog_posts_values(
    authed_page: Page, base_url: str
) -> None:
    """Issue #67 — running a job with declared params must open the
    run-parameters dialog (not fire immediately); submitting it must POST
    ``{params: {...}}`` to ``/api/jobs/<id>/run``. A stuck job served beside
    it then pins the row's sparkline and the job sheet's detail block.

    Since #1438 the run is started from the row's kebab ``Run now`` item
    (``[data-item=job-run-item]``; there is no per-row ▶ button).

    Hermetic: we route-mock ``/api/jobs`` to return one synthetic
    parameterised job, intercept the run endpoint, and assert the
    captured request body.
    """
    fake_job = {
        "id": "scrape",
        "name": "Scrape",
        "target_kind": "py",
        "schedule_chip": "",
        "next_run": None,
        "running": False,
        "stuck": False,
        "args": "",
        "schedule": {"type": "none"},
        "params": [
            {"name": "since", "kind": "date", "flag": "--since",
             "required": True},
            {"name": "tier", "kind": "enum", "options": ["a", "b"],
             "default": "a", "required": False, "flag": "--tier"},
        ],
        "last_run": None,
        "stats": {
            "p50": None, "p95": None, "success_rate_30d": None,
            "completed_count": 0, "last7": [],
        },
    }
    authed_page.route(
        re.compile(r".*/api/jobs(\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"jobs": [fake_job, _STUCK_JOB]}),
        ),
    )

    captured: dict = {}

    def _capture_run(route):
        req = route.request
        captured["body"] = req.post_data or ""
        route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"run_id": "20260601T120000", "job_id": "scrape"}),
        )

    authed_page.route(
        re.compile(r".*/api/jobs/scrape/run$"),
        _capture_run,
    )

    _navigate_collecting_errors(authed_page, base_url)
    authed_page.locator("#tabJobs").click()

    row = authed_page.locator("#jobsList li.job-row[data-id='scrape']")
    expect(row).to_be_visible()
    # Run now from the kebab — for a parameterised job this must open the
    # run dialog, NOT fire the API directly.
    row.locator("button.job-menu-anchor").click()
    open_row_menu(authed_page).locator("[data-item=job-run-item]").click()

    dialog = authed_page.locator("#jobRunDialog")
    expect(dialog).to_be_visible()
    # One input per declared param. Date input → input[type=date]; enum →
    # select.
    since = dialog.locator("input[data-param-name='since']")
    expect(since).to_be_visible()
    since.fill("2026-06-01")
    tier = dialog.locator("select[data-param-name='tier']")
    expect(tier).to_be_visible()
    # Default 'a' should already be selected; flip to 'b' to prove the
    # value lands in the POST body.
    tier.select_option("b")

    dialog.locator("button[type='submit']").click()

    # The dialog closes and the POST fires. Give the route a moment to
    # capture the body.
    authed_page.wait_for_function(
        "() => !document.getElementById('jobRunDialog').open",
        timeout=3_000,
    )
    assert "body" in captured, "POST /api/jobs/scrape/run was never intercepted"
    payload = json.loads(captured["body"])
    assert payload == {"params": {"since": "2026-06-01", "tier": "b"}}, payload

    _assert_jobs_row_renders_sparkline_and_stuck_chip(authed_page)


def test_sign_in_sheet_opens_on_401(authed_page: Page, base_url: str) -> None:
    """A 401 opens the vendored sign-in sheet, with an editable password field.

    The bearer middleware bypasses loopback (server.py), so a bad token from
    127.0.0.1 never surfaces the sheet on its own; stubbing /api/config to 401
    is the same signal the app reacts to.
    """
    authed_page.route(
        "**/api/config",
        lambda route: route.fulfill(status=401, json={"detail": "auth required"}),
    )
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    sheet = authed_page.locator("dialog.sign-in")
    expect(sheet).to_be_visible()
    pw = sheet.locator("input[type=password]")
    expect(pw).to_be_editable()
    pw.fill("dummy")
    expect(pw).to_have_value("dummy")
    expect(sheet.locator("[data-track=sign-in-submit]")).to_be_enabled()
