"""Regression pin for #1603: the Screen view reconnects by itself and reopens
the monitor last viewed.

A dropped socket is retried after 1 s, 2 s, 4 s, 8 s, then 15 s steps, with
"Reconnecting…" over the last frame, until a frame is drawn again (the count
resets) or the steps run out (the Retry card). A refusal (4401, 4403, 4404),
a hidden page and a closed view never retry, and no two sockets are ever open
at once. A drag in flight at the drop sends no release into the next
connection. The monitor picked is remembered per device and reopened after a
reload; a monitor that is gone falls back to the primary.

The view opens through ``/?screen=1`` (the pop-out window's boot path) so the
page under the fake clock is the page that holds the view. Nothing here
touches the real screen: ``/api/screen/monitors`` and ``/api/screen/ws`` are
routed, the frame is a tiny invented JPEG, and the backoff is driven by the
fake clock (#1376), never by waiting.
"""
from __future__ import annotations

import io
import json
import re
from typing import Any, Dict, List, Optional

import pytest
from PIL import Image
from playwright.sync_api import BrowserContext, Page, WebSocketRoute, expect

from tests.e2e import _fake_clock
from tests.e2e.fixtures.waits import wait_until

pytestmark = [pytest.mark.smoke]

_MONITORS = [
    {"id": 0, "left": 0, "top": 0, "width": 1920, "height": 1080, "primary": True},
    {"id": 1, "left": 0, "top": -1080, "width": 1920, "height": 1080, "primary": False},
]
# Every wait the view takes between automatic attempts, in order (ms).
_DELAYS_MS = [1000, 2000, 4000, 8000, 15000, 15000, 15000]
_MONITOR_KEY = "launcher.screenMonitor"


def _frame() -> bytes:
    img = Image.new("RGB", (160, 90), (40, 90, 160))
    img.paste((240, 240, 240), (10, 10, 70, 40))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


class _Stub:
    """The routed monitor list and socket: what the view connected to and what
    it sent."""

    def __init__(
        self,
        context: BrowserContext,
        monitors: Optional[List[Dict[str, Any]]] = None,
        frames: bool = True,
    ) -> None:
        self.urls: List[str] = []
        self.sockets: List[WebSocketRoute] = []
        self.acks = 0
        self.inputs: List[Dict[str, Any]] = []
        self.monitors = monitors or _MONITORS
        self.frames = frames
        context.route(
            re.compile(r".*/api/screen/monitors$"),
            lambda route: route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({"monitors": self.monitors}),
            ),
        )
        context.route_web_socket(re.compile(r".*/api/screen/ws.*"), self._connect)

    def _connect(self, ws: WebSocketRoute) -> None:
        self.urls.append(ws.url)
        self.sockets.append(ws)
        ws.on_message(self._message)
        if self.frames:
            ws.send(_frame())

    def _message(self, msg) -> None:
        if msg == "ack":
            self.acks += 1
        else:
            self.inputs.append(json.loads(msg))

    def drop(self, code: int = 1011, reason: str = "") -> None:
        """The server (or the network) ends the newest socket."""
        self.sockets[-1].close(code=code, reason=reason)


# The most sockets the page ever held open (connecting or open) at once,
# counted as each one is constructed: the page's own view of "never two".
_COUNT_OPEN_SOCKETS = """(() => {
  const Real = window.WebSocket;
  const all = [];
  window.__maxOpenSockets = 0;
  function Counted(...args) {
    const ws = new Real(...args);
    all.push(ws);
    const open = all.filter((s) => s.readyState <= 1).length;
    window.__maxOpenSockets = Math.max(window.__maxOpenSockets, open);
    return ws;
  }
  Object.setPrototypeOf(Counted, Real);
  Counted.prototype = Real.prototype;
  window.WebSocket = Counted;
})();"""


def _max_open_sockets(page: Page) -> int:
    return page.evaluate("() => window.__maxOpenSockets")


def _open_view(page: Page, base_url: str, stub: _Stub, *, drawn: bool = True) -> None:
    """Open the view under the fake clock and, once a frame is up, pause the
    clock: from there time moves only when a test advances it."""
    _fake_clock.install(page)
    page.add_init_script(_COUNT_OPEN_SOCKETS)
    page.goto(f"{base_url}/?screen=1", wait_until="domcontentloaded")
    expect(page.locator("#screenView")).to_be_visible()
    if drawn:
        expect(page.locator("#screenImage")).to_be_visible()
        wait_until(page, lambda: stub.acks >= 1, "the first frame's ack")
    else:
        wait_until(page, lambda: len(stub.urls) >= 1, "the first connection")
    page.clock.pause_at(page.evaluate("Date.now()") + 60_000)


def _connections(stub: _Stub, page: Page) -> int:
    """How many sockets the view has opened, once the page's own calls landed."""
    page.evaluate("() => fetch('/__clock_flush').then(() => undefined)")
    return len(stub.urls)


def _wait_for_socket(stub: _Stub, page: Page, count: int, what: str) -> None:
    wait_until(page, lambda: len(stub.urls) == count, what)


def _attempt(stub: _Stub, page: Page, delay_ms: int) -> None:
    """One more automatic attempt: not a millisecond before ``delay_ms``, and
    exactly then."""
    before = len(stub.urls)
    _fake_clock.advance(page, delay_ms - 1)
    assert _connections(stub, page) == before, f"retried before {delay_ms} ms"
    _fake_clock.advance(page, 1)
    _wait_for_socket(stub, page, before + 1, f"the attempt after {delay_ms} ms")


def test_a_drop_reconnects_by_itself_and_the_picture_resumes(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    stub = _Stub(page.context)
    _open_view(page, base_url, stub)
    state = page.locator("#screenState")
    image = page.locator("#screenImage")
    expect(state).to_be_hidden()
    acks = stub.acks

    stub.drop()
    # The last frame stays, under a quiet line; no Retry card, no new socket yet.
    expect(state).to_have_text("Reconnecting…")
    expect(state.locator(".async-retry")).to_have_count(0)
    expect(image).to_be_visible()
    assert _connections(stub, page) == 1

    _attempt(stub, page, 1000)
    expect(state).to_be_hidden()
    wait_until(page, lambda: stub.acks > acks, "a frame on the new connection")
    expect(image).to_be_visible()

    # A drawn frame reset the count: the next drop waits the first step again,
    # not the second.
    stub.drop()
    expect(state).to_have_text("Reconnecting…")
    _attempt(stub, page, 1000)
    expect(state).to_be_hidden()
    assert _max_open_sockets(page) == 1, "two sockets were open at once"


def test_the_waits_grow_are_capped_and_end_in_the_retry_card(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    # A network that never gets a frame through: each attempt connects and drops.
    stub = _Stub(page.context, frames=False)
    _open_view(page, base_url, stub, drawn=False)
    state = page.locator("#screenState")

    for delay in _DELAYS_MS:
        stub.drop()
        expect(state).to_have_text("Reconnecting…")
        _attempt(stub, page, delay)

    # The last step's attempt fails too: the steps are spent, so the card.
    stub.drop()
    expect(state).to_contain_text("Disconnected from the screen.")
    retry = state.locator(".async-retry")
    expect(retry).to_be_visible()
    count = len(stub.urls)
    _fake_clock.advance(page, 120_000)
    assert _connections(stub, page) == count, "kept retrying past the bound"

    # Retry starts over from the first step.
    stub.frames = True
    retry.click()
    _wait_for_socket(stub, page, count + 1, "the manual retry")
    expect(state).to_be_hidden()
    stub.drop()
    _attempt(stub, page, _DELAYS_MS[0])
    assert _max_open_sockets(page) == 1, "two sockets were open at once"


@pytest.mark.parametrize(
    "code,reason,text",
    [
        (4403, "screen is Tailscale-only", "Tailscale-only"),
        (4401, "missing or invalid bearer token", "Sign in again"),
        (4404, "no such monitor", "display is gone"),
    ],
)
def test_a_refusal_never_retries(
    authed_page: Page, base_url: str, code: int, reason: str, text: str
) -> None:
    page = authed_page
    stub = _Stub(page.context)
    _open_view(page, base_url, stub)

    stub.drop(code, reason)
    state = page.locator("#screenState")
    expect(state).to_contain_text(text)
    expect(state.locator(".async-retry")).to_be_visible()
    _fake_clock.advance(page, 120_000)
    assert _connections(stub, page) == 1, f"retried after a {code}"


def test_a_hidden_page_and_a_closed_view_never_retry(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    stub = _Stub(page.context)
    _open_view(page, base_url, stub)
    set_hidden = """(hidden) => {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
      document.dispatchEvent(new Event('visibilitychange'));
    }"""

    # A retry is pending when the page goes to the background: it never fires,
    # and coming back connects once, at once, with no second socket after it.
    stub.drop()
    expect(page.locator("#screenState")).to_have_text("Reconnecting…")
    page.evaluate(set_hidden, True)
    _fake_clock.advance(page, 120_000)
    assert _connections(stub, page) == 1, "retried while the page was hidden"
    page.evaluate(set_hidden, False)
    _wait_for_socket(stub, page, 2, "the connect on return")
    _fake_clock.advance(page, 120_000)
    assert _connections(stub, page) == 2, "a second connect after the return"

    # A drop that lands while hidden waits for the return, with no timer.
    page.evaluate("""() => Object.defineProperty(document, 'hidden',
      { configurable: true, get: () => true })""")
    stub.drop()
    expect(page.locator("#screenState")).to_have_text("Reconnecting…")
    _fake_clock.advance(page, 120_000)
    assert _connections(stub, page) == 2, "retried while the page was hidden"
    page.evaluate(set_hidden, False)
    _wait_for_socket(stub, page, 3, "the connect on return")

    # A drop, then the view is closed: nothing reconnects for nobody.
    stub.drop()
    page.locator("#screenViewClose").click()
    expect(page.locator("#screenView")).to_be_hidden()
    _fake_clock.advance(page, 120_000)
    assert _connections(stub, page) == 3, "retried after the view was closed"
    assert _max_open_sockets(page) == 1, "two sockets were open at once"


@pytest.mark.iphone
def test_a_drag_in_flight_at_the_drop_sends_no_release_after_it(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    stub = _Stub(page.context)
    _open_view(page, base_url, stub)
    page.locator('#screenMode .segmented-item[data-mode="control"]').click()

    def where(fx: float, fy: float) -> Dict[str, float]:
        return page.evaluate("""([fx, fy]) => {
          const s = document.getElementById('screenStage');
          const img = document.getElementById('screenImage');
          const r = s.getBoundingClientRect();
          const fit = Math.min(s.clientWidth / img.naturalWidth, s.clientHeight / img.naturalHeight);
          const w = img.naturalWidth * fit, h = img.naturalHeight * fit;
          return { x: r.left + (s.clientWidth - w) / 2 + fx * w,
                   y: r.top + (s.clientHeight - h) / 2 + fy * h };
        }""", [fx, fy])

    def pointer(kind: str, pt: Dict[str, float]) -> None:
        page.evaluate("""([type, x, y]) => {
          document.getElementById('screenStage').dispatchEvent(new PointerEvent(type, {
            pointerId: 1, clientX: x, clientY: y, bubbles: true, pointerType: 'touch',
            isPrimary: true, button: 0 }));
        }""", [kind, pt["x"], pt["y"]])

    a, b = where(0.2, 0.2), where(0.7, 0.5)
    pointer("pointerdown", a)
    pointer("pointermove", {"x": (a["x"] + b["x"]) / 2, "y": (a["y"] + b["y"]) / 2})
    wait_until(page, lambda: any(m["type"] == "down" for m in stub.inputs), "the drag's down")

    # The link drops with the finger still down; the next connection draws.
    acks = stub.acks
    stub.drop()
    expect(page.locator("#screenState")).to_have_text("Reconnecting…")
    _attempt(stub, page, 1000)
    wait_until(page, lambda: stub.acks > acks, "a frame on the new connection")
    sent = len(stub.inputs)

    # The finger lifts into the live new connection: nothing goes out for it.
    pointer("pointermove", b)
    pointer("pointerup", b)
    # A tap right after proves input is live again, and orders the check: the
    # socket delivers in order, so anything the lift sent would be ahead of it.
    c = where(0.5, 0.5)
    pointer("pointerdown", c)
    pointer("pointerup", c)
    wait_until(page, lambda: len(stub.inputs) > sent, "the next tap's click")
    assert [m["type"] for m in stub.inputs[sent:]] == ["click"], stub.inputs[sent:]


def test_the_view_reopens_on_the_last_monitor_viewed(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    stub = _Stub(page.context)
    page.goto(f"{base_url}/?screen=1", wait_until="domcontentloaded")
    expect(page.locator("#screenImage")).to_be_visible()
    assert "monitor=0" in stub.urls[0], "nothing picked yet: the primary"
    assert page.evaluate(f"localStorage.getItem('{_MONITOR_KEY}')") is None

    picker = page.locator("#screenPicker .segmented-item")
    picker.nth(1).click()
    wait_until(page, lambda: len(stub.urls) == 2, "the pick's connection")
    assert "monitor=1" in stub.urls[1]
    stored = json.loads(page.evaluate(f"localStorage.getItem('{_MONITOR_KEY}')"))
    assert stored["id"] == 1 and stored["top"] == -1080, stored

    # A reload (and a new pop-out window, which is the same boot) comes back on it.
    page.goto(f"{base_url}/?screen=1", wait_until="domcontentloaded")
    wait_until(page, lambda: len(stub.urls) == 3, "the reload's connection")
    assert "monitor=1" in stub.urls[2], stub.urls[2]
    expect(page.locator("#screenPicker .segmented-item").nth(1)).to_have_attribute(
        "aria-pressed", "true"
    )

    # The layout changed: ids swapped, but the rectangle still finds the monitor.
    stub.monitors = [
        {"id": 0, "left": 0, "top": -1080, "width": 1920, "height": 1080, "primary": False},
        {"id": 1, "left": 0, "top": 0, "width": 1920, "height": 1080, "primary": True},
    ]
    page.goto(f"{base_url}/?screen=1", wait_until="domcontentloaded")
    wait_until(page, lambda: len(stub.urls) == 4, "the swapped layout's connection")
    assert "monitor=0" in stub.urls[3], stub.urls[3]

    # The monitor is gone: the primary, no error card.
    stub.monitors = [_MONITORS[0]]
    page.goto(f"{base_url}/?screen=1", wait_until="domcontentloaded")
    wait_until(page, lambda: len(stub.urls) == 5, "the single-monitor connection")
    assert "monitor=0" in stub.urls[4], stub.urls[4]
    expect(page.locator("#screenPicker")).to_be_hidden()
    expect(page.locator("#screenImage")).to_be_visible()
    expect(page.locator("#screenState")).to_be_hidden()


def test_a_display_that_is_gone_is_forgotten(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    stub = _Stub(page.context)
    page.goto(f"{base_url}/?screen=1", wait_until="domcontentloaded")
    expect(page.locator("#screenImage")).to_be_visible()
    page.locator("#screenPicker .segmented-item").nth(1).click()
    wait_until(page, lambda: len(stub.urls) == 2, "the pick's connection")
    assert page.evaluate(f"localStorage.getItem('{_MONITOR_KEY}')") is not None

    stub.drop(4404, "no such monitor")
    expect(page.locator("#screenState")).to_contain_text("display is gone")
    wait_until(
        page,
        lambda: page.evaluate(f"localStorage.getItem('{_MONITOR_KEY}')") is None,
        "the remembered monitor to be cleared",
    )


def test_two_connects_in_one_tick_leave_one_socket(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    stub = _Stub(page.context)
    _open_view(page, base_url, stub)

    # Two picks before the first connect has finished awaiting its token: the
    # older one is stale and must open nothing.
    page.evaluate("""() => {
      const [main, above] = document.querySelectorAll('#screenPicker .segmented-item');
      above.click();
      main.click();
    }""")
    wait_until(page, lambda: len(stub.urls) >= 2, "the second pick's connection")
    _fake_clock.advance(page, 1000)
    assert _connections(stub, page) == 2, stub.urls
    assert "monitor=0" in stub.urls[1], stub.urls[1]
    assert _max_open_sockets(page) == 1, "two sockets were open at once"
