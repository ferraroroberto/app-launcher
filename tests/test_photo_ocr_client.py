"""The photo-ocr loopback client (`src.photo_ocr_client`, issue #171).

Thin ``requests`` wrapper over photo-ocr's single-shot ``POST /api/extract``
— so we stub ``requests.post`` and assert the one-call shape plus the error
mapping. Mirrors ``test_voice_client.py``.
"""

from __future__ import annotations

import pytest

from src import photo_ocr_client


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def test_extract_posts_images_and_returns_text(monkeypatch):
    captured = {}

    def fake_request(method, url, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        captured["files"] = kwargs.get("files")
        captured["verify"] = kwargs.get("verify")
        return _Resp(200, {"text": "buy milk", "model": "gemini_flash"})

    monkeypatch.setattr(photo_ocr_client._loopback_http.SESSION, "request", fake_request)

    result = photo_ocr_client.extract(
        "https://127.0.0.1:8444/",
        [("shot.png", b"img", "image/png")],
        "gemini_flash",
    )

    assert result["text"] == "buy milk"
    # No double slash from the trailing-slash base.
    assert captured["url"] == "https://127.0.0.1:8444/api/extract"
    assert captured["params"] == {"model": "gemini_flash"}
    # Self-signed loopback cert → verify must be disabled.
    assert captured["verify"] is False
    # One repeated 'files' multipart part per image.
    assert [f[0] for f in captured["files"]] == ["files"]
    assert captured["files"][0][1][0] == "shot.png"


def test_extract_sends_multiple_images_as_repeated_files(monkeypatch):
    """The collation case: several shots of one doc → repeated 'files' parts."""
    captured = {}

    def fake_request(method, url, **kwargs):
        captured["files"] = kwargs.get("files")
        return _Resp(200, {"text": "merged", "model": "gemini_flash"})

    monkeypatch.setattr(photo_ocr_client._loopback_http.SESSION, "request", fake_request)
    photo_ocr_client.extract(
        "https://127.0.0.1:8444",
        [
            ("a.png", b"1", "image/png"),
            ("b.png", b"2", "image/png"),
            ("c.png", b"3", "image/png"),
        ],
    )
    # Three parts, all under the 'files' field name (photo-ocr's contract).
    assert [f[0] for f in captured["files"]] == ["files", "files", "files"]
    assert [f[1][0] for f in captured["files"]] == ["a.png", "b.png", "c.png"]


def test_extract_no_images_raises_400(monkeypatch):
    with pytest.raises(photo_ocr_client.PhotoOcrError) as exc:
        photo_ocr_client.extract("https://127.0.0.1:8444", [])
    assert exc.value.status == 400


def test_extract_no_model_omits_params(monkeypatch):
    def fake_request(method, url, **kwargs):
        assert kwargs["params"] is None
        return _Resp(200, {"text": "x"})

    monkeypatch.setattr(photo_ocr_client._loopback_http.SESSION, "request", fake_request)
    photo_ocr_client.extract(
        "https://127.0.0.1:8444", [("s.png", b"a", "image/png")]
    )


def test_extract_forwards_prompt_id(monkeypatch):
    def fake_request(method, url, **kwargs):
        assert kwargs["params"] == {"prompt_id": "code-fenced"}
        return _Resp(200, {"text": "x"})

    monkeypatch.setattr(photo_ocr_client._loopback_http.SESSION, "request", fake_request)
    photo_ocr_client.extract(
        "https://127.0.0.1:8444",
        [("s.png", b"a", "image/png")],
        prompt_id="code-fenced",
    )


def test_extract_upstream_error_raises(monkeypatch):
    def fake_request(method, url, **kwargs):
        return _Resp(413, {"detail": "too many photos"})

    monkeypatch.setattr(photo_ocr_client._loopback_http.SESSION, "request", fake_request)
    with pytest.raises(photo_ocr_client.PhotoOcrError) as exc:
        photo_ocr_client.extract(
            "https://127.0.0.1:8444", [("s.png", b"a", "image/png")]
        )
    assert exc.value.status == 413
    assert "too many photos" in str(exc.value)


def test_extract_connection_failure_is_503(monkeypatch):
    def fake_request(method, url, **kwargs):
        raise photo_ocr_client.requests.RequestException("connection refused")

    monkeypatch.setattr(photo_ocr_client._loopback_http.SESSION, "request", fake_request)
    with pytest.raises(photo_ocr_client.PhotoOcrError) as exc:
        photo_ocr_client.extract(
            "https://127.0.0.1:8444", [("s.png", b"a", "image/png")]
        )
    assert exc.value.status == 503


# --- #1420: wait out photo-ocr's run budget, keep the three states apart -----

# photo-ocr's default ``extract_run_budget_s`` (photo-ocr#166): the longest a
# single ``/api/extract`` call can legitimately run before it answers.
_PHOTO_OCR_RUN_BUDGET_S = 900.0


def test_timeout_outlasts_photo_ocr_run_budget(monkeypatch):
    """A slow hub on a multi-screenshot attach must not make us give up while
    photo-ocr is still inside its own budget — the call carries a timeout
    longer than that budget, not the old 120 s."""
    captured = {}

    def fake_request(method, url, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return _Resp(200, {"text": "x"})

    monkeypatch.setattr(photo_ocr_client._loopback_http.SESSION, "request", fake_request)
    photo_ocr_client.extract("https://127.0.0.1:8444", [("s.png", b"a", "image/png")])

    assert photo_ocr_client._TIMEOUT > _PHOTO_OCR_RUN_BUDGET_S
    assert captured["timeout"] == photo_ocr_client._TIMEOUT


def test_extract_still_running_past_budget_is_504_not_unreachable(monkeypatch):
    """Connected but never answered within the budget is its own state: 504
    with a message that says photo-ocr was *still working*, not the 503
    "unreachable" a refused connection gets."""

    def fake_request(method, url, **kwargs):
        raise photo_ocr_client.requests.exceptions.ReadTimeout("read timed out")

    monkeypatch.setattr(photo_ocr_client._loopback_http.SESSION, "request", fake_request)
    with pytest.raises(photo_ocr_client.PhotoOcrError) as exc:
        photo_ocr_client.extract(
            "https://127.0.0.1:8444", [("s.png", b"a", "image/png")]
        )
    assert exc.value.status == 504
    assert "still" in str(exc.value)
    assert "unreachable" not in str(exc.value)


def test_extract_connect_timeout_stays_unreachable(monkeypatch):
    """A connect timeout never reached photo-ocr at all — that is the
    unreachable state (503), not "still running"."""

    def fake_request(method, url, **kwargs):
        raise photo_ocr_client.requests.exceptions.ConnectTimeout("connect timed out")

    monkeypatch.setattr(photo_ocr_client._loopback_http.SESSION, "request", fake_request)
    with pytest.raises(photo_ocr_client.PhotoOcrError) as exc:
        photo_ocr_client.extract(
            "https://127.0.0.1:8444", [("s.png", b"a", "image/png")]
        )
    assert exc.value.status == 503
    assert "unreachable" in str(exc.value)


def test_extract_partial_result_is_a_success_carrying_missing_photos(
    monkeypatch, caplog
):
    """photo-ocr#166: a partly read take is a 200 whose ``text`` holds a
    ``[missing: photo N …]`` line per unread photo and ``missing_photos``
    names them. It is returned whole (never raised) and logged."""
    payload = {
        "text": "page one\n[missing: photo 2 (b.jpg) could not be read]\npage three",
        "model": "claude_opus",
        "missing_photos": ["b.jpg"],
    }

    def fake_request(method, url, **kwargs):
        return _Resp(200, payload)

    monkeypatch.setattr(photo_ocr_client._loopback_http.SESSION, "request", fake_request)
    with caplog.at_level("WARNING", logger=photo_ocr_client.logger.name):
        result = photo_ocr_client.extract(
            "https://127.0.0.1:8444",
            [("a.jpg", b"1", "image/jpeg"), ("b.jpg", b"2", "image/jpeg")],
        )

    assert result["missing_photos"] == ["b.jpg"]
    assert "[missing: photo 2" in result["text"]
    assert any("b.jpg" in r.getMessage() for r in caplog.records)


def test_extract_complete_result_reports_no_missing_photos(monkeypatch):
    """The field is always a list for the phone, even from a photo-ocr that
    predates it."""

    def fake_request(method, url, **kwargs):
        return _Resp(200, {"text": "all read"})

    monkeypatch.setattr(photo_ocr_client._loopback_http.SESSION, "request", fake_request)
    result = photo_ocr_client.extract(
        "https://127.0.0.1:8444", [("s.png", b"a", "image/png")]
    )
    assert result["missing_photos"] == []


def test_extract_nothing_readable_is_a_502_from_upstream(monkeypatch):
    """photo-ocr's own 502 (no photo could be read) passes through with its
    detail — the hub-down and too-slow wording stays the upstream's."""

    def fake_request(method, url, **kwargs):
        return _Resp(502, {"detail": "LLM hub still working on photos after the 420s request timeout"})

    monkeypatch.setattr(photo_ocr_client._loopback_http.SESSION, "request", fake_request)
    with pytest.raises(photo_ocr_client.PhotoOcrError) as exc:
        photo_ocr_client.extract(
            "https://127.0.0.1:8444", [("s.png", b"a", "image/png")]
        )
    assert exc.value.status == 502
    assert "still working" in str(exc.value)
