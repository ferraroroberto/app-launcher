"""Chat live refresh: new turns appear with no tap, only the visible chat polls, and scroll and open cards survive."""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.fixtures.waits import OVERLAY_OPEN_MS, stable_read

from tests.e2e.chat._support import (
    _BACKOFF_MAX_MS,
    _BYPASS,
    _OTHER_SID,
    _Picker,
    _SID,
    _Transcript,
    _boot,
    _menu_item,
    _mock_sessions_list,
    _open_chat,
    _plan,
    _pull,
    _question,
    _tick,
    _tool,
    _turn,
)


pytestmark = pytest.mark.smoke


def _check_forced_read_via_load_new(page: Page, tr, open_turn) -> None:
    """Cover the forced read: Load new with nothing new, then with a new turn."""
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
    # The fragment that arrived as the provisional tail has settled into the
    # same turn, once: dropping the tail took its part with it.
    expect(open_turn.locator(".tr-reply")).to_have_count(2)


def _check_pull_up_forces_a_read(page: Page, tr) -> None:
    """Cover a pull up past the bottom reading new turns, and a too-short drag doing nothing."""
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


def _check_question_cards_arrive_and_answer(page: Page, tr):
    """Cover question cards arriving live, a one-tap answer and the later result; return the live card."""
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
    return live


def _check_plan_cards_arrive(page: Page, tr, live):
    """Cover plan cards arriving live in each outcome; return the picker stub and the waiting plan card."""
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
    return picker, wait


def _check_terminal_picker_panel(page: Page, tr, picker, wait):
    """Cover the terminal plan picker panel offering, sending and closing its options; return the panel."""
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
    return panel


def _check_plan_panel_sends_feedback(page: Page, tr, picker, panel) -> None:
    """Cover a picker without a transcript call carrying its own plan and sending it back with feedback."""
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
    # #1475: the new fragment joins the open turn under its one header, the
    # way a run card takes new tool calls — not a turn of its own.
    expect(page.locator("#transcriptList .tr-turn")).to_have_count(2)
    open_turn = page.locator("#transcriptList .tr-assistant")
    expect(open_turn).to_have_count(1)
    expect(open_turn.locator(".tr-reply")).to_have_text(
        ["opening conftest now", "found it — a missing await"]
    )
    expect(open_turn.locator(".tr-turn-summary")).to_have_count(1)

    _check_forced_read_via_load_new(page, tr, open_turn)

    _check_pull_up_forces_a_read(page, tr)

    live = _check_question_cards_arrive_and_answer(page, tr)

    picker, wait = _check_plan_cards_arrive(page, tr, live)

    panel = _check_terminal_picker_panel(page, tr, picker, wait)

    _check_plan_panel_sends_feedback(page, tr, picker, panel)


def test_live_polling_follows_the_visible_chat_only(
    authed_page: Page, base_url: str
) -> None:
    """The live poll runs for the open conversation in Chat mode and nowhere
    else (#1515 folded three tests that each paid for their own boot, row open
    and tick; the one boot now walks all three states in turn):

    * The other live session's transcript is never asked for.
    * A closed overlay fetches nothing.
    * A window sitting on Terminal must not also stream chat for that session
      — half of the "10 windows" constraint.
    """
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

    # Closed overlay: the poll stops.
    page.locator("#terminalBack").click()
    expect(page.locator("#terminalOverlay")).to_be_hidden()
    settled = tr.tail_calls()
    _fake_clock.advance(page, 2 * _BACKOFF_MAX_MS)
    assert tr.tail_calls() == settled, "a closed overlay kept polling"

    # Reopened on Chat it ticks again; switched to Terminal it stops again.
    _open_chat(page)
    expect(page.locator("#transcriptList .tr-turn")).to_have_count(2)
    _tick(page, tr)          # at least one tick while Chat shows
    assert tr.tail_calls() > settled, "Chat mode should have ticked at least once"

    page.locator("#sessionModeTerminal").click()
    expect(page.locator("#terminalOverlay")).to_have_attribute("data-mode", "terminal")
    settled = tr.tail_calls()
    _fake_clock.advance(page, 2 * _BACKOFF_MAX_MS)          # long enough for several ticks
    assert tr.tail_calls() == settled, (
        "Terminal mode kept fetching chat: "
        f"{tr.tail_calls() - settled} request(s) after the switch"
    )
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
    # Open a step line (shown by default since #1476), then scroll up off
    # the tail.
    group = page.locator("#transcriptList .tr-step").first
    group.locator("summary.tr-step-line").click()
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


def test_a_reply_arriving_after_tool_calls_is_unfolded_and_a_user_fold_stays(
    authed_page: Page, base_url: str
) -> None:
    """#1531 — a new agent reply must read straight away, never arrive folded.

    A turn that starts with a tool call is keyed on that entry's offset, and
    so are its step line and the call's own item (both folded by default). The
    provisional tail is harvested by key before it is replaced, so the closed
    step line's state used to be restored onto the open turn the moment the
    reply settled it: the reply arrived behind a one-line summary. Only a fold
    the reader made themselves may persist, and only on that turn.
    """
    page = authed_page
    _mock_sessions_list(page)
    tr = _Transcript(page)
    tr.append(_turn("user", "please look at the flaky test", 100))
    _fake_clock.install(page)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_chat(page)
    turns = page.locator("#transcriptList .tr-assistant")
    expect(page.locator("#transcriptList .tr-user")).to_have_count(1)

    # The turn is born in the provisional tail, as a bare step line...
    tr.append(_tool("Bash", "pytest tests/e2e -q", "3 passed", 150))
    _tick(page, tr)
    expect(turns).to_have_count(1, timeout=OVERLAY_OPEN_MS)
    expect(turns.first).to_have_attribute("open", "")
    # ...and the reply that follows settles it.
    tr.append(_turn("assistant", "the reply the reader is waiting for", 200))
    _tick(page, tr)
    expect(page.locator("#transcriptList")).to_contain_text(
        "the reply the reader is waiting for", timeout=OVERLAY_OPEN_MS
    )
    expect(turns).to_have_count(1)
    expect(turns.first).to_have_attribute("open", "")

    # The reader folds this turn; a later turn must not inherit that fold.
    turns.first.locator("summary.tr-turn-summary").click()
    expect(turns.first).not_to_have_attribute("open", "")
    tr.append(
        _turn("user", "next question", 300),
        _tool("Bash", "git status", "clean", 350),
    )
    _tick(page, tr)
    tr.append(_turn("assistant", "the second reply", 400))
    _tick(page, tr)
    expect(page.locator("#transcriptList")).to_contain_text(
        "the second reply", timeout=OVERLAY_OPEN_MS
    )
    expect(turns).to_have_count(2)
    expect(turns.first).not_to_have_attribute("open", "")
    expect(turns.nth(1)).to_have_attribute("open", "")


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
