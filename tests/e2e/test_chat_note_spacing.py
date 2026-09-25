"""Regression pin for #1098 — the detached-session footnote's own air.

Roberto reported from the phone that the "Detached session — no terminal"
caption in Chat mode was drawn *over* the last line of the message above it.

``.chat-note`` had ``padding: 0 12px 8px``, so the only air above it came
from ``.transcript-body``'s own 12px inner padding — and that padding is
*inside* the scroll box. It shows only when the transcript is scrolled to
its end. At every other scroll position a line of the transcript is clipped
flush with the scroller's bottom edge, and the note's text started 0.0px
(Chromium) / 0.7px (WebKit) below it: text against text.

This pins the measurement that was red before the fix and green after:
mid-scroll, the note's own text must start a clear gap below the last pixel
of transcript text. The raw geometry read goes through ``stable_read``
(#680). The padding assertion uses auto-retrying ``expect`` on the longhands
so a stale handle cannot fake a pass.
"""
from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import OVERLAY_OPEN_MS, stable_read
from tests.e2e.test_session_mode_toggle import _mock_git_status, _mock_sessions_list, _row, _session_row

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_SID = "sid-note-spacing-1098"

_FILLER = (
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod "
    "tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim "
    "veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip."
)

# The last pixel of transcript text actually painted inside the scroller's
# clip box, and where the note's own text starts. Mid-scroll the clipped
# line reaches the scroller's bottom edge, so the two are adjacent and the
# note's top padding is the only thing separating them.
_MEASURE_GAP = """
(() => {
  const note = document.getElementById('chatNote');
  const body = document.getElementById('transcriptBody');
  const b = body.getBoundingClientRect();
  let lowest = null;
  for (const el of body.querySelectorAll('li, p, div, span')) {
    const rng = document.createRange();
    rng.selectNodeContents(el);
    for (const rect of rng.getClientRects()) {
      if (rect.height > 0 && rect.top >= b.top - 0.5 && rect.top < b.bottom - 0.5) {
        const painted = Math.min(rect.bottom, b.bottom);
        if (lowest === null || painted > lowest) lowest = painted;
      }
    }
  }
  if (lowest === null) return null;
  const rng = document.createRange();
  rng.selectNodeContents(note);
  return rng.getBoundingClientRect().top - lowest;
})()
"""


def _mock_long_transcript(page: Page) -> None:
    entries = []
    for i in range(14):
        entries.append({"kind": "user", "timestamp": "2026-09-16T09:00:00Z",
                        "text": f"prompt {i}", "truncated": False, "sidechain": False})
        entries.append({"kind": "assistant", "timestamp": "2026-09-16T09:00:05Z",
                        "text": f"reply {i}: {_FILLER}", "truncated": False, "sidechain": False})
    body = {"available": True, "source": "native", "reason": None, "session_id": _SID,
            "next_cursor": None, "entries": entries}
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/transcript(\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)),
    )


def test_chat_note_keeps_its_own_air_above_the_transcript(
    authed_page: Page, base_url: str
) -> None:
    _mock_git_status(authed_page)
    _mock_sessions_list(authed_page, [
        _session_row(_SID, kind="remote", agent="claude", title="Detached demo"),
    ])
    _mock_long_transcript(authed_page)
    authed_page.goto(base_url, wait_until="domcontentloaded")
    _row(authed_page, _SID).locator(".session-open").click()
    expect(authed_page.locator("#terminalOverlay")).to_be_visible(timeout=OVERLAY_OPEN_MS)
    note = authed_page.locator("#chatNote")
    expect(note).to_be_visible()

    # Symmetric, and on the one 12px gutter token (--gap).
    expect(note).to_have_css("padding-top", "12px")
    expect(note).to_have_css("padding-bottom", "12px")
    # Never the compressible item in the .chat-pane column, as .composer is not.
    expect(note).to_have_css("flex-shrink", "0")

    # The reported collision, at the narrowest phone widths the app supports.
    for width in (390, 320):
        authed_page.set_viewport_size({"width": width, "height": 844})
        authed_page.wait_for_timeout(150)
        authed_page.evaluate(
            "() => { const b = document.getElementById('transcriptBody');"
            " b.scrollTop = Math.round((b.scrollHeight - b.clientHeight) * 0.5); }"
        )
        authed_page.wait_for_timeout(150)
        gap = stable_read(lambda: authed_page.evaluate(_MEASURE_GAP))
        assert gap is not None, f"no transcript text measured inside the scroller at {width}px"
        # Pre-fix this read 0.0 (Chromium) / 0.7 (WebKit); post-fix 12.0 / 12.7.
        assert gap >= 8, (
            f"at {width}px the footnote's text starts only {gap:.1f}px below the last "
            "line of transcript text — #1098's collision is back"
        )
