"""Shared mocks for the Board chief tests: the chief and worker cards, the plan and its questions, uploads and voice."""

from __future__ import annotations

import base64
import copy
import json as _json
import re
from datetime import datetime, timezone

from playwright.sync_api import Page, expect

from src import chief_answers, chief_plan
from tests.e2e._audio_mock import RECORDER_BASE_JS


_CHIEF_CARD = {
    "session_id": "s-chief", "kind": "pty", "agent": "claude",
    "label": "chief", "project_dir": "E:/automation/fleet-config",
    "name": "chief", "alive": True, "started_at": "2026-07-18T06:00:00Z",
    "live_title": "", "prompt_title": "", "manual_title": "chief",
    "project": "fleet-config", "status": "idle", "age_seconds": 60,
}


_WORKER_CARD = {
    "session_id": "s-work", "kind": "pty", "agent": "claude",
    "project_dir": "E:/automation/life-os", "name": "life-os",
    "alive": True, "started_at": "2026-07-18T06:30:00Z",
    "live_title": "weekly recap", "prompt_title": "",
    "project": "life-os", "status": "working", "age_seconds": 240,
}


_BOARD_BASE = {
    "generated_at": "2026-07-18T07:00:00Z",
    "columns": {
        "backlog": [], "claude_turn": [], "your_turn": [], "other": [],
        "done": [],
    },
    "github": {"fetched_at": "2026-07-18T06:59:00Z", "error": None},
    "sessions_state": {"available": True, "stale": False,
                       "updated_at": "2026-07-18T06:59:30Z"},
}


_CHIEF_EXCHANGE = {
    "available": True,
    "source": "native",
    "reason": None,
    "user": {"text": "what's open in app-launcher?",
             "timestamp": "2026-07-18T07:01:00Z"},
    "assistant": {"text": "6 open issues. Smallest is #229 — start it?",
                  "timestamp": "2026-07-18T07:01:30Z"},
}


def _board_payload(*, with_chief: bool) -> dict:
    payload = copy.deepcopy(_BOARD_BASE)
    if with_chief:
        payload["columns"]["claude_turn"] = [copy.deepcopy(_CHIEF_CARD)]
    payload["columns"]["claude_turn"].append(copy.deepcopy(_WORKER_CARD))
    # Stamp gh fresh at real-clock time so tab open never auto-refreshes.
    payload["github"]["fetched_at"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    return payload


def _mock_board(page: Page, payload: dict) -> None:
    body = _json.dumps(payload)
    page.route(
        re.compile(r".*/api/board(?:\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=body,
        ),
    )
    _mock_board_side_routes(page)


_PLAN = {
    "state": "ok", "updated_at": "", "lanes": [], "waiting_on_roberto": [],
    "queue": [{"repo": "app-launcher", "ref": "#1279", "title": "chief's plan card",
               "status": "building", "note": ""}],
}


def _mock_board_side_routes(page: Page) -> None:
    """Everything _mock_board stubs besides /api/board itself, for a test
    that serves a payload it changes mid-test."""
    # The chief's plan (#1279), which rides the Board poll.
    page.route(
        re.compile(r".*/api/board/chief-plan$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(_PLAN),
        ),
    )
    # The answer sheet's answered marks (#1487): recorded as sent, never
    # reaching the webapp's own store.
    page.route(
        re.compile(r".*/api/board/chief/answered$"),
        lambda route: route.fulfill(json={
            "recorded": True,
            "updated_at": route.request.post_data_json["updated_at"],
            "answered": route.request.post_data_json["ids"],
        }),
    )
    page.route(
        re.compile(r".*/api/board/github/refresh$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds").replace("+00:00", "Z"), "error": None}),
        ),
    )
    # Boot-time git-status is git-subprocess-backed and lands whenever it
    # likes; a real response mid-interaction rebuilds the drawer DOM out
    # from under a click (#510/#512) — mock it deterministic.
    page.route(
        re.compile(r".*/api/claude-code/git-status$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"projects": []}),
        ),
    )


def _mock_exchange(page: Page, sid: str = "s-chief") -> None:
    page.route(
        re.compile(r".*/api/board/sessions/" + sid + r"/exchange.*"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(_CHIEF_EXCHANGE),
        ),
    )


def _mock_ensure(
    page: Page, captured: dict, *, spawned: bool = False,
    resumed: bool = False, resume_fallback_reason: str = "",
) -> None:
    def _capture(route):
        captured["method"] = route.request.method
        captured["body"] = route.request.post_data_json
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "session_id": "s-chief", "spawned": spawned,
                "resumed": resumed,
                "resume_fallback_reason": resume_fallback_reason,
            }),
        )
    page.route(re.compile(r".*/api/board/chief/ensure$"), _capture)


def _open_board(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#tabBoard", state="attached", timeout=5_000)
    page.locator("#tabBoard").click()
    expect(page.locator("#paneBoard")).to_be_visible()


# ------------------------------------------- chief's answer sheet (#1295)

# The chief's questions as its plan writer records them (additive plan v1),
# served as the real reader (src/chief_plan.py) makes of them: one full
# single-select, one multi-select with no ref whose recommendation marks no
# option, one old-style text-only item.
_QUESTIONS = [
    {"id": "q-plans", "text": "Approve the plans", "ref": "fleet-config#959", "repo": "fleet-config",
     "question": "Approve the four plans?", "detail": "Each plan ships as its own PR.",
     "recommendation": "Yes: all four are small and independent.",
     "options": [
         {"label": "Yes, all four", "description": "Ship them in order", "recommended": True},
         {"label": "Only the first"},
         {"label": "Hold them"},
     ]},
    {"id": "q-days", "text": "Which days?", "repo": "life-os", "question": "Which days work?",
     "multi": True, "recommendation": "Mon and Wed suit the gym.",
     "options": [{"label": "Mon"}, {"label": "Wed"}, {"label": "Fri"}]},
    {"text": "Remember the last tab?", "ref": "app-launcher#1131"},
]


def _plan_with(tmp_path, waiting: list, updated_at: str = "2026-09-27T09:00:00Z") -> dict:
    f = tmp_path / "chief-plan.json"
    f.write_text(_json.dumps({
        "version": 1, "updated_at": updated_at, "lanes": [],
        "queue": [{"repo": "app-launcher", "ref": "#1295", "title": "answer sheet", "status": "building"}],
        "waiting_on_roberto": waiting,
    }), encoding="utf-8")
    return chief_plan.read_chief_plan(f, "octo")


def _route_plan_from(page: Page, current: dict) -> None:
    """Serve ``current["plan"]`` from the chief-plan endpoint at request time
    (registered after _mock_board, so it wins over that helper's fixed plan)."""
    page.route(
        re.compile(r".*/api/board/chief-plan$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(current["plan"]),
        ),
    )


# A 1x1 PNG, named `e2e-stub-...` for the conftest upload-leak check (#922).
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


_CHIEF_UPLOAD = "E:/automation/fleet-config/.launcher-tmp/e2e-stub-paste.png"


def _mock_chief_upload(page: Page) -> list:
    """Stub the chief session's attachment route, as Chat's composer uploads
    to its session's; return the requested URLs."""
    uploads: list = []

    def _capture(route):
        uploads.append(route.request.url)
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"path": _CHIEF_UPLOAD}),
        )

    page.route(re.compile(r".*/api/claude-code/sessions/s-chief/image(?:\?.*)?$"), _capture)
    return uploads


def _paste_image(page: Page, target: str) -> bool:
    """Fire a synthetic image paste on ``target``, as the Chat composer's
    #1206 test does; return whether the page took it (defaultPrevented)."""
    return page.evaluate(
        """([b64, target]) => {
          const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
          const dt = new DataTransfer();
          dt.items.add(new File([bytes], 'e2e-stub-paste.png', {type: 'image/png'}));
          const ev = new Event('paste', {bubbles: true, cancelable: true});
          Object.defineProperty(ev, 'clipboardData', {value: dt});
          document.querySelector(target).dispatchEvent(ev);
          return ev.defaultPrevented;
        }""",
        [base64.b64encode(_PNG_1x1).decode(), target],
    )


def _capture_chief_input(page: Page) -> list:
    posts: list = []

    def _capture(route):
        posts.append(route.request.post_data_json)
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"ok": True, "bytes": 8, "submit": True}),
        )

    page.route(re.compile(r".*/api/claude-code/sessions/s-chief/input$"), _capture)
    return posts


def _route_voice(page: Page, on: bool) -> None:
    """Serve /api/status with dictation switched ``on`` or off, whatever this
    box's voice-transcriber config says."""
    def _status(route):
        resp = route.fetch()
        body = resp.json()
        body["voice_dictation"] = on
        route.fulfill(response=resp, json=body)

    page.route(re.compile(r".*/api/status$"), _status)


class _SharedPlanServer:
    """The webapp's two plan routes for two pages at once (#1487): the plan
    as the real reader makes of a file, the answered marks in the real
    ``src/chief_answers`` store under ``tmp_path``. Both pages read and write
    this one state, as two devices on one launcher do; recording follows the
    route's rule (only while that is still the plan), which
    tests/test_chief_answers.py pins on the route itself."""

    def __init__(self, tmp_path, waiting: list) -> None:
        self.tmp_path = tmp_path
        self.waiting = waiting
        self.plan = _plan_with(tmp_path, waiting)
        self.store = tmp_path / "chief-answered.json"
        self.records: list = []

    def rewrite(self, updated_at: str) -> None:
        """The chief rewrites its plan: same questions, a new version."""
        self.plan = _plan_with(self.tmp_path, self.waiting, updated_at)

    def _answered(self) -> list:
        return chief_answers.read_answered(self.plan["updated_at"], self.store)

    def _serve_plan(self, route) -> None:
        route.fulfill(json={**self.plan, "answered": self._answered()})

    def _record(self, route) -> None:
        body = route.request.post_data_json
        self.records.append(body)
        recorded = body["updated_at"] == self.plan["updated_at"]
        if recorded:
            chief_answers.record_answered(body["updated_at"], body["ids"], self.store)
        route.fulfill(json={
            "recorded": recorded, "updated_at": self.plan["updated_at"],
            "answered": self._answered(),
        })

    def route(self, page: Page) -> None:
        page.route(re.compile(r".*/api/board/chief-plan$"), self._serve_plan)
        page.route(re.compile(r".*/api/board/chief/answered$"), self._record)


# getUserMedia + a recorder that hands over one blob on stop, as
# test_voice_dictation.py's single-shot mock does.
_RECORDER_MOCK = """
(() => {
  navigator.mediaDevices = navigator.mediaDevices || {};
  navigator.mediaDevices.getUserMedia = async () => ({ getTracks: () => [{ stop: () => {} }] });
""" + RECORDER_BASE_JS + """  FakeRecorder.isTypeSupported = () => true;
  window.MediaRecorder = FakeRecorder;
})()
"""
