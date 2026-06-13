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

    monkeypatch.setattr(photo_ocr_client.requests, "request", fake_request)

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

    monkeypatch.setattr(photo_ocr_client.requests, "request", fake_request)
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

    monkeypatch.setattr(photo_ocr_client.requests, "request", fake_request)
    photo_ocr_client.extract(
        "https://127.0.0.1:8444", [("s.png", b"a", "image/png")]
    )


def test_extract_forwards_prompt_id(monkeypatch):
    def fake_request(method, url, **kwargs):
        assert kwargs["params"] == {"prompt_id": "code-fenced"}
        return _Resp(200, {"text": "x"})

    monkeypatch.setattr(photo_ocr_client.requests, "request", fake_request)
    photo_ocr_client.extract(
        "https://127.0.0.1:8444",
        [("s.png", b"a", "image/png")],
        prompt_id="code-fenced",
    )


def test_extract_upstream_error_raises(monkeypatch):
    def fake_request(method, url, **kwargs):
        return _Resp(413, {"detail": "too many photos"})

    monkeypatch.setattr(photo_ocr_client.requests, "request", fake_request)
    with pytest.raises(photo_ocr_client.PhotoOcrError) as exc:
        photo_ocr_client.extract(
            "https://127.0.0.1:8444", [("s.png", b"a", "image/png")]
        )
    assert exc.value.status == 413
    assert "too many photos" in str(exc.value)


def test_extract_connection_failure_is_503(monkeypatch):
    def fake_request(method, url, **kwargs):
        raise photo_ocr_client.requests.RequestException("connection refused")

    monkeypatch.setattr(photo_ocr_client.requests, "request", fake_request)
    with pytest.raises(photo_ocr_client.PhotoOcrError) as exc:
        photo_ocr_client.extract(
            "https://127.0.0.1:8444", [("s.png", b"a", "image/png")]
        )
    assert exc.value.status == 503
