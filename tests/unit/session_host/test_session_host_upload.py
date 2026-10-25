"""Session-host file upload — any type accepted, size-capped (issue #366).

The compose-bar attach broadened the upload from images-only to arbitrary
files: the file is only ever *stored* under ``<project>/.launcher-tmp`` and
its path pasted into the user's own prompt, so the old image-extension
allowlist bought nothing. These pin the new contract: any sane extension
saves (including none), an odd-looking extension is stripped rather than
written, and the 12 MB / non-empty guards stay.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.session_host import server as sh_server


@pytest.fixture()
def client(tmp_path, monkeypatch):
    session = SimpleNamespace(project_dir=str(tmp_path), write=lambda data: None)
    monkeypatch.setattr(sh_server.manager, "get", lambda sid: session)
    return TestClient(sh_server.app)


def test_plain_text_file_uploads(client, tmp_path):
    resp = client.post(
        "/sessions/x/image?inline=1",
        files={"file": ("notes.txt", b"hello attach", "text/plain")},
    )
    assert resp.status_code == 200, resp.text
    path = resp.json()["path"]
    assert path.endswith(".txt")
    assert ".launcher-tmp" in path


def test_image_still_uploads(client):
    resp = client.post(
        "/sessions/x/image?inline=1",
        files={"file": ("shot.png", b"\x89PNG fake", "image/png")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["path"].endswith(".png")


def test_no_extension_uploads_without_suffix(client):
    resp = client.post(
        "/sessions/x/image?inline=1",
        files={"file": ("Makefile", b"all:\n", "application/octet-stream")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["path"].endswith("-Makefile")


def test_odd_suffix_is_stripped_not_trusted(client):
    resp = client.post(
        "/sessions/x/image?inline=1",
        files={"file": ("weird.t x!t", b"data", "application/octet-stream")},
    )
    assert resp.status_code == 200, resp.text
    path = resp.json()["path"]
    assert not path.endswith(".t x!t")


def test_empty_upload_rejected(client):
    resp = client.post(
        "/sessions/x/image?inline=1",
        files={"file": ("empty.txt", b"", "text/plain")},
    )
    assert resp.status_code == 400


def test_oversize_upload_rejected(client, monkeypatch):
    monkeypatch.setattr(sh_server, "_MAX_IMAGE_BYTES", 10)
    resp = client.post(
        "/sessions/x/image?inline=1",
        files={"file": ("big.bin", b"x" * 11, "application/octet-stream")},
    )
    assert resp.status_code == 400
