"""Smoke tests for the launcher webapp (issue #22).

Tight by design: ~6 checks that catch the bugs we actually hit (JS
exceptions on boot, empty config form, broken tab switch, wrong stop
buttons per session kind, missing login overlay markup). Expand
iteratively in follow-up issues; do NOT turn this file into a regression
net for every feature.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke


def _navigate_collecting_errors(page: Page, base_url: str) -> list[str]:
    """Open the SPA and capture any uncaught JS errors during boot."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    # #sessionsList is rendered server-side in index.html; waiting for it
    # confirms the static document parsed without an early script crash.
    page.wait_for_selector("#sessionsList", state="attached", timeout=5_000)
    return errors


def test_page_loads_without_console_errors(authed_page: Page, base_url: str) -> None:
    errors = _navigate_collecting_errors(authed_page, base_url)
    # Give the boot script a beat to settle: fetchConfig, sessions poll,
    # listeners poll. Anything thrown during that fans out as pageerror.
    authed_page.wait_for_timeout(500)
    assert errors == [], f"JS errors during boot:\n  - " + "\n  - ".join(errors)


def test_coding_options_populated(authed_page: Page, base_url: str) -> None:
    _navigate_collecting_errors(authed_page, base_url)
    # The Coding options card is a <details> collapsed by default — expand
    # it so the segmented controls become visible. renderClaudeOptions()
    # runs after /api/config resolves regardless, but the buttons are only
    # *visible* once the panel is open. Click the title (not the summary's
    # geometric centre, which can land on a stopPropagation toggle now that
    # the row carries both ☁️ Detached and ↺ Resume — issue #151).
    # Scope to the Coding options card: the Running-sessions + Projects
    # panels now share the .coding-summary-title class (issue #212), so a
    # bare class selector matches three titles.
    authed_page.locator("#codingOptions .coding-summary-title").click()
    authed_page.wait_for_selector("#claudeModel > button", timeout=5_000)
    authed_page.wait_for_selector("#claudeEffort > button", timeout=5_000)
    authed_page.wait_for_selector("#claudePermission > button", timeout=5_000)
    model_count = authed_page.locator("#claudeModel > button").count()
    effort_count = authed_page.locator("#claudeEffort > button").count()
    perm_count = authed_page.locator("#claudePermission > button").count()
    assert model_count >= 1, f"#claudeModel rendered no buttons (got {model_count})"
    assert effort_count >= 1, f"#claudeEffort rendered no buttons (got {effort_count})"
    assert perm_count == 2, f"#claudePermission expected 2 buttons (got {perm_count})"


def test_sessions_panel_renders(authed_page: Page, base_url: str) -> None:
    _navigate_collecting_errors(authed_page, base_url)
    expect(authed_page.locator("#sessionsList")).to_be_attached()
    # The panel is valid when it has rows OR the empty-state paragraph
    # (with its copy) is showing. The old test snapshotted the row count
    # and *then* asserted on #sessionsEmpty — between the two, the 5 s
    # fetchSessions poll could re-render, flip #sessionsEmpty to hidden,
    # and add a row, so the assertion saw the now-hidden paragraph (#39).
    # Evaluate both halves atomically in one retried wait_for_function so
    # a mid-test re-render can never split the check.
    authed_page.wait_for_function(
        """() => {
          const list = document.getElementById('sessionsList');
          const empty = document.getElementById('sessionsEmpty');
          if (!list || !empty) return false;
          if (list.querySelectorAll('li.session-item').length > 0) return true;
          return !empty.hidden && empty.textContent.trim() ===
            'No sessions launched from here yet — tap a project below to start one.';
        }""",
        timeout=10_000,
    )


def test_tabs_switch(authed_page: Page, base_url: str) -> None:
    _navigate_collecting_errors(authed_page, base_url)
    pane_claude = authed_page.locator("#paneClaude")
    pane_apps = authed_page.locator("#paneApps")
    pane_jobs = authed_page.locator("#paneJobs")

    expect(pane_claude).to_be_visible()
    expect(pane_apps).to_be_hidden()
    expect(pane_jobs).to_be_hidden()

    authed_page.locator("#tabApps").click()
    expect(pane_apps).to_be_visible()
    expect(pane_claude).to_be_hidden()
    expect(pane_jobs).to_be_hidden()
    # Substring match on className so future class reorders don't false-fail.
    expect(authed_page.locator("#tabApps")).to_have_class(re.compile(r"\bactive\b"))

    # Issue #47 — third tab. Switching to Jobs must hide both other panes
    # and surface the Jobs pane; the empty-state message renders when no
    # jobs are registered (which is the case in CI / a fresh install).
    authed_page.locator("#tabJobs").click()
    expect(pane_jobs).to_be_visible()
    expect(pane_claude).to_be_hidden()
    expect(pane_apps).to_be_hidden()
    expect(authed_page.locator("#tabJobs")).to_have_class(re.compile(r"\bactive\b"))

    authed_page.locator("#tabClaude").click()
    expect(pane_claude).to_be_visible()
    expect(pane_apps).to_be_hidden()
    expect(pane_jobs).to_be_hidden()


def test_pty_session_renders_with_both_stop_buttons(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """End-to-end: the launched PTY session shows up with both stop buttons.

    The `launched_pty_session` fixture POSTs /api/apps/.../launch (mode=pty)
    before the test and force-kills the session in teardown — so this test
    no longer depends on the user having something running.

    Other session rows (if any) are also checked: detached → only ⏏️,
    full-control → both ⏹ and ⏏️. The launched session must be among
    the full-control rows.
    """
    _navigate_collecting_errors(authed_page, base_url)
    # The session was launched before navigation, so boot()'s initial
    # fetchSessions should already include it; the SPA also re-polls on a
    # 5 s timer. Wait out one poll cycle as a fallback (no manual refresh
    # button to force an immediate fetch any more).
    pty_rows = authed_page.locator("#sessionsList li.session-item:has(.session-kind.pty)")
    expect(pty_rows.first).to_be_visible(timeout=8_000)

    rows = authed_page.locator("#sessionsList li.session-item")
    count = rows.count()
    assert count >= 1, "fixture launched a session but #sessionsList is empty"

    saw_pty = False
    for i in range(count):
        row = rows.nth(i)
        kind = row.locator(".session-kind").inner_text().strip().lower()
        # Issue #253: exactly one 🛑 Stop-and-kill button per row, both
        # kinds — the old ⏹ "leave window open" button is gone.
        stop = row.locator(".action-stop:not(.action-stop-close)")
        stop_kill = row.locator(".action-stop-close")
        assert stop.count() == 0, f"row {i} ({kind}): stray legacy Stop button"
        assert stop_kill.count() == 1, f"row {i} ({kind}): expected one 🛑 button"
        expect(stop_kill.first).to_be_visible()
        if "detached" in kind:
            pass
        elif "full control" in kind:
            saw_pty = True
        else:
            pytest.fail(f"row {i}: unrecognised session kind text {kind!r}")

    assert saw_pty, "launched PTY session did not surface as a 'full control' row"


def test_jobs_row_renders_sparkline_and_duration_chip(
    authed_page: Page, base_url: str
) -> None:
    """Issue #66 — the Jobs row must render the sparkline + duration chip
    when stats are present, and the ⚠️ stuck marker when ``job.stuck``.

    We route-mock ``/api/jobs`` to return one synthetic stuck job so the
    live polling renders it through the production code path — the
    dynamic-import alternative would land on a different ES-module URL
    than the rewriter-hashed one main.js loaded, so its render would
    fight the OG polling and lose.
    """
    fake_job = {
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
    import json as _json
    body = _json.dumps({"jobs": [fake_job]})
    authed_page.route(
        re.compile(r".*/api/jobs(\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=body,
        ),
    )

    _navigate_collecting_errors(authed_page, base_url)
    authed_page.locator("#tabJobs").click()

    row = authed_page.locator("#jobsList li.app-item[data-id='demo']")
    expect(row).to_be_visible()
    # Sparkline: 5 dots, the failed one carries .down, running carries .live.
    spark = row.locator("[data-role='sparkline']")
    expect(spark).to_be_visible()
    dot_count = spark.locator(".job-spark-dot").count()
    assert dot_count == 5, f"expected 5 sparkline dots, got {dot_count}"
    assert spark.locator(".job-spark-dot.down").count() == 1
    assert spark.locator(".job-spark-dot.live").count() == 1
    # Duration chip text contains both percentiles.
    chip = row.locator("[data-role='duration-chip']")
    expect(chip).to_contain_text("p50")
    expect(chip).to_contain_text("p95")
    # Stuck marker shows up in the meta text.
    expect(row.locator(".meta")).to_contain_text("stuck")
    # Health dot inherits the stuck class.
    expect(row.locator("[data-role='status-dot']")).to_have_class(
        re.compile(r"\bstuck\b")
    )


def test_parameterised_job_run_dialog_posts_values(
    authed_page: Page, base_url: str
) -> None:
    """Issue #67 — tapping ▶ on a job with declared params must open the
    run-parameters dialog (not fire immediately); submitting it must POST
    ``{params: {...}}`` to ``/api/jobs/<id>/run``.

    Hermetic: we route-mock ``/api/jobs`` to return one synthetic
    parameterised job, intercept the run endpoint, and assert the
    captured request body.
    """
    import json as _json

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
            body=_json.dumps({"jobs": [fake_job]}),
        ),
    )

    captured: dict = {}

    def _capture_run(route):
        req = route.request
        captured["body"] = req.post_data or ""
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"run_id": "20260601T120000", "job_id": "scrape"}),
        )

    authed_page.route(
        re.compile(r".*/api/jobs/scrape/run$"),
        _capture_run,
    )

    _navigate_collecting_errors(authed_page, base_url)
    authed_page.locator("#tabJobs").click()

    row = authed_page.locator("#jobsList li.app-item[data-id='scrape']")
    expect(row).to_be_visible()
    # Tap ▶ — for a parameterised job this must open the run dialog,
    # NOT fire the API directly.
    row.locator("[data-role='run-btn']").click()

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
    payload = _json.loads(captured["body"])
    assert payload == {"params": {"since": "2026-06-01", "tier": "b"}}, payload


def test_login_overlay_dom_present(authed_page: Page, base_url: str) -> None:
    """The login overlay markup is wired so showLogin() can reveal it.

    We exercise the DOM directly rather than triggering a real 401: the
    bearer middleware bypasses loopback (server.py:267), so a bad token
    from 127.0.0.1 won't surface the overlay. This still catches the
    regression we care about — overlay element + password input missing
    or renamed.
    """
    _navigate_collecting_errors(authed_page, base_url)
    overlay = authed_page.locator("#loginOverlay")
    expect(overlay).to_be_hidden()
    # Flip the hidden attr the same way showLogin() does.
    authed_page.evaluate(
        "document.getElementById('loginOverlay').hidden = false"
    )
    expect(overlay).to_be_visible()
    pw = authed_page.locator("#loginPassword")
    expect(pw).to_be_editable()
    pw.fill("dummy")
    expect(pw).to_have_value("dummy")
