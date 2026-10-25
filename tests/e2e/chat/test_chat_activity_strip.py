"""Chat's activity strip and an unconfirmed send: the turn in progress, then sent, confirmed or returned."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.fixtures.waits import OVERLAY_OPEN_MS, stable_eval

from tests.e2e.chat._support import (
    _CONFIRM_WINDOW_MS,
    _POLL_MS,
    _STRIP_LINE_JS,
    _boot,
    _line_shown_with_status,
    _open_chat,
    _send_unconfirmed,
    _since,
    _tick,
    _turn,
)


pytestmark = pytest.mark.smoke


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
    # The stub session has no PTY behind it, so on WebKit the page's own
    # reconnect loop rewrites #terminalStatus about once a second and leaves
    # it clear for a few ms (#1700): a clear followed by an auto-retrying
    # expect() loses to the loop's next "Reconnecting…" about one run in eight.
    # Set the status and read the line's box in one evaluate, so nothing runs
    # between the write and the read.
    assert _line_shown_with_status(page, "Reconnecting…") is False
    assert _line_shown_with_status(page, None) is True

    # A harness with no tool data gives the time alone — never "0 actions".
    tr.activity = {"since": tr.activity["since"], "actions": None, "last": "Thinking", "working": True}
    _tick(page, tr)
    expect(line).to_have_text(re.compile(r"\d+:\d\d · Thinking$"))

    # The turn ends: the line goes, and its timer with it.
    # `hidden` is the line's own word, apart from the status rule above, which
    # may be hiding it again by now.
    tr.activity = {**tr.activity, "working": False}
    _tick(page, tr)
    expect(line).to_have_attribute("hidden", "")


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


@pytest.mark.iphone
def test_an_unconfirmed_send_says_sent_and_stays_quiet_when_the_turn_shows_up(
    authed_page: Page, base_url: str
) -> None:
    """The common case: the message reaches the agent, its turn lands in the
    transcript within the window, and no warning is ever raised."""
    page = authed_page
    tr = _boot(page, base_url)
    _send_unconfirmed(page, "yes please")

    tr.append(_turn("user", "yes please", 300))
    _tick(page, tr)
    expect(page.locator("#transcriptList .tr-user").last).to_contain_text("yes please")

    # Well past the window, with the fake clock and a tick after it (the
    # watch is judged on a read): still no warning, and the box stays empty.
    _fake_clock.advance(page, _CONFIRM_WINDOW_MS + _POLL_MS)
    _tick(page, tr)
    expect(page.locator("#toast")).not_to_contain_text("picked it up")
    expect(page.locator("#chatComposeBar .composer-input")).to_have_value("")


@pytest.mark.iphone
def test_an_unconfirmed_send_is_confirmed_by_the_agent_going_busy(
    authed_page: Page, base_url: str
) -> None:
    """No turn yet (the agent logs it late), but the session went busy: that
    is evidence too, and the window closing later raises nothing."""
    page = authed_page
    tr = _boot(page, base_url)
    _send_unconfirmed(page, "go ahead")

    tr.activity = {"since": _since(page, 5), "actions": None, "last": "Thinking", "working": True}
    _tick(page, tr)

    _fake_clock.advance(page, _CONFIRM_WINDOW_MS + _POLL_MS)
    _tick(page, tr)
    expect(page.locator("#toast")).not_to_contain_text("picked it up")
    expect(page.locator("#chatComposeBar .composer-input")).to_have_value("")


@pytest.mark.iphone
def test_an_unconfirmed_send_the_agent_never_takes_is_flagged_and_the_text_returned(
    authed_page: Page, base_url: str
) -> None:
    """The case the old warning was for: nothing shows up. The window closes
    on a distinct error state, and the text goes back to the box to resend."""
    page = authed_page
    tr = _boot(page, base_url)
    _send_unconfirmed(page, "did you get this")

    # Still inside the window: quiet, however many ticks have found nothing.
    _tick(page, tr)
    _tick(page, tr)
    expect(page.locator("#toast")).not_to_contain_text("picked it up")

    _fake_clock.advance(page, _CONFIRM_WINDOW_MS)
    toast = page.locator("#toast")
    expect(toast).to_contain_text("hasn’t picked it up", timeout=OVERLAY_OPEN_MS)
    expect(toast).to_have_class(re.compile(r"\berror\b"))
    expect(page.locator("#chatComposeBar .composer-input")).to_have_value("did you get this")
