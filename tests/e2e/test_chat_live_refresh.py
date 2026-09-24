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

from tests.e2e.conftest import (
    OVERLAY_OPEN_MS,
    open_session_row,
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
                "tool_errors": "reported",
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


def _boot(page: Page, base_url: str, alive: bool = True) -> _Transcript:
    _mock_sessions_list(page, alive=alive)
    tr = _Transcript(page)
    tr.append(
        _turn("user", "please look at the flaky test", 100),
        _turn("assistant", "opening conftest now", 200),
    )
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    return tr


def test_new_turns_appear_with_no_user_action(authed_page: Page, base_url: str) -> None:
    """The feature: the conversation refreshes by itself."""
    page = authed_page
    tr = _boot(page, base_url)
    _open_chat(page)
    expect(page.locator("#transcriptList .tr-turn")).to_have_count(2)

    tr.append(_turn("assistant", "found it — a missing await", 300))
    # No tap, no reload: the next tick brings it in.
    expect(page.locator("#transcriptList")).to_contain_text(
        "found it — a missing await", timeout=OVERLAY_OPEN_MS
    )
    expect(page.locator("#transcriptList .tr-turn")).to_have_count(3)

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
    expect(live).to_have_attribute("data-mode", "answered", timeout=OVERLAY_OPEN_MS)
    expect(live.locator(".tr-ask-opt--picked .tr-ask-label")).to_have_text("Await it")
    expect(live.locator(".tr-ask-opt:enabled")).to_have_count(0)


def test_terminal_mode_does_not_fetch_chat(authed_page: Page, base_url: str) -> None:
    """A window sitting on Terminal must not also stream chat for that
    session — half of the "10 windows" constraint."""
    page = authed_page
    tr = _boot(page, base_url)
    _open_chat(page)
    expect(page.locator("#transcriptList .tr-turn")).to_have_count(2)
    page.wait_for_timeout(3500)          # at least one tick while Chat shows
    assert tr.tail_calls() >= 1, "Chat mode should have ticked at least once"

    page.locator("#sessionModeTerminal").click()
    expect(page.locator("#terminalOverlay")).to_have_attribute("data-mode", "terminal")
    settled = tr.tail_calls()
    page.wait_for_timeout(5000)          # long enough for several ticks
    assert tr.tail_calls() == settled, (
        "Terminal mode kept fetching chat: "
        f"{tr.tail_calls() - settled} request(s) after the switch"
    )


def test_a_closed_overlay_fetches_nothing(authed_page: Page, base_url: str) -> None:
    page = authed_page
    tr = _boot(page, base_url)
    _open_chat(page)
    expect(page.locator("#transcriptList .tr-turn")).to_have_count(2)
    page.wait_for_timeout(3500)
    assert tr.tail_calls() >= 1

    page.locator("#terminalBack").click()
    expect(page.locator("#terminalOverlay")).to_be_hidden()
    settled = tr.tail_calls()
    page.wait_for_timeout(5000)
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
    page.wait_for_timeout(4000)
    assert tr.tail_calls() >= 1
    assert other_calls == [], "a session nobody opened was polled"


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
    expect(page.locator("#transcriptState")).to_contain_text(
        "no longer running", timeout=OVERLAY_OPEN_MS
    )
    settled = tr.tail_calls()
    page.wait_for_timeout(5000)
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
    expect(page.locator("#transcriptState")).to_contain_text(
        "No transcript found", timeout=OVERLAY_OPEN_MS
    )
    # It is still trying, unlike the ended case above.
    settled = tr.tail_calls()
    page.wait_for_timeout(6000)
    assert tr.tail_calls() > settled, "a transient condition latched refresh off"

    tr.unavailable = None
    tr.append(_turn("assistant", "back again", 400))
    expect(page.locator("#transcriptList")).to_contain_text(
        "back again", timeout=OVERLAY_OPEN_MS
    )
    expect(page.locator("#transcriptState")).to_be_hidden()


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
    expect(listing).to_contain_text("a brand new reply", timeout=OVERLAY_OPEN_MS)
    expect(pill).to_be_visible()

    pill.click()
    expect(pill).to_be_hidden()
    expect(listing.locator(".tr-turn", has_text="a brand new reply")).to_be_in_viewport()

    # Sticking resumed: the next live turn arrives on screen with no scroll.
    tr.append(_turn("user", "and one more after the jump", 6000))
    expect(
        listing.locator(".tr-turn", has_text="and one more after the jump")
    ).to_be_in_viewport(timeout=OVERLAY_OPEN_MS)
    expect(pill).to_be_hidden()
