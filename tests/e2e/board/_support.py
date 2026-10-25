"""Route mocks and payload builders shared by the Board e2e modules."""

from __future__ import annotations

import copy
import json as _json
import re
from datetime import datetime, timedelta, timezone

from playwright.sync_api import Page, expect

from src.chief import plan as chief_plan


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )

_FAKE_BOARD = {
    "generated_at": "2026-07-02T12:00:00Z",
    "columns": {
        "backlog": [
            {"kind": "issue", "repo": "app-launcher", "number": 301,
             "title": "Board tab 2/3: drill-down + reply",
             "url": "https://github.com/ferraroroberto/app-launcher/issues/301",
             "updated_at": "2026-07-01T10:00:00Z", "labels": ["enhancement"]},
        ],
        "claude_turn": [
            {"session_id": "s-work", "kind": "pty", "agent": "claude",
             "project_dir": "E:/automation/life-os", "name": "life-os",
             "alive": True, "started_at": "2026-07-02T11:56:00Z",
             "live_title": "weekly recap", "prompt_title": "",
             "project": "life-os", "status": "working", "age_seconds": 240},
        ],
        "your_turn": [
            {"session_id": "s-wait", "kind": "pty", "agent": "claude",
             "project_dir": "E:/automation/photo-ocr", "name": "photo-ocr",
             "alive": True, "started_at": "2026-07-02T11:30:00Z",
             "live_title": "chunk merge fix", "prompt_title": "",
             "project": "photo-ocr", "status": "awaiting-input", "age_seconds": 720},
        ],
        "other": [
            {"kind": "pr", "repo": "app-launcher", "number": 158,
             "title": "keyboard-aware overlay",
             "url": "https://github.com/ferraroroberto/app-launcher/pull/158",
             "updated_at": "2026-07-02T09:00:00Z", "is_draft": False},
            {"kind": "job", "job_id": "reporting", "job_name": "reporting pipeline",
             "state": "failed", "run_id": "20260702T090200",
             "finished_at": "2026-07-02T09:02:00", "age_seconds": 10680},
        ],
        "done": [
            {"kind": "issue", "repo": "voice-transcriber", "number": 87,
             "title": "read-aloud segmentation",
             "url": "https://github.com/ferraroroberto/voice-transcriber/issues/87",
             "updated_at": "2026-07-02T08:00:00Z", "state": "closed", "labels": []},
        ],
    },
    "github": {"fetched_at": "2026-07-02T11:00:00Z", "error": None},
    "sessions_state": {"available": True, "stale": False,
                       "updated_at": "2026-07-02T11:58:00Z"},
}


def _board_payload(gh_age_seconds: int = 0) -> dict:
    """_FAKE_BOARD with ``fetched_at`` stamped relative to the real clock —
    fresh by default so opening the tab does not trigger the stale-cache
    auto-refresh; pass a large age to test that it does."""
    payload = copy.deepcopy(_FAKE_BOARD)
    payload["github"]["fetched_at"] = _iso_utc(
        datetime.now(timezone.utc) - timedelta(seconds=gh_age_seconds)
    )
    return payload


# The chief's plan file (#1279, format v1) as the chief's helper writes it.
# Tests serve what the real reader (src/chief/plan.py) makes of it, so the
# card is proven against the server's own output, not a hand-written copy.
_PLAN_FILE_V1 = {
    "version": 1,
    "lanes": [{"repo": "app-launcher", "session": "s-work", "item": "#1273", "status": "gate"}],
    "queue": [
        {"repo": "app-launcher", "ref": "#1273", "title": "Chat by default on desktop",
         "status": "gate", "note": ""},
        {"repo": "automation", "ref": "#135",
         "title": "parking burst trial across every garage in town, with the long tail of the "
                  "October permits and the fallback plan if the city portal is down again",
         "status": "someday", "note": "before Thu 1 Oct 16:00"},
    ],
    "waiting_on_roberto": [{"text": "Remember the last tab?", "ref": "app-launcher#1131"}],
}


def _plan(tmp_path, minutes_ago: int = 12) -> dict:
    """The /api/board/chief-plan body for _PLAN_FILE_V1, stamped N min ago."""
    f = tmp_path / "chief-plan.json"
    f.write_text(_json.dumps({**_PLAN_FILE_V1, "updated_at": _iso_utc(
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))}), encoding="utf-8")
    return chief_plan.read_chief_plan(f, "octo")


def _route_plan_from(page: Page, current: dict) -> None:
    """Serve ``current["plan"]`` from the chief-plan endpoint at request time."""
    page.route(
        re.compile(r".*/api/board/chief-plan$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(current["plan"]),
        ),
    )


def _mock_board(page: Page, payload: dict | None = None, plan: dict | None = None) -> None:
    body = _json.dumps(payload or _board_payload())
    page.route(
        re.compile(r".*/api/board(?:\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=body,
        ),
    )
    # The chief's plan rides the Board poll (#1279); no plan unless a test
    # serves one, so the real server's file never reaches a test.
    _route_plan_from(page, {"plan": plan or {"state": "empty"}})
    # Default stub for the gh-refresh POST so an auto-refresh can never
    # escape to the real server (and its real gh subprocess). Tests that
    # care about the POST register their own capturing route *after* this
    # one — Playwright matches the most recently added route first.
    page.route(
        re.compile(r".*/api/board/github/refresh$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(
                {"fetched_at": _iso_utc(datetime.now(timezone.utc)), "error": None}
            ),
        ),
    )
    # Same reasoning for the boot-time git-status fetch, which is backed by a
    # real `git` subprocess per project and so lands whenever it likes; its
    # completion calls renderBoard() whenever the Board tab is up
    # (apps-coding.js::refreshGitStatus), rebuilding the card DOM
    # mid-test (#510/#680). Clean payload so it can't perturb the rendered
    # annotations other tests read.
    # _chief_support.py's own _mock_board already does this; the two tests
    # below that care about the response register their route *after* this.
    page.route(
        re.compile(r".*/api/claude-code/git-status$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"projects": []}),
        ),
    )


def _open_board(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#tabBoard", state="attached", timeout=5_000)
    page.locator("#tabBoard").click()
    expect(page.locator("#paneBoard")).to_be_visible()


def _unfold(page: Page, *section_ids: str) -> None:
    """Open Board column sections (#1198). The GitHub-fed ones start folded on
    the phone and open on the desktop grid, so tap a summary only when its
    section is still closed — a tap on an open one would fold it."""
    for section_id in section_ids:
        section = page.locator(f"#{section_id}")
        if section.get_attribute("open") is None:
            page.locator(f"#{section_id} > summary").click()
        expect(section).to_have_attribute("open", "")


def _route_board_from(page: Page, current: dict) -> None:
    """Serve whatever ``current["body"]`` holds at request time, so a test
    can change the server's answer mid-flight. Registered after _mock_board,
    so it wins."""
    page.route(
        re.compile(r".*/api/board(?:\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(current["body"]),
        ),
    )


_FAKE_EXCHANGE = {
    "available": True,
    "source": "native",
    "reason": None,
    "user": {"text": "please fix the merge", "timestamp": "2026-07-02T11:50:00Z"},
    "assistant": {
        "text": "Merge fixed — tests green. Ship it?",
        "timestamp": "2026-07-02T11:55:00Z",
    },
}


def _mock_exchange(
    page: Page, sid: str = "s-wait", payload: dict | None = None
) -> None:
    page.route(
        re.compile(r".*/api/board/sessions/" + sid + r"/exchange.*"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(payload or _FAKE_EXCHANGE),
        ),
    )
