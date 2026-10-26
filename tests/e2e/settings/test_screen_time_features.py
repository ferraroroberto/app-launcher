"""Screen time › By feature: the input features used, split by device.

Two halves, one contract:

  * the sheet draws "By feature" under "By section" for each period, as counts
    split by device with the chart's fills (PC solid, phone striped), busiest
    first, in both themes and without spilling sideways; a failed read hides
    that block alone and leaves the minutes standing;
  * the composer records a feature once, when it succeeded, through the real
    usage log client: the click-to-event path a stub DOM cannot prove. Only the
    constant id is posted, never a file name, a message or a transcript.

Hermetic: ``/api/usage/time``, ``/api/usage/features``, the upload endpoints,
the transcriber and the usage log's POST are answered by routes set before
``goto()``; no real log, host or voice-transcriber is touched.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e.compose.test_compose_bar import (
    ATTACH_INPUT, COMPOSER, INPUT, SEND, _PNG_1x1, _open_terminal, _show_composer,
)
from tests.e2e.compose.test_voice_dictation import _MEDIA_MOCK
from tests.e2e.fixtures.ui import HeldUploads, open_settings_sheet
from tests.e2e.fixtures.waits import stable_eval, wait_until
from tests.e2e.settings.test_screen_time import _no_sideways_spill, _route_export, _route_usage

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_FEATURES_RE = re.compile(r".*/api/usage/features\?.*")
_EVENTS_RE = re.compile(r".*/api/usage/events$")
_UPLOAD_RE = re.compile(r".*/api/claude-code/sessions/[^/]+/image(?:\?.*)?$")
_LARGE_RE = re.compile(r".*/api/claude-code/sessions/[^/]+/large-file(?:\?.*)?$")
_IDS = [
    "feat.send.typed", "feat.send.dictated", "feat.dictation", "feat.attach.image",
    "feat.attach.file", "feat.attach.large", "feat.ocr", "feat.paste-pc",
]
# Uses per period: id -> {device: n}. Today is a little, a month a lot.
_USES = {
    1: {"feat.send.typed": {"desktop": 2, "phone": 1}, "feat.attach.image": {"phone": 1}},
    7: {"feat.send.typed": {"desktop": 9, "phone": 4}, "feat.dictation": {"phone": 6},
        "feat.attach.image": {"phone": 3, "tablet": 1}, "feat.ocr": {"desktop": 1}},
    30: {"feat.send.typed": {"desktop": 30, "phone": 12}, "feat.dictation": {"phone": 20},
         "feat.send.dictated": {"phone": 14}, "feat.attach.file": {"desktop": 5},
         "feat.attach.large": {"desktop": 2}, "feat.paste-pc": {"phone": 8},
         "feat.attach.image": {"phone": 9}, "feat.ocr": {"desktop": 3}},
}
_LABELS = {
    "feat.send.typed": "Sent typed", "feat.send.dictated": "Sent dictated",
    "feat.dictation": "Dictations", "feat.attach.image": "Images", "feat.attach.file": "Files",
    "feat.attach.large": "Large files", "feat.ocr": "Text from images",
    "feat.paste-pc": "Paste to PC",
}


def _route_features(page: Page, *, status: int = 200) -> list[int]:
    seen: list[int] = []

    def handle(route: Route) -> None:
        days = int(re.search(r"days=(\d+)", route.request.url).group(1))
        seen.append(days)
        if status != 200:
            route.fulfill(status=status, content_type="application/json",
                          body=json.dumps({"detail": "usage log unavailable"}))
            return
        uses = _USES[days]
        features = [{"id": fid, "total": sum(uses.get(fid, {}).values()),
                     "by_device": uses.get(fid, {})} for fid in _IDS]
        route.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"days": days, "log_exists": True, "features": features}))

    page.route(_FEATURES_RE, handle)
    return seen


def _expected(days: int) -> list[tuple[str, str]]:
    """(label, total) busiest first, ties in the sheet's own list order."""
    rows = [(_LABELS[fid], sum(_USES[days].get(fid, {}).values())) for fid in _IDS]
    return [(label, str(total)) for label, total in sorted(rows, key=lambda r: -r[1])]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_by_feature_draws_each_period_split_by_device(
    authed_page: Page, base_url: str, theme: str
) -> None:
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844})
    _route_usage(page)
    _route_export(page, {"configured": True, "last_ok": None, "last_error": None})
    seen = _route_features(page)
    page.add_init_script(f"localStorage.setItem('launcher.theme', '{theme}')")
    page.goto(base_url, wait_until="domcontentloaded")
    page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
    open_settings_sheet(page, "screenTimeSheet")
    sheet = page.locator("#screenTimeSheet")

    # It sits under "By section", with its own heading.
    headings = sheet.locator(".screen-time-report .opt-group-title")
    expect(headings).to_have_text(["By section", "By feature"])
    labels = page.locator("#screenTimeFeatures .screen-time-row-label")
    values = page.locator("#screenTimeFeatures .screen-time-row-value")

    for days, button in ((1, "Today"), (7, "7 days"), (30, "30 days")):
        sheet.locator(f'#screenTimeRange [data-days="{days}"]').click()
        expect(sheet.locator('#screenTimeRange [aria-pressed="true"]')).to_have_text(button)
        want = _expected(days)
        expect(labels).to_have_text([label for label, _ in want])
        expect(values).to_have_text([total for _, total in want])
        _no_sideways_spill(page)
    assert {1, 7, 30} <= set(seen), seen

    # A month's busiest row: 42 typed sends, PC 30 and phone 12, each device
    # on its own fill, the phone's striped; the row says the split too.
    top = page.locator("#screenTimeFeatures .screen-time-row").first
    expect(top).to_have_attribute("title", "PC 30 · Phone 12")
    segs = stable_eval(top, """row => Array.from(row.querySelectorAll('.screen-time-share-fill'))
      .map((s) => ({ device: s.dataset.device, w: s.getBoundingClientRect().width,
                     image: getComputedStyle(s).backgroundImage,
                     color: getComputedStyle(s).backgroundColor }))""")
    assert [s["device"] for s in segs] == ["desktop", "phone"], segs
    assert abs(segs[0]["w"] / segs[1]["w"] - 30 / 12) < 0.15, segs
    assert segs[0]["image"] == "none" and "gradient" in segs[1]["image"], segs
    assert segs[0]["color"] != "rgba(0, 0, 0, 0)", segs

    for width, height in ((320, 700), (1280, 900)):
        page.set_viewport_size({"width": width, "height": height})
        _no_sideways_spill(page)
        expect(labels).to_have_text([label for label, _ in _expected(30)])


def test_a_failed_feature_read_hides_only_that_block(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _route_usage(page)
    _route_features(page, status=503)
    page.goto(base_url, wait_until="domcontentloaded")
    open_settings_sheet(page, "screenTimeSheet")
    expect(page.locator("#screenTimeTotal")).to_have_text("1 h 25 m")
    expect(page.locator("#screenTimeSections .screen-time-row")).not_to_have_count(0)
    expect(page.locator("#screenTimeFeaturesBlock")).to_be_hidden()
    expect(page.locator("#screenTimeState")).to_be_hidden()


# ---- the composer records a use, once, when it succeeded -------------------

def _capture_events(page: Page) -> list[dict]:
    posted: list[dict] = []

    def handle(route: Route) -> None:
        posted.extend(json.loads(route.request.post_data or "{}").get("events", []))
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"accepted": 0, "rejected": 0}))

    page.route(_EVENTS_RE, handle)
    return posted


def _feature_taps(page: Page, posted: list[dict]) -> list[str]:
    # The client batches for 10 s; a pagehide sends what is queued now.
    page.evaluate("() => window.dispatchEvent(new Event('pagehide'))")
    page.wait_for_timeout(100)
    return [e["id"] for e in posted if e.get("kind") == "tap" and e["id"].startswith("feat.")]


def _wait_for_taps(page: Page, posted: list[dict], want: list[str]) -> None:
    wait_until(page, lambda: _feature_taps(page, posted) == want,
               f"feature taps {want}", timeout_ms=10_000)


def test_the_composer_records_each_success_once_and_only_its_id(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    page = authed_page
    page.add_init_script(_MEDIA_MOCK)
    posted = _capture_events(page)
    held = HeldUploads(page, _UPLOAD_RE)
    page.route(_LARGE_RE, lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"path": "E:/tmp/.launcher-tmp/e2e-stub-big.bin", "bytes": 10})))
    page.route("**/api/transcribe/sessions", lambda route: route.fulfill(status=503, body="nope"))
    page.route("**/api/transcribe", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"transcript": "e2e-stub dictated words", "language": "en"})))
    _open_terminal(page, base_url, launched_pty_session)
    _show_composer(page)
    page.evaluate(f"document.querySelector('{COMPOSER} .composer-mic').disabled = false")

    # A picked file that is refused before upload, or still in flight, is no use.
    page.locator(ATTACH_INPUT).set_input_files(files=[
        {"name": "e2e-stub-secret-name.png", "mimeType": "image/png", "buffer": _PNG_1x1},
        {"name": "e2e-stub-secret-notes.txt", "mimeType": "text/plain", "buffer": b"x"},
    ])
    held.wait_for(1)
    assert _feature_taps(page, posted) == []
    held.ok(0, "E:/tmp/.launcher-tmp/e2e-stub-a.png")
    held.wait_for(2)
    held.ok(1, "E:/tmp/.launcher-tmp/e2e-stub-b.txt")
    _wait_for_taps(page, posted, ["feat.attach.image", "feat.attach.file"])

    page.locator(f"{COMPOSER} .composer-large-input").set_input_files(
        files=[{"name": "e2e-stub-big.bin", "mimeType": "application/octet-stream", "buffer": b"0123456789"}])
    _wait_for_taps(page, posted, ["feat.attach.image", "feat.attach.file", "feat.attach.large"])

    # A typed message sent: one send, not a dictated one.
    page.locator(INPUT).fill("e2e-stub typed words")
    page.locator(SEND).click()
    expect(page.locator(INPUT)).to_have_value("")
    _wait_for_taps(page, posted, ["feat.attach.image", "feat.attach.file", "feat.attach.large",
                                  "feat.send.typed"])

    # A dictation that produced text, then the send that follows it.
    mic = page.locator(f"{COMPOSER} .composer-mic")
    mic.click()
    expect(mic).to_have_class(re.compile(r"\brecording\b"))
    mic.click()
    expect(page.locator(INPUT)).to_have_value("e2e-stub dictated words", timeout=10_000)
    page.locator(SEND).click()
    expect(page.locator(INPUT)).to_have_value("")
    _wait_for_taps(page, posted, ["feat.attach.image", "feat.attach.file", "feat.attach.large",
                                  "feat.send.typed", "feat.dictation", "feat.send.dictated"])

    # Only constant ids went out: nothing the owner typed, said or picked.
    everything = json.dumps(posted)
    for secret in ("secret-name", "secret-notes", "e2e-stub typed", "dictated words", "big.bin"):
        assert secret not in everything, f"{secret} reached the usage log"
