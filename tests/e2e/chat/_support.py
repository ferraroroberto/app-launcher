"""Shared mocks for the Chat pane tests: the session list, a scripted transcript, the resume picker and the fake clock tick."""

from __future__ import annotations

import json as _json
import re

from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.fixtures.ui import open_session_row, stub_session_mirror


_SID = "sid-live-1050"


_OTHER_SID = "sid-other-1050"


def _mock_sessions_list(page: Page, *, alive: bool = True) -> None:
    """Two live rows, so "only the open conversation refreshes" is testable."""
    def _handler(route):
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"sessions": [
                {
                    "session_id": _SID, "kind": "pty", "agent": "claude",
                    "project_dir": "E:/automation/liveproj", "name": "liveproj",
                    "alive": alive, "started_at": "2026-09-19T10:00:00Z",
                    "live_title": "", "prompt_title": "", "manual_title": "Live demo",
                },
                {
                    "session_id": _OTHER_SID, "kind": "pty", "agent": "claude",
                    "project_dir": "E:/automation/otherproj", "name": "otherproj",
                    "alive": True, "started_at": "2026-09-19T10:00:00Z",
                    "live_title": "", "prompt_title": "", "manual_title": "Other demo",
                },
            ]}),
        )

    page.route(re.compile(r".*/api/claude-code/sessions$"), _handler)


_QUESTIONS = [{
    "question": "Which fix?", "header": "Fix", "multiSelect": False,
    "options": [
        {"label": "Await it", "description": "add the missing await"},
        {"label": "Retry it", "description": "wrap it in a retry"},
    ],
}]


def _question(call_id: str, offset: int, answer: str | None = None) -> dict:
    """An AskUserQuestion call as the server forwards it (#1149); answered
    when ``answer`` is given, pending otherwise."""
    e = {
        "kind": "tool_call", "timestamp": "2026-09-19T10:01:02Z",
        "name": "AskUserQuestion", "summary": "questions", "call_id": call_id,
        "questions": _QUESTIONS, "result": None, "result_truncated": False,
        "sidechain": False, "offset": offset,
    }
    if answer is not None:
        e["result"] = f'User has answered your questions: "Which fix?"="{answer}".'
        e["answers"] = {"Which fix?": answer}
    return e


def _plan(call_id: str, offset: int, **outcome) -> dict:
    """An ExitPlanMode call as the server forwards it (#1151); ``outcome``
    carries the result fields for an answered one."""
    e = {
        "kind": "tool_call", "timestamp": "2026-09-19T10:01:02Z",
        "name": "ExitPlanMode", "summary": "# Add the await", "call_id": call_id,
        "plan": "## Add the await\n\n- wrap the call\n- rerun the suite",
        "plan_truncated": False, "result": None, "result_truncated": False,
        "sidechain": False, "offset": offset,
    }
    if outcome:
        e["result"] = "answered"
        e.update(outcome)
    return e


_BYPASS = "Yes, and switch to BYPASS PERMISSIONS (no further prompts) for this session"


class _Picker:
    """A stub of the plan-picker routes (#1151): what the server read off the
    terminal's screen (``None``: no picker), and every answer posted."""

    def __init__(self, page: Page, sid: str = _SID) -> None:
        self.showing: dict | None = None
        self.answers: list = []
        # Whether the same screen read sees Claude Code's /resume picker (#1300).
        self.resume_up = False
        base = r".*/api/claude-code/sessions/" + sid
        page.route(re.compile(base + r"/plan-picker$"), self._read)
        page.route(re.compile(base + r"/plan-answer$"), self._answer)

    def show(self, plan: str | None = None) -> None:
        self.showing = {
            "available": True, "showing": True, "answerable": True, "reason": None,
            "cursor": 1, "plan": plan, "plan_source": "file" if plan else None,
            "plan_truncated": False,
            "options": [
                {"n": 1, "label": _BYPASS, "kind": "approve"},
                {"n": 2, "label": "Yes, manually approve edits", "kind": "approve"},
                {"n": 3, "label": "Tell Claude what to change", "kind": "feedback"},
            ],
        }

    def _read(self, route) -> None:
        body = self.showing or {
            "available": True, "showing": False, "answerable": False,
            "reason": "not_showing", "options": [], "cursor": None,
            "plan": None, "plan_source": None, "plan_truncated": False,
        }
        body = {**body, "resume_picker": self.resume_up}
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(body))

    def _answer(self, route) -> None:
        self.answers.append(route.request.post_data_json)
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps({"ok": True, "delivered": "unconfirmed", "answer": "approve"}))


# The project's resumable conversations as the server lists them (#1300):
# newest first, titles and times only.
_RESUMABLE = [
    {"id": "00000001-0000-4000-8000-000000000001", "title": "Fix the login redirect",
     "updated_at": "2026-09-19T09:58:00Z"},
    {"id": "00000002-0000-4000-8000-000000000002", "title": "Add a dark theme",
     "updated_at": "2026-09-19T09:30:00Z"},
    {"id": "00000003-0000-4000-8000-000000000003", "title": "Tidy the flaky test",
     "updated_at": "2026-09-18T17:00:00Z"},
]


class _Resume:
    """A stub of the resume routes (#1300): the list, and every pick posted."""

    def __init__(self, page: Page, sid: str = _SID) -> None:
        self.picks: list = []
        base = r".*/api/claude-code/sessions/" + sid
        page.route(re.compile(base + r"/resume-sessions$"), self._list)
        page.route(re.compile(base + r"/resume$"), self._pick)

    def _list(self, route) -> None:
        route.fulfill(status=200, content_type="application/json", body=_json.dumps({
            "available": True, "reason": None, "picker": True, "sessions": _RESUMABLE,
        }))

    def _pick(self, route) -> None:
        body = route.request.post_data_json
        self.picks.append(body)
        title = next(r["title"] for r in _RESUMABLE if r["id"] == body["session_id"])
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps({"ok": True, "outcome": "resumed", "title": title}))


def _turn(kind: str, text: str, offset: int) -> dict:
    return {
        "kind": kind, "timestamp": "2026-09-19T10:01:00Z", "text": text,
        "truncated": False, "sidechain": False, "offset": offset,
    }


def _tool(name: str, summary: str, result: str, offset: int) -> dict:
    return {
        "kind": "tool_call", "timestamp": "2026-09-19T10:01:02Z", "name": name,
        "summary": summary, "result": result, "result_truncated": False,
        "sidechain": False, "offset": offset,
    }


class _Transcript:
    """A stub of the real route, both directions.

    Holds a list of entries with byte-ish offsets and answers a page request
    with all of them, a forward request (``after=``) with the ones past that
    offset. ``tail`` is the newest entry's offset — the provisional region
    the real reader holds back — so the client's split behaves as it does
    against a live file.
    """

    def __init__(self, page: Page, sid: str = _SID) -> None:
        self.entries: list[dict] = []
        self.calls: list[str] = []
        self.dead = False
        # A non-terminal reason the source is unavailable, e.g. the #1023
        # rowless window. Distinct from `dead` on purpose: one latches
        # refresh off, the other must not.
        self.unavailable: str | None = None
        # The `activity` the server rides on every page/tail answer (#1387).
        self.activity: dict | None = None
        self._page = page
        page.route(
            re.compile(r".*/api/claude-code/sessions/" + sid + r"/transcript(\?.*)?$"),
            self._handle,
        )

    # -- what the server would compute ------------------------------------
    @property
    def tail(self) -> int:
        return self.entries[-1]["offset"] if self.entries else 0

    @property
    def size(self) -> int:
        return self.tail + 1

    def _handle(self, route) -> None:
        url = route.request.url
        self.calls.append(url)
        reason = "session_not_found" if self.dead else self.unavailable
        if reason:
            route.fulfill(
                status=200, content_type="application/json",
                body=_json.dumps({
                    "available": False, "reason": reason,
                    "entries": [], "next_cursor": None, "session_id": _SID,
                }),
            )
            return
        m = re.search(r"[?&]after=(\d+)", url)
        if m is None:
            body = {
                "available": True, "source": "native", "reason": None,
                "session_id": _SID, "next_cursor": None,
                "entries": list(self.entries), "tail": self.tail,
                "size": self.size, "tool_errors": "reported",
                "activity": self.activity,
            }
        else:
            after = int(m.group(1))
            sm = re.search(r"[?&]size=(\d+)", url)
            fresh = [e for e in self.entries if e["offset"] >= after]
            changed = not (sm and int(sm.group(1)) == self.size)
            body = {
                "available": True, "source": "native", "reason": None,
                "session_id": _SID, "changed": changed, "reset": False,
                "entries": fresh[:-1] if changed else [],
                "pending": fresh[-1:] if changed else [],
                "tail": self.tail, "size": self.size,
                "tool_errors": "reported", "activity": self.activity,
            }
        route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)
        )

    # -- driving it --------------------------------------------------------
    def append(self, *entries: dict) -> None:
        self.entries.extend(entries)

    def tail_calls(self) -> int:
        return sum(1 for u in self.calls if "after=" in u)


def _row(page: Page, sid: str = _SID):
    return page.locator(f'#sessionsList li[data-session-id="{sid}"]')


def _open_chat(page: Page, sid: str = _SID) -> None:
    stub_session_mirror(page)
    open_session_row(page, _row(page, sid), mode="chat")


def _pull(page: Page, dy: int) -> None:
    """Drag one finger ``dy`` px on the Chat list (+ down, - up), as #1292's
    pull gestures read it: a touchstart, then a touchend ``dy`` further on."""
    page.evaluate("""(dy) => {
        const box = document.getElementById('transcriptBody');
        const fire = (type, y) => {
            const ev = new Event(type, {bubbles: true});
            const pt = [{clientX: 20, clientY: y}];
            Object.defineProperty(ev, 'touches', {value: type === 'touchend' ? [] : pt});
            Object.defineProperty(ev, 'changedTouches', {value: pt});
            box.dispatchEvent(ev);
        };
        fire('touchstart', 300);
        fire('touchend', 300 + dy);
    }""", dy)


def _menu_item(page: Page, name: str):
    page.locator("#terminalMenu").click()
    menu = page.locator(".terminal-menu")
    expect(menu).to_be_visible()
    return menu.get_by_role("menuitem", name=name)


def _boot(page: Page, base_url: str, alive: bool = True) -> _Transcript:
    _mock_sessions_list(page, alive=alive)
    tr = _Transcript(page)
    tr.append(
        _turn("user", "please look at the flaky test", 100),
        _turn("assistant", "opening conftest now", 200),
    )
    # The live poll is a plain setTimeout: the tests jump it (#1376) rather
    # than wait it out.
    _fake_clock.install(page)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    return tr


# session-transcript.js: LIVE_POLL_MS is 3 s, and a failing or unavailable
# source backs off up to LIVE_BACKOFF_MAX_MS.
_POLL_MS = 3_000


_BACKOFF_MAX_MS = 30_000


def _tick(page: Page, tr: _Transcript) -> None:
    """Fire the next live-poll read and return once the stub has seen it.

    One poll interval is the smallest jump that can fire the armed timer, and
    a tick re-arms only after its request resolves, so a longer jump would
    run a whole chain of ticks (each a real round trip). The loop covers the
    beat where the previous tick has not re-armed yet, and a backed-off
    source (up to ``_BACKOFF_MAX_MS``). Every pass is a real round trip,
    never a sleep.
    """
    before = tr.tail_calls()
    for _ in range(_BACKOFF_MAX_MS // _POLL_MS + 5):
        _fake_clock.advance(page, _POLL_MS + 1)
        if tr.tail_calls() > before:
            return
    raise AssertionError("the live poll never fired on the fake clock")


def _since(page: Page, seconds: int) -> str:
    """An ISO stamp ``seconds`` before the page's (fake) now."""
    return page.evaluate("(s) => new Date(Date.now() - s * 1000).toISOString()", seconds)


def _line_shown_with_status(page: Page, message: str | None) -> bool:
    """Put ``message`` in the connection status (None clears it) and say
    whether the strip's live line has a box, read in the same task."""
    return page.evaluate(
        """(m) => {
          const s = document.getElementById('terminalStatus');
          s.textContent = m || '';
          s.hidden = !m;
          const r = document.getElementById('terminalActivity').getBoundingClientRect();
          return r.width > 0 && r.height > 0;
        }""",
        message,
    )


# The line's own box and its strip, in one evaluate so a re-render between two
# reads cannot hand back two different nodes (#680, #1346). `span` is the
# union of everything the line drew (icon and text): its vertical centre is
# what the eye reads as "where the line sits".
_STRIP_LINE_JS = """el => {
  if (!el.isConnected) return null;
  const strip = el.parentElement.getBoundingClientRect();
  const icon = el.querySelector('svg.icon');
  const text = el.querySelector('.terminal-activity-text');
  const box = icon && icon.getBoundingClientRect();
  const tbox = text && text.getBoundingClientRect();
  const boxes = [box, tbox].filter(Boolean);
  const top = Math.min(...boxes.map(b => b.top));
  const bottom = Math.max(...boxes.map(b => b.bottom));
  const span = { top: top, height: bottom - top };
  const cs = getComputedStyle(el);
  return {
    stripMid: strip.top + strip.height / 2,
    spanMid: span.top + span.height / 2,
    iconMid: box ? box.top + box.height / 2 : null,
    iconSize: box ? box.height : null,
    iconColor: icon ? getComputedStyle(icon).color : null,
    textMid: tbox ? tbox.top + tbox.height / 2 : null,
    lineColor: cs.color,
    text: el.textContent,
    use: icon && icon.querySelector('use') ? icon.querySelector('use').getAttribute('href') : null,
  };
}"""


# --- #1744: say Sent, then confirm, and warn only when the look fails --------

# send-confirm.js: CONFIRM_WINDOW_MS.
_CONFIRM_WINDOW_MS = 15_000


_UNCONFIRMED_SEND = {
    "ok": True, "bytes": 8, "submit": True, "delivered": True, "reason": "unverified",
    "ingested": True, "submitted": True, "submit_confirmed": None,
    "submit_state": "unconfirmed", "waited_ms": 40, "deferred": False,
}


def _send_unconfirmed(page: Page, text: str) -> None:
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + _SID + r"/input$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(_UNCONFIRMED_SEND)),
    )
    _open_chat(page)
    composer = page.locator("#chatComposeBar")
    composer.locator(".composer-input").fill(text)
    composer.locator(".composer-send").click()
    expect(composer.locator(".composer-input")).to_have_value("")
    # Said at once, and never the old warning: nothing has looked yet.
    expect(page.locator("#toast")).to_have_text(re.compile(r"^\W*Sent$"))
