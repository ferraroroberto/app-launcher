"""Issue #1050 — Chat keeps the visible conversation live, and only that one.

The phone-facing contract, end to end. What makes this feature correct is as
much what it *doesn't* fetch as what it shows, so most of these count
requests rather than looking at the screen:

  * a Chat view that is being looked at appends new turns on its own, with no
    tap — the thing Roberto asked for;
  * a window showing **Terminal** does not also fetch chat for that session,
    and a **closed** overlay fetches nothing at all — the "10 windows, and
    I'm not focusing on that terminal" constraint the design is built around;
  * only the open conversation refreshes, never another session;
  * appended turns do not rebuild what the reader is looking at: scroll
    position and an open disclosure survive a tick (the #680/#958 lesson,
    which is why refresh is incremental rather than a reload);
  * a session that ends stops the refresh and says so, instead of polling a
    dead session forever — while a *transient* unavailable source backs off
    and recovers on its own, because only an ended session is terminal.

Every boot fetch is stubbed before ``goto()`` (#510), and the transcript stub
serves a *growing* file: the newest page first, then forward-cursor reads
keyed on ``after``, exactly as the real route does.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.conftest import (
    OVERLAY_OPEN_MS,
    open_session_row,
    stable_eval,
    stable_read,
    stub_session_mirror,
)

pytestmark = pytest.mark.smoke

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
    menu = page.locator("#terminalOverlay .terminal-menu")
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


@pytest.mark.iphone
def test_new_turns_appear_with_no_user_action(authed_page: Page, base_url: str) -> None:
    """The feature: the conversation refreshes by itself."""
    page = authed_page
    tr = _boot(page, base_url)
    _open_chat(page)
    expect(page.locator("#transcriptList .tr-turn")).to_have_count(2)

    tr.append(_turn("assistant", "found it — a missing await", 300))
    # No tap, no reload: the next tick brings it in.
    _tick(page, tr)
    expect(page.locator("#transcriptList")).to_contain_text(
        "found it — a missing await", timeout=OVERLAY_OPEN_MS
    )
    expect(page.locator("#transcriptList .tr-turn")).to_have_count(3)

    # -- #1292: forcing a read, for when polling seems stuck --
    # ⋮ Load new with nothing new says so, briefly, and rebuilds nothing.
    # The note lives in the bottom strip (#1387), not over the transcript.
    newer = page.locator("#terminalActivity")
    expect(page.locator("#transcriptNewer")).to_have_count(0)
    page.evaluate("document.querySelector('#transcriptList .tr-turn')._kept = 1")
    _menu_item(page, "Load new messages").click()
    expect(newer).to_have_text("No new messages")
    _fake_clock.advance(page, 2_500)       # NEWER_NOTE_MS (2 s) in session-transcript.js
    expect(newer).to_be_hidden()

    # Timed against the poll: right after a tick's read lands, the next is a
    # full LIVE_POLL_MS (3 s) away, so a turn that shows within 1.5 s of the
    # tap came from the forced read, not the timer.
    _tick(page, tr)
    tr.append(_turn("user", "and the second flake?", 350))
    before = tr.tail_calls()
    _menu_item(page, "Load new messages").click()
    expect(page.locator("#transcriptList")).to_contain_text("and the second flake?", timeout=1_500)
    assert tr.tail_calls() > before
    # Appended, never rebuilt: the first turn is the same node.
    assert page.evaluate("document.querySelector('#transcriptList .tr-turn')._kept") == 1

    # A pull up past the bottom is the same read.
    _tick(page, tr)
    tr.append(_turn("assistant", "same cause, same fix", 380))
    page.evaluate("const b = document.getElementById('transcriptBody'); b.scrollTop = b.scrollHeight")
    _pull(page, -120)
    expect(page.locator("#transcriptList")).to_contain_text("same cause, same fix", timeout=1_500)
    # A drag too short to be a pull does nothing.
    calls = tr.tail_calls()
    _pull(page, -20)
    _fake_clock.advance(page, 1)           # only to let a (wrongly) fired read reach the stub
    assert tr.tail_calls() == calls

    # #1149 — a question the agent asks arrives the same way, as its own
    # card: visible with tool calls hidden (the default), never folded. An
    # earlier, answered question is history; the newest unanswered one is the
    # only card a tap can answer.
    answers: list = []
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + _SID + r"/answer$"),
        lambda route: (
            answers.append(route.request.post_data_json),
            route.fulfill(status=200, content_type="application/json",
                          body=_json.dumps({"ok": True, "delivered": "unconfirmed", "steps": 1})),
        )[-1],
    )
    tr.append(
        _question("toolu_old", 400, answer="Retry it"),
        _turn("assistant", "retrying did not help", 450),
        _question("toolu_live", 500),
    )
    _tick(page, tr)
    cards = page.locator("#transcriptList .tr-ask-item")
    expect(cards).to_have_count(2, timeout=OVERLAY_OPEN_MS)
    old, live = cards.nth(0), cards.nth(1)
    expect(old).to_be_visible()
    expect(old).to_have_attribute("data-mode", "answered")
    expect(old.locator(".tr-ask-opt--picked .tr-ask-label")).to_have_text("Retry it")
    expect(old.locator(".tr-ask-opt:enabled")).to_have_count(0)
    expect(live).to_have_attribute("data-mode", "live")
    expect(live.locator(".tr-ask-opt:enabled")).to_have_count(2)
    expect(live.locator(".tr-ask-input")).to_be_visible()

    # One tap answers a single-select question: the pick goes to the /answer
    # route by number, with the call it answers, and the card locks until the
    # agent's result lands.
    live.locator(".tr-ask-opt").first.click()
    expect(live).to_have_attribute("data-mode", "sent")
    expect(live.locator(".tr-ask-opt:enabled")).to_have_count(0)
    assert answers == [{"tool_use_id": "toolu_live", "answers": [{"option": 1}]}], answers

    # The result arrives on a later tick as a standalone tool_result (the
    # call had already settled), and the card still closes, marked answered.
    tr.append({
        "kind": "tool_result", "timestamp": "2026-09-19T10:01:09Z",
        "text": "answered", "truncated": False, "tool_use_id": "toolu_live",
        "answers": {"Which fix?": "Await it"}, "sidechain": False, "offset": 600,
    }, _turn("assistant", "adding the await", 700))
    _tick(page, tr)
    expect(live).to_have_attribute("data-mode", "answered", timeout=OVERLAY_OPEN_MS)
    expect(live.locator(".tr-ask-opt--picked .tr-ask-label")).to_have_text("Await it")
    expect(live.locator(".tr-ask-opt:enabled")).to_have_count(0)

    # #1151 — a plan the agent asks you to approve arrives as its own card
    # too: the plan through the markdown renderer, and how it was answered.
    # The card never offers a control itself; while Chat can't see the
    # picker on the terminal, a waiting plan says to answer it there.
    picker = _Picker(page)
    tr.append(
        _plan("plan_back", 800, error=True, plan_outcome="sent_back",
              plan_feedback="keep the retry as well"),
        _turn("assistant", "revised", 850),
        _plan("plan_ok", 900, plan_outcome="approved", plan_edited=True),
        _turn("assistant", "on it", 950),
        _plan("plan_wait", 1000),
    )
    _tick(page, tr)
    plans = page.locator("#transcriptList .tr-plan-item")
    expect(plans).to_have_count(3, timeout=OVERLAY_OPEN_MS)
    back, ok, wait = plans.nth(0), plans.nth(1), plans.nth(2)
    expect(back.locator(".tr-plan-body h2")).to_have_text("Add the await")
    expect(back.locator(".tr-plan-body li")).to_have_count(2)
    expect(back).to_have_attribute("data-mode", "sent_back")
    expect(back.locator(".tr-plan-feedback")).to_have_text("Your feedback: “keep the retry as well”")
    # A sent-back plan is the user's answer, not a failed call.
    expect(back.locator(".tr-fail-chip")).to_have_count(0)
    expect(ok.locator(".tr-ask-status")).to_have_text("Approved after your edits")
    expect(wait).to_have_attribute("data-mode", "waiting")
    expect(wait.locator(".tr-ask-status")).to_have_text(
        "Waiting for your answer: Chat can’t see the plan picker on the terminal, so answer it there"
    )
    expect(wait.locator("button")).to_have_count(0)
    # The question card above is history now that the agent moved on.
    expect(live).to_have_attribute("data-mode", "answered")

    # The terminal shows the picker: the panel below the list offers its
    # options exactly as the screen lists them — the BYPASS wording
    # included — and leaves out the plan the pending card already shows.
    panel = page.locator("#transcriptPlanLive")
    expect(panel).to_be_hidden()
    picker.show(plan="## Add the await\n\n- wrap the call")
    _tick(page, tr)
    expect(panel).to_be_visible(timeout=OVERLAY_OPEN_MS)
    expect(panel.locator(".tr-ask-opt .tr-ask-label")).to_have_text(
        [_BYPASS, "Yes, manually approve edits", "Tell Claude what to change"]
    )
    expect(panel.locator(".tr-plan-body")).to_have_count(0)
    expect(wait.locator(".tr-ask-status")).to_have_text("Waiting for your answer: answer it below")
    # A tap sends the digit with the label it showed, for the server to
    # check against the screen, and locks the panel.
    panel.locator("button.tr-ask-opt").first.click()
    expect(panel.locator(".tr-ask-status")).to_have_text("Sent: waiting for Claude")
    expect(panel.locator("button:enabled")).to_have_count(0)
    assert picker.answers == [{"option": 1, "label": _BYPASS, "feedback": None}], picker.answers
    # The picker closes on the terminal and the approval reaches the card.
    picker.showing = None
    _tick(page, tr)
    expect(panel).to_be_hidden(timeout=OVERLAY_OPEN_MS)
    tr.append({
        "kind": "tool_result", "timestamp": "2026-09-19T10:01:12Z", "text": "ok",
        "truncated": False, "tool_use_id": "plan_wait", "plan_outcome": "approved",
        "sidechain": False, "offset": 1100,
    }, _turn("assistant", "editing now", 1150))
    _tick(page, tr)
    expect(wait).to_have_attribute("data-mode", "approved", timeout=OVERLAY_OPEN_MS)

    # A picker whose call never reached the transcript: the panel carries
    # the plan itself (from the plan file the screen names), and sends it
    # back with the reader's feedback.
    picker.show(plan="## Revised plan\n\n- keep the retry")
    _tick(page, tr)
    expect(panel.locator(".tr-plan-body h2")).to_have_text("Revised plan", timeout=OVERLAY_OPEN_MS)
    send_back = panel.locator(".tr-ask-send")
    expect(send_back).to_be_disabled()
    # The field gets the row, not the button: .button-tint's width:100%
    # once squeezed it to a sliver and pushed Send back out of the card.
    def widths():
        boxes = [panel.locator(sel).bounding_box() for sel in (".tr-ask-input", ".tr-ask-send")]
        return None if None in boxes else [b["width"] for b in boxes]

    field_w, send_w = stable_read(widths)
    assert field_w > send_w, (field_w, send_w)
    panel.locator(".tr-ask-input").fill("keep it smaller")
    send_back.click()
    expect(panel.locator(".tr-ask-status")).to_have_text("Sent back: waiting for Claude to revise the plan")
    assert picker.answers[-1] == {
        "option": 3, "label": "Tell Claude what to change", "feedback": "keep it smaller",
    }, picker.answers


def test_terminal_mode_does_not_fetch_chat(authed_page: Page, base_url: str) -> None:
    """A window sitting on Terminal must not also stream chat for that
    session — half of the "10 windows" constraint."""
    page = authed_page
    tr = _boot(page, base_url)
    _open_chat(page)
    expect(page.locator("#transcriptList .tr-turn")).to_have_count(2)
    _tick(page, tr)          # at least one tick while Chat shows
    assert tr.tail_calls() >= 1, "Chat mode should have ticked at least once"

    page.locator("#sessionModeTerminal").click()
    expect(page.locator("#terminalOverlay")).to_have_attribute("data-mode", "terminal")
    settled = tr.tail_calls()
    _fake_clock.advance(page, 2 * _BACKOFF_MAX_MS)          # long enough for several ticks
    assert tr.tail_calls() == settled, (
        "Terminal mode kept fetching chat: "
        f"{tr.tail_calls() - settled} request(s) after the switch"
    )


def test_a_closed_overlay_fetches_nothing(authed_page: Page, base_url: str) -> None:
    page = authed_page
    tr = _boot(page, base_url)
    _open_chat(page)
    expect(page.locator("#transcriptList .tr-turn")).to_have_count(2)
    _tick(page, tr)
    assert tr.tail_calls() >= 1

    page.locator("#terminalBack").click()
    expect(page.locator("#terminalOverlay")).to_be_hidden()
    settled = tr.tail_calls()
    _fake_clock.advance(page, 2 * _BACKOFF_MAX_MS)
    assert tr.tail_calls() == settled, "a closed overlay kept polling"


def test_only_the_open_conversation_is_refreshed(
    authed_page: Page, base_url: str
) -> None:
    """The other live session's transcript is never asked for."""
    page = authed_page
    tr = _boot(page, base_url)
    other_calls: list[str] = []
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + _OTHER_SID + r"/transcript.*"),
        lambda route: (
            other_calls.append(route.request.url),
            route.fulfill(status=200, content_type="application/json", body="{}"),
        )[-1],
    )
    _open_chat(page)
    expect(page.locator("#transcriptList .tr-turn")).to_have_count(2)
    _tick(page, tr)
    assert tr.tail_calls() >= 1
    assert other_calls == [], "a session nobody opened was polled"


@pytest.mark.iphone
def test_an_appended_turn_leaves_scroll_and_open_cards_alone(
    authed_page: Page, base_url: str
) -> None:
    """The #680/#958 lesson: a poll that rebuilds the DOM breaks whatever the
    reader is interacting with. Refresh is incremental precisely so an open
    tool-call group and a scrolled-up reader both survive a tick."""
    page = authed_page
    tr = _boot(page, base_url)
    tr.append(
        _tool("Bash", "pytest tests/e2e -q", "3 passed", 150),
        *[_turn("user", f"filler prompt {i}", 1000 + i * 10) for i in range(20)],
    )
    _open_chat(page)
    # Reveal the folded groups and open one, then scroll up off the tail.
    page.locator("#terminalMenu").click()
    page.get_by_role("menuitem", name=re.compile("tool calls")).click()
    group = page.locator("#transcriptList .tr-group").first
    group.click()
    expect(group).to_have_attribute("open", "")
    # Park the reader partway up the history — deliberately *not* at 0,
    # where a full rebuild would also land, so this position distinguishes
    # "left alone" from both failure modes: reset to the top and jump to the
    # bottom.
    body = page.locator("#transcriptBody")
    body.evaluate("el => { el.scrollTop = Math.round(el.scrollHeight * 0.4); }")
    before = stable_read(lambda: body.evaluate("el => el.scrollTop"))
    assert before, "the transcript did not scroll; the fixture is too short"

    tr.append(_turn("assistant", "a brand new reply", 5000))
    _tick(page, tr)
    expect(page.locator("#transcriptList")).to_contain_text(
        "a brand new reply", timeout=OVERLAY_OPEN_MS
    )
    # The group the reader opened is still open, and they are still where
    # they were: an append must not scroll them to the bottom.
    expect(group).to_have_attribute("open", "")
    # A raw geometry read on a surface that now re-renders on a poll, so it
    # goes through stable_read (#680) — the list rebuilds only its newest
    # message, but the convention holds for any polled surface.
    after = stable_read(lambda: body.evaluate("el => el.scrollTop"))
    assert after == before, f"the reader was moved: {before} -> {after}"


def test_a_session_that_ends_stops_refreshing_and_says_so(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    tr = _boot(page, base_url)
    _open_chat(page)
    expect(page.locator("#transcriptList .tr-turn")).to_have_count(2)

    tr.dead = True
    _tick(page, tr)
    expect(page.locator("#transcriptState")).to_contain_text(
        "no longer running", timeout=OVERLAY_OPEN_MS
    )
    settled = tr.tail_calls()
    _fake_clock.advance(page, 2 * _BACKOFF_MAX_MS)
    assert tr.tail_calls() == settled, "a dead session was still being polled"
    # What was already read stays readable.
    expect(page.locator("#transcriptList .tr-turn")).to_have_count(2)


def test_a_transient_unavailable_source_recovers_without_a_tap(
    authed_page: Page, base_url: str
) -> None:
    """Only an *ended session* is terminal. A live Claude session reports
    `no_transcript` for as long as its hook row is deleted and the filesystem
    fallback can't name the file (#1023) — a condition that clears by itself,
    so refresh must back off and recover rather than latch off and make the
    reader tap Reload.
    """
    page = authed_page
    tr = _boot(page, base_url)
    _open_chat(page)
    expect(page.locator("#transcriptList .tr-turn")).to_have_count(2)

    tr.unavailable = "no_transcript"
    _tick(page, tr)
    expect(page.locator("#transcriptState")).to_contain_text(
        "No transcript found", timeout=OVERLAY_OPEN_MS
    )
    # It is still trying, unlike the ended case above.
    settled = tr.tail_calls()
    _tick(page, tr)
    assert tr.tail_calls() > settled, "a transient condition latched refresh off"

    tr.unavailable = None
    tr.append(_turn("assistant", "back again", 400))
    _tick(page, tr)
    expect(page.locator("#transcriptList")).to_contain_text(
        "back again", timeout=OVERLAY_OPEN_MS
    )
    expect(page.locator("#transcriptState")).to_be_hidden()


@pytest.mark.iphone
def test_latest_pill_brings_a_scrolled_up_reader_back_to_the_newest_turn(
    authed_page: Page, base_url: str
) -> None:
    """#1140: the shared ↓ Latest pill, mounted on the Chat pane. Scrolled up,
    it shows — and stays shown while live turns land (the reader is still
    not looking at them). One tap lands on the newest turn, hides it, and
    hands the pane back to #1050's stick-to-bottom, so the next live turn
    appears on screen by itself."""
    page = authed_page
    tr = _boot(page, base_url)
    tr.append(*[
        _turn("user" if i % 2 == 0 else "assistant",
              f"filler turn {i} " + "with enough words to wrap a line " * 3,
              1000 + i * 10)
        for i in range(30)
    ])
    _open_chat(page)
    listing = page.locator("#transcriptList")
    expect(listing).to_contain_text("filler turn 29", timeout=OVERLAY_OPEN_MS)
    pill = page.locator("#chatLatest")
    expect(pill).to_have_count(1)
    # Opens at the tail: nothing to jump to.
    expect(pill).to_be_hidden()

    page.locator("#transcriptBody").evaluate(
        "el => { el.scrollTop = Math.round(el.scrollHeight * 0.3); }"
    )
    expect(pill).to_be_visible()

    tr.append(_turn("assistant", "a brand new reply", 5000))
    _tick(page, tr)
    expect(listing).to_contain_text("a brand new reply", timeout=OVERLAY_OPEN_MS)
    expect(pill).to_be_visible()

    pill.click()
    expect(pill).to_be_hidden()
    expect(listing.locator(".tr-turn", has_text="a brand new reply")).to_be_in_viewport()

    # Sticking resumed: the next live turn arrives on screen with no scroll.
    tr.append(_turn("user", "and one more after the jump", 6000))
    _tick(page, tr)
    expect(
        listing.locator(".tr-turn", has_text="and one more after the jump")
    ).to_be_in_viewport(timeout=OVERLAY_OPEN_MS)
    expect(pill).to_be_hidden()


@pytest.mark.iphone
def test_a_resume_launch_with_no_transcript_shows_the_card_in_place_of_the_empty_state(
    authed_page: Page, base_url: str
) -> None:
    """#1300, reopened: a session launched with `--resume` has no transcript
    yet, and its screen shows the resume picker from the start. The first
    load's `no_transcript` used to end the view, so the screen was never
    asked about and Chat showed only "No transcript found". The card must
    come up in that line's place, and the line comes back once it is ✕'d."""
    page = authed_page
    picker = _Picker(page)
    picker.resume_up = True
    _Resume(page)
    tr = _boot(page, base_url)
    tr.unavailable = "no_transcript"
    _open_chat(page)

    card = page.locator("#transcriptResumeLive")
    state = page.locator("#transcriptState")
    _tick(page, tr)
    expect(card).to_be_visible(timeout=OVERLAY_OPEN_MS)
    expect(card.locator(".tr-resume-opt")).to_have_count(3)
    expect(state).to_be_hidden()
    box = stable_read(card.bounding_box)
    viewport = page.viewport_size
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= viewport["width"] + 1, box

    card.locator(".tr-resume-close").click()
    expect(card).to_be_hidden()
    expect(state).to_contain_text("No transcript found")

    # The view stayed live: once a transcript appears, its turns land by
    # themselves and the reason line goes with them.
    tr.unavailable = None
    _tick(page, tr)
    expect(page.locator("#transcriptList")).to_contain_text(
        "opening conftest now", timeout=OVERLAY_OPEN_MS
    )
    expect(state).to_be_hidden()


@pytest.mark.iphone
def test_the_resume_card_lists_searches_and_resumes_from_the_picker_or_the_composer(
    authed_page: Page, base_url: str
) -> None:
    """#1300: while the terminal shows Claude Code's /resume picker, Chat
    offers the project's sessions as a searchable card, newest first; a pick
    posts that session with via=picker and the card goes. Typing /resume in
    the composer opens the same card instead of sending anything to the
    terminal, and a pick from it posts via=composer."""
    page = authed_page
    tr = _boot(page, base_url)
    picker = _Picker(page)
    resume = _Resume(page)
    typed: list = []
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + _SID + r"/input$"),
        lambda route: (typed.append(route.request.post_data_json),
                       route.fulfill(status=200, content_type="application/json",
                                     body=_json.dumps({"ok": True, "submit_state": "confirmed"}))),
    )
    _open_chat(page)
    card = page.locator("#transcriptResumeLive")
    expect(card).to_be_hidden()

    picker.resume_up = True
    _tick(page, tr)
    expect(card).to_be_visible(timeout=OVERLAY_OPEN_MS)
    rows = card.locator(".tr-resume-opt")
    expect(rows).to_have_count(3)
    expect(rows.locator(".tr-ask-label")).to_have_text(
        ["Fix the login redirect", "Add a dark theme", "Tidy the flaky test"])
    card.locator(".tr-resume-search").fill("THEME dark")
    shown = card.locator(".tr-resume-opt:not([hidden])")
    expect(shown).to_have_count(1)
    expect(shown).to_contain_text("Add a dark theme")
    card.locator(".tr-resume-search").fill("nothing like this")
    expect(shown).to_have_count(0)
    expect(card.locator(".tr-resume-none")).to_be_visible()
    card.locator(".tr-resume-search").fill("theme")
    box = stable_read(card.locator(".tr-resume-close").bounding_box)
    assert box and box["height"] >= 44 and box["width"] >= 44, box
    # The ✕ is a ghost icon button like the Jobs run list's, not the UA's grey
    # square: same computed fill, border, glyph colour and radius as a probe
    # built from that real rule, and its colour reacts on hover (pointer only).
    ghost = (
        "el => { const s = getComputedStyle(el); return el.isConnected ? "
        "{bg: s.backgroundColor, border: s.borderTopWidth, color: s.color, "
        "radius: s.borderTopLeftRadius} : null; }"
    )
    probe = (
        "() => { const ul = document.createElement('ul'); ul.className = 'jobs-runs-list';"
        "ul.hidden = true; const li = document.createElement('li');"
        "const b = document.createElement('button'); b.className = 'icon-button';"
        "li.appendChild(b); ul.appendChild(li); document.body.appendChild(ul);"
        "const s = getComputedStyle(b); const out = {bg: s.backgroundColor,"
        "border: s.borderTopWidth, color: s.color, radius: s.borderTopLeftRadius};"
        "ul.remove(); return out; }"
    )
    close = card.locator(".tr-resume-close")
    sibling = page.evaluate(probe)
    assert sibling["bg"] == "rgba(0, 0, 0, 0)" and sibling["border"] == "0px", sibling
    assert stable_eval(close, ghost) == sibling, sibling
    # Hover only exists on a pointer device; the touch projection has none.
    if page.evaluate("matchMedia('(hover: hover)').matches"):
        close.hover()
        assert stable_eval(close, ghost)["color"] != sibling["color"]
    shown.first.click()
    expect(page.locator("#toast")).to_contain_text("Resumed: Add a dark theme")
    assert resume.picks == [{"session_id": _RESUMABLE[1]["id"], "via": "picker"}], resume.picks
    expect(card).to_be_hidden()

    # The picker closed on the terminal; /resume typed in Chat opens the card.
    picker.resume_up = False
    composer = page.locator("#chatComposeBar")
    composer.locator(".composer-input").fill("/resume")
    composer.locator(".composer-send").click()
    expect(card).to_be_visible()
    expect(composer.locator(".composer-input")).to_have_value("")
    card.locator(".tr-resume-opt").first.click()
    expect(page.locator("#toast")).to_contain_text("Resumed: Fix the login redirect")
    assert resume.picks[-1] == {"session_id": _RESUMABLE[0]["id"], "via": "composer"}, resume.picks
    assert typed == [], "the bare /resume must never reach the terminal"


def _since(page: Page, seconds: int) -> str:
    """An ISO stamp ``seconds`` before the page's (fake) now."""
    return page.evaluate("(s) => new Date(Date.now() - s * 1000).toISOString()", seconds)


@pytest.mark.iphone
def test_the_strip_shows_the_turn_in_progress_and_gives_way_to_status(
    authed_page: Page, base_url: str
) -> None:
    """#1387: one line in the bottom strip while a turn runs — elapsed time,
    the action count and the newest step — ticking on the client with no
    request of its own, gone when the turn ends, and never over a
    connection status."""
    page = authed_page
    tr = _boot(page, base_url)
    tr.activity = {"since": _since(page, 902), "actions": 10, "last": "Running Bash", "working": True}
    _open_chat(page)

    line = page.locator("#terminalActivity")
    expect(line).to_have_text(re.compile(r"^15:0\d · 10 actions · Running Bash$"), timeout=OVERLAY_OPEN_MS)
    # One line that ellipsizes, with tabular numerals — and the strip's box is
    # the one it already reserved when idle, so nothing above it moved.
    expect(line).to_have_css("white-space", "nowrap")
    expect(line.locator(".terminal-activity-text")).to_have_css("text-overflow", "ellipsis")
    expect(line).to_have_css("font-variant-numeric", "tabular-nums")
    strip = page.locator("#terminalStatusStrip")
    assert stable_eval(strip, "el => el.isConnected ? el.getBoundingClientRect().height : null")         == stable_eval(line, "el => el.isConnected ? el.parentElement.getBoundingClientRect().height : null")

    # The counter moves on the client: 2 s on the clock is under the 3 s poll,
    # so no read goes out, yet the text has advanced.
    shown = line.text_content()
    before = tr.tail_calls()
    _fake_clock.advance(page, 2_000)
    assert tr.tail_calls() == before
    assert line.text_content() != shown

    # Connection status wins the row while it shows, and the line returns.
    page.evaluate("() => { const s = document.getElementById('terminalStatus');"
                  "s.textContent = 'Reconnecting…'; s.hidden = false; }")
    expect(line).to_be_hidden()
    page.evaluate("() => { const s = document.getElementById('terminalStatus');"
                  "s.hidden = true; s.textContent = ''; }")
    expect(line).to_be_visible()

    # A harness with no tool data gives the time alone — never "0 actions".
    tr.activity = {"since": tr.activity["since"], "actions": None, "last": "Thinking", "working": True}
    _tick(page, tr)
    expect(line).to_have_text(re.compile(r"\d+:\d\d · Thinking$"))

    # The turn ends: the line goes, and its timer with it.
    tr.activity = {**tr.activity, "working": False}
    _tick(page, tr)
    expect(line).to_be_hidden()


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


@pytest.mark.iphone
def test_the_activity_line_is_an_icon_and_sits_in_the_middle_of_its_strip_at_phone_width(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    """#1394: the timer is the vendored `i-timer` icon in the line's muted
    colour, not the ⏱ emoji (iOS draws a colour stopwatch, desktop another
    glyph), and at 390 px the line's vertical centre is within 1 px of the
    strip's. The icon is centred on the text, so it cannot lift or sink it."""
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844})
    tr = _boot(page, base_url)
    tr.activity = {"since": _since(page, 902), "actions": 10, "last": "Running Bash", "working": True}
    _open_chat(page)
    line = page.locator("#terminalActivity")
    expect(line).to_have_text(re.compile(r"^15:0\d · 10 actions · Running Bash$"), timeout=OVERLAY_OPEN_MS)
    # A connection status owns the row while the mirror is down, and the
    # stubbed mirror re-shows it whenever it reconnects (WebKit, under load):
    # a style rule keeps it off for the whole measurement, where flipping
    # `hidden` once would be undone before the read.
    page.evaluate("""() => {
      const s = document.getElementById('terminalStatus');
      const hide = () => { if (!s.hidden) { s.hidden = true; s.textContent = ''; } };
      new MutationObserver(hide).observe(s, { attributes: true, childList: true, characterData: true, subtree: true });
      hide();
    }""")
    expect(line).to_be_visible()

    got = stable_eval(line, _STRIP_LINE_JS)
    assert "⏱" not in got["text"], got
    assert got["use"] == "#i-timer", got
    assert got["iconColor"] == got["lineColor"], f"the icon is not in the line's muted colour: {got}"
    assert 12 <= got["iconSize"] <= 18, got
    assert abs(got["spanMid"] - got["stripMid"]) <= 1, (
        f"the line's centre is {got['spanMid'] - got['stripMid']:+.1f}px off the strip's: {got}"
    )
    assert abs(got["iconMid"] - got["textMid"]) <= 1.5, f"the icon is off the text's centre: {got}"

    # The strip is max(row, home-indicator inset) tall (#1219), so a taller
    # inset leaves slack the line must split evenly, not keep at its top.
    # Chromium can emulate an inset over CDP; WebKit has no such override.
    if browser_name == "chromium":
        cdp = page.context.new_cdp_session(page)
        cdp.send("Emulation.setSafeAreaInsetsOverride", {"insets": {"bottom": 56, "bottomMax": 56}})
        tall = stable_eval(line, _STRIP_LINE_JS)
        assert tall["stripMid"] != got["stripMid"], f"the emulated inset did not reach the strip: {tall}"
        assert abs(tall["spanMid"] - tall["stripMid"]) <= 1, (
            f"in a taller strip the line is {tall['spanMid'] - tall['stripMid']:+.1f}px off its centre: {tall}"
        )
