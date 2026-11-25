"""A dictation take leaves the browser as 16 kHz mono WAV, so the PC needs no ffmpeg.

Chromium's fake microphone (``--use-fake-device-for-media-stream``, set in
``tests/e2e/fixtures/pages.py``) feeds the real MediaRecorder and Web Audio path
of ``voice.js``. The take is recorded as PCM from the microphone stream, so no
recorder container is ever converted: a streamed take sends open-ended WAV
chunks that joined are one WAV, a single-shot take uploads ``recording.wav``.
With Web Audio removed the page falls back to the recorder's own container,
which the server converts when it can.
"""

from __future__ import annotations

import struct
from typing import List

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.smoke, pytest.mark.unbundled]

# Chromium's fake microphone beeps once a second; this much recording holds one.
_TAKE_MS = 1600
_TRANSCRIPT = "fake microphone transcript"
_EBML = b"\x1a\x45\xdf\xa3"  # the start of a WebM container

# Mounts one dictation on a throwaway button and textarea, importing voice.js
# from the URL the app itself loaded so the module and its state are shared.
# The button is tapped with a real click, which is the user gesture Web Audio needs.
_MOUNT = """async () => {
  const hit = performance.getEntriesByType('resource').find(e => /\\/voice\\.js/.test(e.name));
  const mod = await import(hit ? hit.name : '/static/voice.js');
  const button = document.createElement('button');
  button.id = 'e2eDictationMic';
  button.textContent = 'mic';
  const field = document.createElement('textarea');
  field.id = 'e2eDictationText';
  // Above the page, so nothing of the app covers the tap.
  button.style.cssText = 'position:fixed;left:8px;bottom:8px;z-index:99999';
  field.style.cssText = 'position:fixed;left:80px;bottom:8px;z-index:99999';
  document.body.append(button, field);
  const dictation = mod.createDictation({ button, getTextarea: () => field });
  button.addEventListener('click', () => dictation.toggle());
  window.__dictation = dictation;
}"""

_NO_WEB_AUDIO = """
Object.defineProperty(window, 'AudioContext', { value: undefined, configurable: true });
Object.defineProperty(window, 'webkitAudioContext', { value: undefined, configurable: true });
"""


def _wav_header(wav: bytes) -> dict:
    """The fields of a 44-byte WAV header."""
    assert wav[:4] == b"RIFF" and wav[8:16] == b"WAVEfmt ", "not a WAV"
    _, channels, rate, _, _, bits = struct.unpack("<HHIIHH", wav[20:36])
    riff_size, data_size = struct.unpack("<I", wav[4:8])[0], struct.unpack("<I", wav[40:44])[0]
    return {
        "channels": channels, "rate": rate, "bits": bits, "riff_size": riff_size,
        "data_size": data_size, "data_tag": wav[36:40],
    }


def _seconds_and_peak(wav: bytes) -> tuple[float, int]:
    """Length in seconds and loudest 16-bit sample of a WAV with a 44-byte header."""
    count = (len(wav) - 44) // 2
    samples = struct.unpack(f"<{count}h", wav[44:44 + count * 2])
    return count / 16000, max((abs(s) for s in samples), default=0)


def _record(page: Page) -> None:
    """Tap the mic, record the fake microphone for a moment, leave it recording."""
    page.click("#e2eDictationMic")
    page.wait_for_function("() => window.__dictation.isRecording()")
    page.wait_for_timeout(_TAKE_MS)


def _open(page: Page, base_url: str) -> None:
    page.goto(base_url, wait_until="domcontentloaded")
    page.evaluate(_MOUNT)


def _single_shot(page: Page) -> None:
    """No streamed session can be opened, so the take is buffered and posted once."""
    page.route("**/api/transcribe/sessions", lambda route: route.fulfill(status=503, body="no session"))
    page.route(
        "**/api/transcribe",
        lambda route: route.fulfill(status=200, content_type="application/json", body='{"transcript": "%s"}' % _TRANSCRIPT),
    )


def _streamed(page: Page, chunks: List[bytes], types: List[str]) -> None:
    """A streamed session that keeps every chunk it is sent."""
    page.route(
        "**/api/transcribe/sessions",
        lambda route: route.fulfill(status=200, content_type="application/json", body='{"session_id": "vt-1"}'),
    )
    page.route(
        "**/api/transcribe/sessions/vt-1/events*",
        lambda route: route.fulfill(status=200, content_type="text/event-stream", body="event: ping\ndata: {}\n\n"),
    )

    def _chunk(route) -> None:
        chunks.append(route.request.post_data_buffer or b"")
        types.append(route.request.headers.get("content-type", ""))
        route.fulfill(status=200, content_type="application/json", body='{"raw_bytes": 1}')

    page.route("**/api/transcribe/sessions/vt-1/chunk", _chunk)
    page.route(
        "**/api/transcribe/sessions/vt-1/finish",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"transcript": "%s"}' % _TRANSCRIPT
        ),
    )


def _stop_and_upload(page: Page) -> bytes:
    """Stop the take and return the body of the single-shot upload."""
    with page.expect_request("**/api/transcribe") as upload:
        page.click("#e2eDictationMic")
    return upload.value.post_data_buffer or b""


def _stop_and_settle(page: Page) -> None:
    """Stop a streamed take and wait for its transcript to land."""
    page.click("#e2eDictationMic")
    expect(page.locator("#e2eDictationText")).to_have_value(_TRANSCRIPT)


def test_dictation_leaves_the_browser_as_pcm_wav_and_falls_back_to_the_recorder(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    console: List[str] = []
    page.on("console", lambda msg: console.append(msg.text))

    # A streamed take: open-ended WAV chunks that joined are one WAV.
    chunks: List[bytes] = []
    types: List[str] = []
    _streamed(page, chunks, types)
    _open(page, base_url)
    _record(page)
    _stop_and_settle(page)
    assert chunks and all(t == "audio/wav" for t in types), f"chunk types: {types}"
    whole = b"".join(chunks)
    head = _wav_header(whole)
    assert (head["rate"], head["channels"], head["bits"]) == (16000, 1, 16)
    # The end of a streamed take is unknown at its start, so the lengths are open-ended.
    assert head["riff_size"] == head["data_size"] == 0xFFFFFFFF
    seconds, peak = _seconds_and_peak(whole)
    assert 1.0 < seconds < 4.0 and peak > 500, f"the streamed PCM is not the take: {seconds:.2f}s, peak {peak}"
    assert (len(whole) - 44) % 2 == 0, "a chunk split a sample"

    # A single-shot take (no streamed session): one recording.wav, closed at the end of the audio.
    _single_shot(page)
    _open(page, base_url)
    console.clear()
    _record(page)
    sent = _stop_and_upload(page)
    assert b'filename="recording.wav"' in sent, "the take was not uploaded as WAV"
    start = sent.index(b"RIFF")
    head = _wav_header(sent[start:])
    assert (head["rate"], head["channels"], head["bits"]) == (16000, 1, 16)
    # The header's length is the audio the form carries: it closes where the WAV does.
    wav = sent[start:start + 44 + head["data_size"]]
    boundary = bytes([13, 10]) + b"--"  # the multipart boundary that follows the file part
    assert head["riff_size"] == len(wav) - 8 and sent[start + len(wav):].startswith(boundary)
    seconds, peak = _seconds_and_peak(wav)
    assert 1.0 < seconds < 4.0 and peak > 500, f"the PCM WAV is not the take: {seconds:.2f}s, peak {peak}"
    assert any("as WAV (pcm)" in line for line in console), "the PCM path was not logged"
    expect(page.locator("#e2eDictationText")).to_have_value(_TRANSCRIPT)

    # Without Web Audio the single-shot take is the recorder's own container...
    page.add_init_script(_NO_WEB_AUDIO)
    _open(page, base_url)
    _record(page)
    sent = _stop_and_upload(page)
    assert b'filename="recording.webm"' in sent, "the raw recording was not uploaded"
    assert _EBML in sent and b"RIFF" not in sent
    expect(page.locator("#e2eDictationText")).to_have_value(_TRANSCRIPT)

    # ...and so is a streamed one.
    chunks.clear()
    types.clear()
    _streamed(page, chunks, types)
    _open(page, base_url)
    _record(page)
    _stop_and_settle(page)
    assert chunks and chunks[0].startswith(_EBML), "the recorder's own container was not streamed"
    assert not any(t == "audio/wav" for t in types)
