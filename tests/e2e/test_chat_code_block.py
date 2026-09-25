"""Regression pin for #1100 — a fenced code block inside a Chat-mode turn.

Roberto reported from the phone a monospace heading line missing its first
character. ``markdown.js`` emits ``<pre class="md-code"><code>`` for a fence,
and inside a chat bubble the only rule that reached it was
``.tr-md pre { overflow-x: auto; }``. Everything else was UA default: no
surface, no padding, no radius, the UA's ``1em`` block margin fighting
``.tr-md p``'s ``0 0 8px``, and ``white-space: pre`` — ``.tr-text``'s
``overflow-wrap: anywhere`` does not reach a ``pre``. So a long line became a
bare unwrapped strip that scrolled sideways; a diagonal swipe panned it and it
stayed panned, with nothing on screen saying the block was scrolled.

Two legs, both run red against pre-fix CSS first:

* **Geometry** — at 390px and 320px the block's ``scrollWidth`` must equal its
  ``clientWidth``: it wraps, so there is no lateral offset a swipe can hide
  characters in. Measured pre-fix at 390px: 941px of content in a 321px box
  (Chromium) and 1027px in 336px (WebKit) -- 620px / 691px reachable only by
  panning. The page itself must not scroll sideways either.
* **Surface** — the block carries ``.tr-pre``'s treatment (``--card-off``,
  ``--radius-sm``, ``8px 10px``) and ``.tr-md p``'s vertical rhythm, so a
  reader can see where the block starts and ends. Pre-fix ``white-space`` read
  ``pre``, the background ``rgba(0, 0, 0, 0)`` and the padding ``0px``. Inline
  code gets the same chip (the other half of the same UA-default gap), and the
  fence's own ``<code>`` child is pinned *not* to paint it a second time —
  otherwise a chip would be drawn around the whole listing.

The fix does not add a third copy of these declarations: ``.tr-md pre`` joins
``.tr-pre``'s existing rule, which had become the identical ten declarations,
with ``.tr-pre``'s own ``max-height``/``overflow-y`` split out. So this module
also guards ``.tr-pre`` — a tool result's surface — against drift.

The Life OS file viewer renders the same ``pre.md-code`` and is not asserted
here, deliberately: the shared rule is scoped to ``.tr-md`` / ``.tr-pre``, and
``session-transcript.js:396`` is the only writer of ``.tr-md``, so the file
viewer's own rule is out of reach. Its own module covers that consumer, and
the fenced block there keeps its existing scroll route.

Its selector is spelled out in ``styles.css`` rather than here on purpose:
``tests/test_classify_e2e.py``'s surface-coverage invariant matches surface
markers as *text* over every ``tests/e2e/test_*.py``, so naming that class in
this docstring would file this module under a surface it does not exercise
and route a file-viewer-only diff into it.

Style assertions use auto-retrying ``expect().to_have_css`` and the raw
geometry read goes through ``stable_read`` (#680) — the chat transcript
re-renders on its own poll.
"""
from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import OVERLAY_OPEN_MS, stable_read
from tests.e2e.test_session_mode_toggle import (
    _mock_git_status,
    _mock_sessions_list,
    _row,
    _session_row,
)

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_SID = "sid-code-block-1100"

# Long enough that it cannot fit any phone width, and a single unbroken run so
# the wrap has to break inside a "word" — the case `overflow-wrap: anywhere`
# exists for, and the shape of the reported heading line.
_LONG_LINE = "### session_host.stale_relevant==false&sha=" + "0123456789abcdef" * 4
_REPLY = (
    "Here is the check I ran:\n\n"
    "```\n"
    + _LONG_LINE + "\n"
    "short line\n"
    "```\n\n"
    "…and it came back clean, per `GET /api/version`."
)

# scrollWidth/clientWidth of the fenced block, plus whether the document
# itself gained a horizontal scroll.
_MEASURE = """
(() => {
  const pre = document.querySelector('#transcriptList .tr-md pre');
  // A null here (missing, or zero-width mid-render) is the artifact
  // `stable_read` retries past -- 0 === 0 would otherwise pass falsely.
  if (!pre || !pre.clientWidth) return null;
  const de = document.documentElement;
  return {
    scrollWidth: pre.scrollWidth,
    clientWidth: pre.clientWidth,
    right: Math.round(pre.getBoundingClientRect().right),
    docScroll: de.scrollWidth - de.clientWidth,
  };
})()
"""


def _mock_transcript(page: Page) -> None:
    body = {
        "available": True, "source": "native", "reason": None, "session_id": _SID,
        "next_cursor": None,
        "entries": [
            {"kind": "user", "timestamp": "2026-09-20T09:00:00Z",
             "text": "is the session host live?", "truncated": False, "sidechain": False},
            {"kind": "assistant", "timestamp": "2026-09-20T09:00:05Z",
             "text": _REPLY, "truncated": False, "sidechain": False},
        ],
    }
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/transcript(\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)),
    )


def test_chat_fenced_block_is_a_bounded_surface_that_wraps(
    authed_page: Page, base_url: str
) -> None:
    _mock_git_status(authed_page)
    _mock_sessions_list(authed_page, [
        _session_row(_SID, kind="remote", agent="claude", title="Code block demo"),
    ])
    _mock_transcript(authed_page)
    authed_page.goto(base_url, wait_until="domcontentloaded")
    _row(authed_page, _SID).locator(".session-open").click()
    expect(authed_page.locator("#terminalOverlay")).to_be_visible(timeout=OVERLAY_OPEN_MS)

    pre = authed_page.locator("#transcriptList .tr-md pre")
    expect(pre).to_be_visible()
    # The fence rendered as a block, not as raw ``` text in a paragraph.
    expect(pre.locator("code")).to_contain_text("session_host.stale_relevant")

    # Surface: the same treatment `.tr-pre` already had, tokens only.
    expect(pre).to_have_css("white-space", "pre-wrap")
    expect(pre).to_have_css("overflow-wrap", "anywhere")
    for side in ("top", "bottom"):
        expect(pre).to_have_css(f"padding-{side}", "8px")
    for side in ("left", "right"):
        expect(pre).to_have_css(f"padding-{side}", "10px")
    expect(pre).to_have_css("border-radius", "8px")
    # --card-off, not transparent: the block has to read as its own surface.
    # Asserted as "not transparent" rather than one rgb() so the pin holds in
    # both themes (the token differs; having a surface at all does not).
    expect(pre).not_to_have_css("background-color", "rgba(0, 0, 0, 0)")
    # `.tr-md p`'s rhythm, never the UA's 1em.
    expect(pre).to_have_css("margin-top", "0px")
    expect(pre).to_have_css("margin-bottom", "8px")

    # Inline code is the same surface as a chip -- the other half of the same
    # UA-default gap, and named in the issue's approach.
    inline = authed_page.locator("#transcriptList .tr-md p code")
    expect(inline).to_have_text("GET /api/version")
    expect(inline).not_to_have_css("background-color", "rgba(0, 0, 0, 0)")
    expect(inline).to_have_css("border-radius", "8px")
    # ...and the fence's own `<code>` child must not paint it a second time
    # inside the block, which would draw a chip around the whole listing.
    fence_code = pre.locator("code")
    expect(fence_code).to_have_css("background-color", "rgba(0, 0, 0, 0)")
    expect(fence_code).to_have_css("padding-left", "0px")

    # Geometry: nothing reachable only by panning, at the two phone widths.
    for width in (390, 320):
        authed_page.set_viewport_size({"width": width, "height": 844})
        authed_page.wait_for_timeout(150)
        m = stable_read(lambda: authed_page.evaluate(_MEASURE))
        assert m is not None, f"no fenced block found at {width}px"
        assert m["scrollWidth"] == m["clientWidth"], (
            f"at {width}px the code block scrolls sideways "
            f"({m['scrollWidth']}px of content in {m['clientWidth']}px) — #1100's "
            "silently-panned strip is back"
        )
        assert m["right"] <= width, (
            f"at {width}px the code block's right edge is at {m['right']}px, "
            "outside the viewport"
        )
        assert m["docScroll"] <= 0, (
            f"at {width}px the code block gave the page {m['docScroll']}px of "
            "horizontal scroll"
        )
