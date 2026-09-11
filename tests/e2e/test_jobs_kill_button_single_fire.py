"""Regression pin for app-launcher#881 (Jobs tab: "Kill stuck run" fired twice).

``renderKillButton`` created the button once with an ``addEventListener``
click handler closing over the *first* run id, then "re-bound" it on every
later render by assigning ``onclick`` — which never removed the original
listener. After one re-render a single tap raised two ``confirm()`` prompts
and sent two ``POST .../kill`` calls, one of them against the stale run id
captured when the button was created.

Hermetic: route-mock ``/api/jobs`` (one stuck job), its run list (two live
runs) and each run's detail, plus the kill endpoint itself, which only
records what it was asked to kill. Expanding the job paints the newest run
(``run-b``) and creates the button; selecting the older run (``run-a``)
re-renders it. One tap must then send exactly one kill, for ``run-a``.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

_RUN_A = "20260910T120000"  # older, selected second
_RUN_B = "20260910T130000"  # newest, painted on expand


def _run_summary(run_id: str) -> dict:
    return {
        "run_id": run_id, "status": "running",
        "started_at": "2026-09-10T12:00:00", "trigger": "schedule",
        "exit_code": None, "dry_run": False, "params": {},
    }


def _wire_job_routes(page: Page, killed: list) -> None:
    fake_job = {
        "id": "demo",
        "name": "Demo",
        "target_kind": "py",
        "schedule_chip": "",
        "next_run": None,
        "running": True,
        "stuck": True,
        "args": "",
        "schedule": {"type": "none"},
        "params": [],
        "last_run": {
            "run_id": _RUN_B, "status": "running",
            "started_at": "2026-09-10T13:00:00", "duration_seconds": None,
        },
        "stats": {
            "p50": None, "p95": None, "success_rate_30d": None,
            "completed_count": 0, "last7": [],
        },
    }

    def _fulfill(route, body: dict) -> None:
        route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)
        )

    def _kill(route) -> None:
        killed.append(route.request.url.rsplit("/runs/", 1)[1].split("/", 1)[0])
        _fulfill(route, {"killed": True})

    def _detail(route) -> None:
        run_id = route.request.url.rsplit("/", 1)[1]
        _fulfill(route, {"run": {
            "run_id": run_id, "status": "running", "output_tail": "working…\n",
            "exit_code": None, "cpu_seconds": None, "peak_rss_bytes": None,
            "duration_seconds": None,
        }})

    # Most specific first; the four regexes are mutually exclusive anyway.
    page.route(re.compile(r".*/api/jobs/demo/runs/[^/]+/kill$"), _kill)
    page.route(re.compile(r".*/api/jobs/demo/runs/[^/?]+$"), _detail)
    page.route(
        re.compile(r".*/api/jobs/demo/runs$"),
        lambda route: _fulfill(
            route, {"runs": [_run_summary(_RUN_B), _run_summary(_RUN_A)]}
        ),
    )
    page.route(
        re.compile(r".*/api/jobs(\?.*)?$"),
        lambda route: _fulfill(route, {"jobs": [fake_job]}),
    )


def test_kill_button_fires_once_for_the_current_run(
    authed_page: Page, base_url: str
) -> None:
    killed: list = []
    dialogs: list = []
    _wire_job_routes(authed_page, killed)

    def _accept(dialog) -> None:
        dialogs.append(dialog.message)
        dialog.accept()

    authed_page.on("dialog", _accept)

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#sessionsList", state="attached", timeout=5_000)
    authed_page.locator("#tabJobs").click()

    row = authed_page.locator("#jobsList li.app-item[data-id='demo']")
    expect(row).to_be_visible()
    row.locator("button.session-open").click()

    # First render: the newest run is painted and the button is created.
    label = authed_page.locator("[data-role='output-label']")
    expect(label).to_contain_text(_RUN_B)
    kill_btn = authed_page.locator("[data-role='kill-btn']")
    expect(kill_btn).to_have_count(1)

    # Second render of the same button: select the older live run.
    authed_page.locator(".jobs-run-btn").nth(1).click()
    expect(label).to_contain_text(_RUN_A)
    expect(kill_btn).to_have_count(1)

    kill_btn.click()
    expect(authed_page.locator(".toast")).to_contain_text("Kill signal sent")

    assert len(dialogs) == 1, (
        f"one tap raised {len(dialogs)} confirm() prompts — a stale click "
        "listener survived the re-render"
    )
    assert killed == [_RUN_A], (
        f"one tap sent kills for {killed}, expected exactly [{_RUN_A!r}] "
        "(the currently displayed run)"
    )
