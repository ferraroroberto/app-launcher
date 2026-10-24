"""The screenshot baseline's capture: the five tabs, plus a sent HTML document's card and
viewer, at phone and desktop, light and dark.

`scripts/shot_baseline.py` compares what this writes under `tests/e2e/shots`
with the committed PNGs, so a move-only refactor proves it changed no pixel.
Every fetch that carries data, a clock or a build identity is route-mocked
with fictional values, so a shot never shows this machine's apps, jobs,
sessions or folders, and two runs of one commit write the same pixels.
Chromium only: unmarked tests are not run on WebKit/iPhone.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e._shot import PINNED_NOW, shot, variant_page, variants
from tests.e2e.conftest import open_session_row, stub_session_mirror

pytestmark = pytest.mark.smoke

SHOTS_DIR = Path(__file__).resolve().parent.parent / "shots"
NOW = int(PINNED_NOW.timestamp())
STAMP = "2026-01-15T08:50:00Z"


def _job(job_id: str, name: str, **extra) -> dict:
    job = {
        "id": job_id, "name": name, "target_kind": "python", "schedule_chip": "daily 03:00",
        "next_run": "tomorrow 03:00", "next_run_epoch": NOW + 6 * 3600, "running": False,
        "stuck": False, "paused": False, "confirm": False, "params": [],
        "schedule": {"type": "daily", "at": "03:00"},
        "stats": {"last7": [{"run_id": f"h{i}", "status": "success", "outcome": "success"} for i in range(7)]},
    }
    return {**job, **extra}


def _app(app_id: str, name: str, kind: str, **extra) -> dict:
    return {"id": app_id, "name": name, "kind": kind, "bat_path": f"C:\\stub\\{app_id}\\run.bat",
            "added_at": "2026-01-01T00:00:00", "autostart": False, **extra}


def _skill(name: str) -> dict:
    return {"id": name, "name": name, "command": name, "description": "A fictional skill.", "skill_md": "x"}


_SESSIONS = [
    {"session_id": f"s-{name}", "kind": "pty", "agent": "claude", "label": "", "project_dir": f"C:/stub/{name}",
     "name": name, "flags": "", "started_at": NOW - started, "alive": True, "rows": 40, "cols": 120,
     "live_title": "", "prompt_title": f"{name} title", "manual_title": "", "output_chars": 10,
     "board_column": column, "board_status": status}
    for name, started, column, status in (
        ("alpha", 600, "claude_turn", "working"), ("beta", 3600, "your_turn", "awaiting-input"))
]

_BOARD = {
    "generated_at": STAMP,
    "columns": {
        "backlog": [{"kind": "issue", "repo": "demo-project", "number": 12, "title": "A synthetic queued item",
                     "url": "https://example.invalid/12", "updated_at": STAMP, "labels": ["enhancement"]}],
        "claude_turn": [{"session_id": "s-alpha", "kind": "pty", "agent": "claude", "project_dir": "C:/stub/alpha",
                         "name": "alpha", "alive": True, "started_at": STAMP, "live_title": "a fictional task",
                         "prompt_title": "", "project": "alpha", "status": "working", "age_seconds": 240}],
        "your_turn": [{"session_id": "s-beta", "kind": "pty", "agent": "claude", "project_dir": "C:/stub/beta",
                       "name": "beta", "alive": True, "started_at": STAMP, "live_title": "a fictional question",
                       "prompt_title": "", "project": "beta", "status": "awaiting-input", "age_seconds": 720}],
        "other": [{"kind": "pr", "repo": "demo-project", "number": 15, "title": "A synthetic pull request",
                   "url": "https://example.invalid/15", "updated_at": STAMP, "is_draft": False}],
        "done": [{"kind": "issue", "repo": "demo-project", "number": 11, "title": "A synthetic finished item",
                  "url": "https://example.invalid/11", "updated_at": STAMP, "state": "closed", "labels": []}],
    },
    "github": {"fetched_at": STAMP, "error": None},
    "sessions_state": {"available": True, "stale": False, "updated_at": STAMP},
}

_SKILLS = {"available": True, "life_os_dir": "C:/stub/life", "skills": [_skill("garden-notes"), _skill("reading-list")]}
_APPS = {"scan_root": "C:\\stub", "apps": [
    _app("alpha-dash", "Alpha Dashboard", "streamlit"), _app("beta-web", "Beta Web", "webapp"),
    _app("gamma-tunnel", "Gamma Tunnel", "tunnel", tunnel_url="https://gamma.example.invalid/", health="up"),
    _app("omega-tray", "Omega Tray", "tray", autostart=True)]}
_JOBS = {"jobs": [
    _job("ledger", "Ledger reconciliation", alert_on_failure=True),
    _job("mailbox", "Mailbox sweep", schedule_chip="every 15 min", next_run_epoch=NOW + 300),
    _job("planner", "Planner sync", paused=True)]}

#: endpoint pattern -> body; every fetch the five tabs make that is not static.
_MOCKS = {
    r"/api/version": {"git_sha": "0000000", "built_at": "2026-01-15 08:00"},
    r"/api/system/load": {"level": "normal", "reason": None, "cpu_percent": 12.0, "cores": 8, "top": [],
                          "memory": {"used_bytes": 8 * 1024**3, "total_bytes": 32 * 1024**3, "percent": 25.0},
                          "age_seconds": 1.0},
    r"/api/rate-limits": {"quota_lines": []},
    r"/api/context-filter": {},
    r"/api/claude-code/sessions": {"sessions": _SESSIONS},
    r"/api/claude-code/git-status": {"projects": []},
    r"/api/apps": _APPS,
    r"/api/apps/running": {"running": []},
    r"/api/ports/probe": {"listeners": []},
    r"/api/jobs": _JOBS,
    r"/api/jobs/agenda": {"days": 7, "generated_epoch": NOW, "occurrences": [
        {"job_id": "ledger", "name": "Ledger reconciliation", "fire_epoch": NOW + 6 * 3600,
         "cadence": "daily 03:00"}], "frequent": []},
    r"/api/life-os/skills": _SKILLS,
    r"/api/life-os/channels": {"profiles": [], "problems": [], "setup": {
        "file_present": True, "life_os_found": True, "bun": True, "plugin": True, "skills": []}},
    r"/api/life-os/recap-status": {"available": True, "ledger_exists": True, "age_days": 3.0,
                                   "staleness": "fresh", "proposal_pending": False, "proposal_name": None},
    r"/api/board": _BOARD,
    r"/api/board/chief-plan": {"state": "empty"},
    r"/api/board/github/refresh": {"fetched_at": STAMP, "error": None},
    r"/api/usage/events": {},
}

#: tab button -> the first row that proves the tab rendered its fixture.
_TABS = {
    "code": ("#tabClaude", "#sessionsList li"),
    "apps": ("#tabApps", "#appsList li.action-row"),
    "jobs": ("#tabJobs", "#jobsList li.job-row"),
    "life": ("#tabLifeOS", "#lifeOsList li.action-row"),
    "board": ("#tabBoard", "#boardColumns .board-card"),
}


def _json_route(body: dict):
    return lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def _mock_data(page: Page) -> None:
    for pattern, body in _MOCKS.items():
        page.route(re.compile(rf".*{pattern}(\?.*)?$"), _json_route(body))


@pytest.mark.parametrize("name", _TABS)
def test_tab_shots(browser: Browser, base_url: str, name: str) -> None:
    tab, ready = _TABS[name]
    for variant in variants():
        with variant_page(browser, variant) as page:
            page.add_init_script(f"localStorage.setItem('app-launcher.theme', '{variant.theme}');")
            _mock_data(page)
            page.goto(f"{base_url}/", wait_until="domcontentloaded")
            page.locator(tab).click()
            expect(page.locator(ready).first).to_be_visible()
            shot(page, SHOTS_DIR / f"{name}-{variant.slug}.png")


_REPORT_HTML = (
    "<!doctype html><html><head><style>"
    "body{font:15px/1.5 system-ui,sans-serif;margin:24px;color:#1f2328}"
    "h1{font-size:22px;margin:0 0 8px}table{border-collapse:collapse;margin-top:12px}"
    "td,th{border:1px solid #d1d9e0;padding:4px 10px;text-align:left}"
    "</style></head><body><h1>Garden plan</h1>"
    "<p>A fictional report: what grows where this season.</p>"
    "<table><tr><th>Bed</th><th>Crop</th></tr><tr><td>North</td><td>Beans</td></tr>"
    "<tr><td>South</td><td>Tomatoes</td></tr></table></body></html>"
)
_REPORT_ID = "0" * 31 + "1"
_CHAT = {
    "available": True, "source": "claude", "reason": None, "session_id": "s-alpha",
    "next_cursor": None, "tail": None, "size": 10,
    "entries": [
        {"kind": "user", "timestamp": STAMP, "offset": 0, "text": "Send me the garden plan.",
         "truncated": False, "sidechain": False},
        {"kind": "assistant", "timestamp": "2026-01-15T08:50:20Z", "offset": 5, "text": "Here it is.",
         "truncated": False, "sidechain": False},
    ],
    "outbox_rev": 1,
    "outbox": [{"id": _REPORT_ID, "name": "garden-plan.html", "caption": "", "kind": "html",
                "media": "text/html; charset=utf-8", "size": len(_REPORT_HTML),
                "timestamp": "2026-01-15T08:50:10Z"}],
}


def test_html_viewer_shots(browser: Browser, base_url: str) -> None:
    """A sent HTML document's card in the Chat pane, then its viewer open."""
    for variant in variants():
        with variant_page(browser, variant) as page:
            page.add_init_script(f"localStorage.setItem('app-launcher.theme', '{variant.theme}');")
            _mock_data(page)
            base = r".*/api/claude-code/sessions/s-alpha"
            page.route(re.compile(base + r"/transcript(\?.*)?$"), _json_route(_CHAT))
            page.route(re.compile(base + r"/outbox/[0-9a-f]+$"), lambda route: route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=_REPORT_HTML))
            stub_session_mirror(page)
            page.goto(f"{base_url}/", wait_until="domcontentloaded")
            open_session_row(page, page.locator('#sessionsList li[data-session-id="s-alpha"]'), mode="chat")
            card = page.locator("#transcriptList .tr-file-card[data-kind='html']")
            expect(card.locator("iframe").content_frame.locator("h1")).to_have_text("Garden plan")
            shot(page, SHOTS_DIR / f"chat-html-{variant.slug}.png")
            card.locator(".tr-file-html-open").click()
            expect(page.locator("#htmlViewer iframe").content_frame.locator("h1")).to_have_text("Garden plan")
            shot(page, SHOTS_DIR / f"html-viewer-{variant.slug}.png")
