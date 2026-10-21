"""Files an agent sends to the chat view (#1530): the outbox's guards, its
store, its routes, and Claude ``SendUserFile`` parity.

Every path here is invented under ``tmp_path``. The guard tests are written
to fail on the naive implementation: a string-prefix root check admits a
sibling folder that shares the prefix, and a check on the unresolved path
admits ``..`` and a junction or symlink that points outside.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import zipfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src import board, session_outbox
from src.session_outbox import OutboxRefusal
from src.webapp_config import WebappConfig

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PDF = b"%PDF-1.7\n%fake\n"


@pytest.fixture(autouse=True)
def _outbox_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(session_outbox, "OUTBOX_DIR", tmp_path / "outbox-store")


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    return root


def _refusal(path: Any, base: Any, roots: List[Path]) -> str:
    with pytest.raises(OutboxRefusal) as info:
        session_outbox.read_source(str(path), str(base) if base else None, roots)
    return info.value.reason


def _roots(proj: Path, *extra: str) -> List[Path]:
    return session_outbox.allowed_roots(str(proj), list(extra))


# ------------------------------------------------------------------ guards


def test_an_image_under_the_session_folder_is_read_and_typed_by_its_bytes(proj: Path):
    (proj / "shot.png").write_bytes(PNG)
    resolved, data, kind, media = session_outbox.read_source("shot.png", str(proj), _roots(proj))
    assert resolved == (proj / "shot.png").resolve()
    assert (data, kind, media) == (PNG, "image", "image/png")


def test_dotdot_out_of_the_folder_is_out_of_root(tmp_path: Path, proj: Path):
    (tmp_path / "secret.txt").write_text("not yours", encoding="utf-8")
    (proj / "sub").mkdir()
    assert _refusal(proj / "sub" / ".." / ".." / "secret.txt", proj, _roots(proj)) == "out_of_root"
    assert _refusal("../secret.txt", proj, _roots(proj)) == "out_of_root"


def test_a_sibling_folder_sharing_the_prefix_is_out_of_root(tmp_path: Path, proj: Path):
    # A string-prefix check reads ".../proj-evil/x.png" as inside ".../proj".
    evil = tmp_path / "proj-evil"
    evil.mkdir()
    (evil / "x.png").write_bytes(PNG)
    assert _refusal(evil / "x.png", proj, _roots(proj)) == "out_of_root"


def test_a_junction_inside_the_folder_pointing_out_is_out_of_root(tmp_path: Path, proj: Path):
    _winapi = pytest.importorskip("_winapi")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keys.txt").write_text("private", encoding="utf-8")
    link = proj / "looks-local"
    try:
        _winapi.CreateJunction(str(outside), str(link))
    except OSError as exc:  # pragma: no cover - a filesystem without junctions
        pytest.skip(f"cannot create a junction here: {exc}")
    # The unresolved path sits under proj; only resolving it shows the escape.
    assert _refusal(link / "keys.txt", proj, _roots(proj)) == "out_of_root"


def test_a_symlink_inside_the_folder_pointing_out_is_out_of_root(tmp_path: Path, proj: Path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    link = proj / "inside.png"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")
    assert _refusal(link, proj, _roots(proj)) == "out_of_root"


def test_a_declared_scratch_root_admits_its_files_with_env_vars_expanded(
    tmp_path: Path, proj: Path, monkeypatch
):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "report.md").write_text("# Report\n", encoding="utf-8")
    assert _refusal(scratch / "report.md", proj, _roots(proj)) == "out_of_root"
    monkeypatch.setenv("OUTBOX_TEST_SCRATCH", str(tmp_path))
    roots = _roots(proj, "%OUTBOX_TEST_SCRATCH%/scratch")
    _, _, kind, media = session_outbox.read_source(str(scratch / "report.md"), str(proj), roots)
    assert (kind, media) == ("markdown", session_outbox.TEXT_MEDIA)


@pytest.fixture
def temp_base(tmp_path: Path, monkeypatch) -> Path:
    """An invented %TEMP% holding Claude's scratch base, ``claude``."""
    temp = tmp_path / "fake-temp"
    (temp / "claude").mkdir(parents=True)
    monkeypatch.setenv("TEMP", str(temp))
    monkeypatch.setenv("TMP", str(temp))
    return temp


def _default_roots(proj: Path) -> List[Path]:
    # The roots a fresh install resolves: no scratch setting authored.
    return session_outbox.allowed_roots(str(proj), WebappConfig().outbox_scratch_roots)


def test_claudes_scratch_base_is_a_scratch_root_by_default(temp_base: Path, proj: Path):
    # Any session's scratch folder, not only the asking session's own.
    other = temp_base / "claude" / "E--some-project" / "session-b" / "scratchpad"
    other.mkdir(parents=True)
    (other / "chart.png").write_bytes(PNG)
    _, _, kind, media = session_outbox.read_source(str(other / "chart.png"), str(proj), _default_roots(proj))
    assert (kind, media) == ("image", "image/png")


def test_a_configured_scratch_list_replaces_the_default(tmp_path: Path, temp_base: Path, proj: Path):
    (temp_base / "claude" / "note.md").write_text("# n\n", encoding="utf-8")
    mine = tmp_path / "mine"
    mine.mkdir()
    roots = session_outbox.allowed_roots(str(proj), [str(mine)])
    assert _refusal(temp_base / "claude" / "note.md", proj, roots) == "out_of_root"


def test_a_folder_sharing_the_scratch_base_prefix_is_out_of_root(temp_base: Path, proj: Path):
    # A string-prefix check reads ".../claude-other/x.png" as inside ".../claude".
    sibling = temp_base / "claude-other"
    sibling.mkdir()
    (sibling / "x.png").write_bytes(PNG)
    assert _refusal(sibling / "x.png", proj, _default_roots(proj)) == "out_of_root"
    # The temp folder itself is not the scratch base either.
    (temp_base / "loose.png").write_bytes(PNG)
    assert _refusal(temp_base / "loose.png", proj, _default_roots(proj)) == "out_of_root"


def test_a_junction_from_scratch_to_outside_is_out_of_root(tmp_path: Path, temp_base: Path, proj: Path):
    _winapi = pytest.importorskip("_winapi")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keys.txt").write_text("private", encoding="utf-8")
    link = temp_base / "claude" / "looks-scratch"
    try:
        _winapi.CreateJunction(str(outside), str(link))
    except OSError as exc:  # pragma: no cover - a filesystem without junctions
        pytest.skip(f"cannot create a junction here: {exc}")
    assert _refusal(link / "keys.txt", proj, _default_roots(proj)) == "out_of_root"


def test_a_symlink_from_scratch_to_outside_is_out_of_root(tmp_path: Path, temp_base: Path, proj: Path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    link = temp_base / "claude" / "inside.png"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")
    assert _refusal(link, proj, _default_roots(proj)) == "out_of_root"


@pytest.mark.parametrize("name", ["page.html", "logo.svg", "tool.exe", "data.xlsx", "noext", "run.bat", "app.jar", "app.apk",
                                  "doc.docx", "bare.tar", "pack.rar"])
def test_types_off_the_allowlist_are_refused(proj: Path, name: str):
    (proj / name).write_bytes(b"<svg onload=alert(1)>")
    assert _refusal(proj / name, proj, _roots(proj)) == "type_not_allowed"


@pytest.mark.parametrize("name,data", [
    ("fake.png", b"just text"),
    ("fake.pdf", PNG),
    ("binary.txt", b"abc\x00def"),
    ("latin1.md", "café".encode("latin-1")),
])
def test_bytes_that_are_not_what_the_extension_claims_are_refused(proj: Path, name: str, data: bytes):
    (proj / name).write_bytes(data)
    assert _refusal(proj / name, proj, _roots(proj)) == "content_mismatch"


def _zip_bytes() -> bytes:
    # A fixed member time: these bytes sit in parametrize ids, and xdist aborts
    # when two workers collect different ids, as they do across a clock second.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(zipfile.ZipInfo("hello.txt", date_time=(2026, 1, 1, 0, 0, 0)), "hello")
    return buf.getvalue()


SEVEN_Z = b"7z\xbc\xaf\x27\x1c\x00\x04" + b"\x00" * 24


@pytest.mark.parametrize("name,data", [
    ("pack.zip", _zip_bytes()),
    ("log.gz", gzip.compress(b"hello")),
    ("src.tgz", gzip.compress(b"tar-ish")),
    ("pack.7z", SEVEN_Z),
    ("LOUD.ZIP", _zip_bytes()),
])
def test_an_archive_is_read_as_opaque_download_only_bytes(proj: Path, name: str, data: bytes):
    (proj / name).write_bytes(data)
    _resolved, got, kind, media = session_outbox.read_source(name, str(proj), _roots(proj))
    assert (got, kind, media) == (data, "archive", "application/octet-stream")
    assert session_outbox.kind_of(name) == "archive"


@pytest.mark.parametrize("name,data", [
    ("fake.zip", b"just text, not a zip"),
    ("empty.zip", b"PK\x05\x06" + b"\x00" * 18),  # a zero-entry zip is not what anyone sends
    ("fake.gz", _zip_bytes()),
    ("fake.7z", gzip.compress(b"x")),
    ("page.zip", b"<html><script>alert(1)</script></html>"),
])
def test_an_archive_whose_bytes_disagree_with_its_extension_is_a_content_mismatch(
    proj: Path, name: str, data: bytes
):
    (proj / name).write_bytes(data)
    assert _refusal(proj / name, proj, _roots(proj)) == "content_mismatch"


def test_an_archive_outside_every_root_is_out_of_root_not_type_not_allowed(tmp_path: Path, proj: Path):
    (tmp_path / "out.zip").write_bytes(_zip_bytes())
    assert _refusal(tmp_path / "out.zip", proj, _roots(proj)) == "out_of_root"


def test_an_oversize_archive_is_too_large(proj: Path, monkeypatch):
    monkeypatch.setattr(session_outbox, "MAX_FILE_BYTES", 16)
    (proj / "big.zip").write_bytes(b"PK\x03\x04" + b"\x00" * 13)
    (proj / "fits.zip").write_bytes(b"PK\x03\x04" + b"\x00" * 12)
    assert _refusal(proj / "big.zip", proj, _roots(proj)) == "too_large"
    assert session_outbox.read_source(str(proj / "fits.zip"), str(proj), _roots(proj))[3] == "application/octet-stream"


def test_an_oversize_file_is_refused(proj: Path, monkeypatch):
    monkeypatch.setattr(session_outbox, "MAX_FILE_BYTES", 16)
    (proj / "big.txt").write_text("x" * 17, encoding="utf-8")
    (proj / "fits.txt").write_text("x" * 16, encoding="utf-8")
    assert _refusal(proj / "big.txt", proj, _roots(proj)) == "too_large"
    assert session_outbox.read_source(str(proj / "fits.txt"), str(proj), _roots(proj))[1] == b"x" * 16


def test_the_three_main_refusals_name_three_different_reasons(tmp_path: Path, proj: Path, monkeypatch):
    monkeypatch.setattr(session_outbox, "MAX_FILE_BYTES", 16)
    (tmp_path / "out.png").write_bytes(PNG)
    (proj / "page.html").write_text("<b>x</b>", encoding="utf-8")
    (proj / "big.txt").write_text("x" * 32, encoding="utf-8")
    reasons = {
        _refusal(tmp_path / "out.png", proj, _roots(proj)),
        _refusal(proj / "page.html", proj, _roots(proj)),
        _refusal(proj / "big.txt", proj, _roots(proj)),
    }
    assert reasons == {"out_of_root", "type_not_allowed", "too_large"}


def test_missing_paths_and_folders_are_refused(proj: Path):
    (proj / "dir.png").mkdir()
    assert _refusal(proj / "nope.png", proj, _roots(proj)) == "not_found"
    assert _refusal(proj / "dir.png", proj, _roots(proj)) == "not_a_file"
    assert _refusal("", proj, _roots(proj)) == "bad_request"
    assert _refusal("rel.png", None, _roots(proj)) == "bad_request"


@pytest.mark.parametrize("path", [
    r"\\203.0.113.9\share\x.png",          # TEST-NET-3: never a real host
    "//203.0.113.9/share/x.png",
    r"\\?\C:\invented\x.png",
    r"\\.\pipe\invented",
])
def test_network_and_device_paths_are_refused_before_any_filesystem_call(
    proj: Path, monkeypatch, path: str
):
    # Resolving a UNC path makes Windows connect to the host, handing it the
    # user's NTLM hash, so the refusal must come before resolve() is called.
    def no_resolve(self, *a, **k):
        raise AssertionError(f"resolve() reached for {self}")

    monkeypatch.setattr(Path, "resolve", no_resolve)
    assert _refusal(path, proj, [proj]) == "out_of_root"


def test_no_usable_root_refuses_everything(proj: Path):
    (proj / "a.png").write_bytes(PNG)
    assert _refusal(proj / "a.png", proj, []) == "out_of_root"


# ------------------------------------------------------------------- store


def test_post_copies_the_file_so_a_later_change_to_the_source_is_not_served(proj: Path):
    src = proj / "chart.png"
    src.write_bytes(PNG)
    entry = session_outbox.post("s1", str(src), "  the chart  ", str(proj), _roots(proj), now=1_800_000_000)
    src.write_bytes(b"changed afterwards")
    stored, listed = session_outbox.entry_file("s1", entry["id"])
    assert stored.read_bytes() == PNG
    assert listed == entry
    assert entry["caption"] == "the chart" and entry["name"] == "chart.png"
    assert entry["kind"] == "image" and entry["media"] == "image/png" and entry["size"] == len(PNG)
    assert entry["timestamp"].endswith("Z")
    found = session_outbox.listing("s1")
    assert found["rev"] == 1 and [e["id"] for e in found["entries"]] == [entry["id"]]


def test_a_refused_post_writes_nothing(tmp_path: Path, proj: Path):
    (tmp_path / "out.png").write_bytes(PNG)
    with pytest.raises(OutboxRefusal):
        session_outbox.post("s1", str(tmp_path / "out.png"), "", str(proj), _roots(proj))
    assert session_outbox.listing("s1") == {"rev": 0, "entries": []}
    assert not (session_outbox.OUTBOX_DIR / "s1").exists()


def test_a_session_keeps_only_its_newest_entries(proj: Path, monkeypatch):
    monkeypatch.setattr(session_outbox, "MAX_ENTRIES_PER_SESSION", 2)
    (proj / "a.txt").write_text("a", encoding="utf-8")
    ids = [session_outbox.post("s1", str(proj / "a.txt"), "", str(proj), _roots(proj))["id"] for _ in range(4)]
    found = session_outbox.listing("s1")
    assert [e["id"] for e in found["entries"]] == ids[-2:]
    assert found["rev"] == 4
    assert sorted(p.name for p in (session_outbox.OUTBOX_DIR / "s1").iterdir() if p.name != "index.json") == sorted(ids[-2:])


def test_expired_session_folders_are_pruned_on_the_next_post(proj: Path):
    (proj / "a.txt").write_text("a", encoding="utf-8")
    session_outbox.post("old", str(proj / "a.txt"), "", str(proj), _roots(proj))
    old = session_outbox.OUTBOX_DIR / "old"
    past = old.stat().st_mtime - session_outbox.OUTBOX_TTL - 60
    os.utime(old, (past, past))
    session_outbox.post("new", str(proj / "a.txt"), "", str(proj), _roots(proj))
    assert not old.exists() and (session_outbox.OUTBOX_DIR / "new").is_dir()


@pytest.mark.parametrize("sid", ["../escape", "a/b", "", "x" * 65])
def test_a_session_id_that_is_not_one_never_becomes_a_folder(proj: Path, sid: str):
    (proj / "a.txt").write_text("a", encoding="utf-8")
    with pytest.raises(OutboxRefusal) as info:
        session_outbox.post(sid, str(proj / "a.txt"), "", str(proj), _roots(proj))
    assert info.value.reason == "bad_request"
    assert session_outbox.listing(sid) == {"rev": 0, "entries": []}


def test_entry_file_refuses_an_id_that_is_not_listed(proj: Path):
    (proj / "a.txt").write_text("a", encoding="utf-8")
    session_outbox.post("s1", str(proj / "a.txt"), "", str(proj), _roots(proj))
    assert session_outbox.entry_file("s1", "index.json") is None
    assert session_outbox.entry_file("s1", "../s1/index") is None
    assert session_outbox.entry_file("s1", "0" * 32) is None


# --------------------------------------------------- Claude SendUserFile


def _jsonl(path: Path, rows: List[Dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def _send_call(files: Any, caption: str = "", tid: str = "t1", cwd: str = "", ts: str = "2026-10-12T10:00:01Z") -> Dict[str, Any]:
    inputs: Dict[str, Any] = {"files": files, "status": "normal"}
    if caption:
        inputs["caption"] = caption
    row: Dict[str, Any] = {
        "type": "assistant", "timestamp": ts,
        "message": {"id": "m-" + tid, "role": "assistant",
                    "content": [{"type": "tool_use", "id": tid, "name": "SendUserFile", "input": inputs}]},
    }
    if cwd:
        row["cwd"] = cwd
    return row


def _prompt(text: str = "show me the chart") -> Dict[str, Any]:
    return {"type": "user", "timestamp": "2026-10-12T10:00:00Z",
            "message": {"role": "user", "content": text}}


def test_send_user_file_becomes_refs_without_the_path(tmp_path: Path):
    from src import session_transcript as st

    path = _jsonl(tmp_path / "t.jsonl", [
        _prompt(),
        _send_call(["C:/invented/place/chart.png", "notes.md", "page.html"], caption="before vs after"),
    ])
    entries = st.transcript_page(path, flavor="claude")["entries"]
    call = next(e for e in entries if e["kind"] == "tool_call")
    offset = call["sent_files"][0]["offset"]
    assert [(f["n"], f["name"], f["kind"]) for f in call["sent_files"]] == [
        (0, "chart.png", "image"), (1, "notes.md", "markdown"), (2, "page.html", None),
    ]
    assert call["caption"] == "before vs after"
    assert "invented/place" not in json.dumps(call["sent_files"])
    assert st.transcript_sent_file(path, offset, 1, "claude") == ("notes.md", None)
    assert st.transcript_sent_file(path, offset, 3, "claude") is None
    assert st.transcript_sent_file(path, offset, 0, "codex") is None
    assert st.transcript_sent_file(path, offset + 1, 0, "claude") is None


def test_a_sent_file_name_loses_its_bidi_override(tmp_path: Path):
    from src import session_transcript as st

    path = _jsonl(tmp_path / "t.jsonl", [_prompt(), _send_call(["C:/invented/report\u202egnp.txt"])])
    call = next(e for e in st.transcript_page(path, flavor="claude")["entries"] if e["kind"] == "tool_call")
    assert call["sent_files"][0]["name"] == "reportgnp.txt"


# ------------------------------------------------------------------ routes


@pytest.fixture
def _bypass_gate(monkeypatch):
    from app.webapp import middleware
    monkeypatch.setattr(
        middleware, "LOOPBACK_HOSTS",
        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )


def _live(proj: Path, sid: str = "s1", agent: str = "claude") -> Dict[str, Any]:
    return {"session_id": sid, "kind": "pty", "agent": agent, "alive": True,
            "project_dir": str(proj), "started_at": 1_789_000_000}


def test_every_outbox_path_is_passkey_gated():
    from app.webapp.middleware import _terminal_guard_level

    for path in (
        "/api/claude-code/sessions/s1/outbox",
        "/api/claude-code/sessions/s1/outbox/abc123",
        "/api/claude-code/sessions/s1/transcript/file",
    ):
        assert _terminal_guard_level(path) == "passkey", path


def test_off_the_tailnet_the_outbox_is_not_reachable(webapp_client):
    client, _, _ = webapp_client
    assert client.get("/api/claude-code/sessions/s1/outbox").status_code == 403
    assert client.post("/api/claude-code/sessions/s1/outbox", json={"path": "x.png"}).status_code == 403


def test_a_post_that_is_not_from_the_pc_is_refused_by_the_handler(
    webapp_client, _bypass_gate, monkeypatch, proj: Path
):
    # Past the middleware (as a passkey holder on the tailnet would be), the
    # handler still takes files from this PC only.
    from app.webapp.routers import session_outbox_routes as outbox_routes

    client, _, overrides = webapp_client
    overrides["session"].list_sessions.return_value = [_live(proj)]
    (proj / "a.png").write_bytes(PNG)
    monkeypatch.setattr(outbox_routes, "is_pc_itself", lambda host, headers: False)
    res = client.post("/api/claude-code/sessions/s1/outbox", json={"path": str(proj / "a.png")})
    assert res.status_code == 403 and res.json()["detail"]["reason"] == "loopback_only"
    assert session_outbox.listing("s1")["entries"] == []


def test_post_list_and_serve_a_file(webapp_client, _bypass_gate, proj: Path):
    client, _, overrides = webapp_client
    overrides["session"].list_sessions.return_value = [_live(proj)]
    (proj / "chart.png").write_bytes(PNG)
    res = client.post(
        "/api/claude-code/sessions/s1/outbox",
        json={"path": "chart.png", "caption": "<img src=x onerror=alert(1)>"},
    )
    assert res.status_code == 200, res.text
    entry = res.json()["entry"]
    # Stored as text; the chat view escapes it on render.
    assert entry["caption"] == "<img src=x onerror=alert(1)>"
    listed = client.get("/api/claude-code/sessions/s1/outbox").json()
    assert listed["rev"] == 1 and listed["entries"] == [entry]
    got = client.get(f"/api/claude-code/sessions/s1/outbox/{entry['id']}")
    assert got.status_code == 200 and got.content == PNG
    assert got.headers["content-type"] == "image/png"
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["cache-control"] == "no-store"
    assert got.headers["content-security-policy"].startswith("sandbox")
    assert got.headers["content-disposition"] == "inline; filename*=UTF-8''chart.png"
    assert client.get("/api/claude-code/sessions/s1/outbox/" + "0" * 32).status_code == 404


def test_a_pdf_is_served_without_the_sandbox_its_viewer_cannot_run_under(
    webapp_client, _bypass_gate, proj: Path
):
    client, _, overrides = webapp_client
    overrides["session"].list_sessions.return_value = [_live(proj)]
    (proj / "doc.pdf").write_bytes(PDF)
    entry = client.post("/api/claude-code/sessions/s1/outbox", json={"path": str(proj / "doc.pdf")}).json()["entry"]
    got = client.get(f"/api/claude-code/sessions/s1/outbox/{entry['id']}")
    assert got.headers["content-type"] == "application/pdf"
    assert "content-security-policy" not in got.headers


def test_an_archive_is_served_as_an_attachment_byte_for_byte(webapp_client, _bypass_gate, proj: Path):
    client, _, overrides = webapp_client
    overrides["session"].list_sessions.return_value = [_live(proj)]
    data = _zip_bytes()
    (proj / "pack.zip").write_bytes(data)
    entry = client.post("/api/claude-code/sessions/s1/outbox", json={"path": "pack.zip"}).json()["entry"]
    assert entry["kind"] == "archive" and entry["media"] == "application/octet-stream"
    got = client.get(f"/api/claude-code/sessions/s1/outbox/{entry['id']}")
    assert got.status_code == 200
    assert hashlib.sha256(got.content).hexdigest() == hashlib.sha256(data).hexdigest()
    assert got.headers["content-type"] == "application/octet-stream"
    assert got.headers["content-disposition"] == "attachment; filename*=UTF-8''pack.zip"
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["cache-control"] == "no-store"
    assert got.headers["content-security-policy"].startswith("sandbox")


def test_images_pdfs_and_text_keep_an_inline_disposition(webapp_client, _bypass_gate, proj: Path):
    client, _, overrides = webapp_client
    overrides["session"].list_sessions.return_value = [_live(proj)]
    for name, data in (("doc.pdf", PDF), ("shot.png", PNG), ("note.txt", b"hi")):
        (proj / name).write_bytes(data)
        entry = client.post("/api/claude-code/sessions/s1/outbox", json={"path": name}).json()["entry"]
        got = client.get(f"/api/claude-code/sessions/s1/outbox/{entry['id']}")
        assert got.headers["content-disposition"].startswith("inline;"), name


def test_a_hostile_file_name_is_quoted_in_the_header(webapp_client, _bypass_gate, proj: Path):
    client, _, overrides = webapp_client
    overrides["session"].list_sessions.return_value = [_live(proj)]
    # Windows refuses `"` and `<` in a name; these are the header breakers it allows.
    name = "a; x=1, 'é' %0d.txt"
    (proj / name).write_text("hello", encoding="utf-8")
    entry = client.post("/api/claude-code/sessions/s1/outbox", json={"path": str(proj / name)}).json()["entry"]
    got = client.get(f"/api/claude-code/sessions/s1/outbox/{entry['id']}")
    quoted = got.headers["content-disposition"].split("filename*=UTF-8''", 1)[1]
    assert not set(quoted) & set(" ;,'\r\n") and "%0d" not in quoted
    assert quoted.startswith("a%3B%20x%3D1")
    assert got.headers["content-type"] == "text/plain; charset=utf-8"


@pytest.mark.parametrize("name,data,status,reason", [
    ("page.html", b"<b>x</b>", 415, "type_not_allowed"),
    ("fake.png", b"text", 415, "content_mismatch"),
    ("fake.zip", b"text", 415, "content_mismatch"),
    ("big.txt", b"x" * 32, 413, "too_large"),
    ("missing.png", None, 404, "not_found"),
])
def test_post_refusals_come_back_with_their_own_status_and_reason(
    webapp_client, _bypass_gate, monkeypatch, proj: Path, name, data, status, reason
):
    monkeypatch.setattr(session_outbox, "MAX_FILE_BYTES", 16)
    client, _, overrides = webapp_client
    overrides["session"].list_sessions.return_value = [_live(proj)]
    if data is not None:
        (proj / name).write_bytes(data)
    res = client.post("/api/claude-code/sessions/s1/outbox", json={"path": str(proj / name)})
    assert res.status_code == status and res.json()["detail"]["reason"] == reason


def test_post_out_of_root_is_403(webapp_client, _bypass_gate, tmp_path: Path, proj: Path):
    client, _, overrides = webapp_client
    overrides["session"].list_sessions.return_value = [_live(proj)]
    (tmp_path / "elsewhere.png").write_bytes(PNG)
    res = client.post("/api/claude-code/sessions/s1/outbox", json={"path": str(tmp_path / "elsewhere.png")})
    assert res.status_code == 403 and res.json()["detail"]["reason"] == "out_of_root"


def test_post_for_an_unknown_session_or_a_bad_body(webapp_client, _bypass_gate, proj: Path):
    client, _, overrides = webapp_client
    overrides["session"].list_sessions.return_value = [_live(proj, sid="other")]
    res = client.post("/api/claude-code/sessions/s1/outbox", json={"path": "a.png"})
    assert res.status_code == 404 and res.json()["detail"]["reason"] == "session_not_found"
    bad = client.post("/api/claude-code/sessions/s1/outbox", content=b"not json")
    assert bad.status_code == 400 and bad.json()["detail"]["reason"] == "bad_request"


def test_transcript_responses_carry_the_outbox_until_the_client_has_its_rev(
    webapp_client, _bypass_gate, monkeypatch, proj: Path, tmp_path: Path
):
    client, _, overrides = webapp_client
    overrides["session"].list_sessions.return_value = [_live(proj, agent="codex")]
    from app.webapp.routers import session_transcript as router_mod

    rollout = _jsonl(tmp_path / "rollout.jsonl", [{
        "timestamp": "2026-10-12T10:00:00Z", "type": "response_item",
        "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
    }])
    monkeypatch.setattr(router_mod, "find_codex_transcript", lambda session: rollout)
    page = client.get("/api/claude-code/sessions/s1/transcript").json()
    assert page["available"] is True and page["outbox_rev"] == 0 and page["outbox"] == []

    (proj / "a.txt").write_text("from codex", encoding="utf-8")
    entry = client.post("/api/claude-code/sessions/s1/outbox", json={"path": "a.txt"}).json()["entry"]
    tail_q = f"after={page['tail']}&size={page['size']}"
    moved = client.get(f"/api/claude-code/sessions/s1/transcript?{tail_q}&ob=0").json()
    assert moved["outbox_rev"] == 1 and moved["outbox"] == [entry]
    held = client.get(f"/api/claude-code/sessions/s1/transcript?{tail_q}&ob=1").json()
    assert held["outbox_rev"] == 1 and "outbox" not in held
    older = client.get("/api/claude-code/sessions/s1/transcript?before=1").json()
    assert "outbox" not in older


def test_send_user_file_is_served_through_the_same_guards(
    webapp_client, _bypass_gate, monkeypatch, tmp_path: Path, proj: Path
):
    client, _, overrides = webapp_client
    overrides["session"].list_sessions.return_value = [_live(proj)]
    (proj / "chart.png").write_bytes(PNG)
    (proj / "page.html").write_text("<b>x</b>", encoding="utf-8")
    (tmp_path / "elsewhere.png").write_bytes(PNG)
    path = _jsonl(tmp_path / "t.jsonl", [
        _prompt(),
        _send_call(["chart.png", str(tmp_path / "elsewhere.png"), "page.html"], cwd=str(proj)),
    ])
    monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: {"transcript_path": str(path)})
    entries = client.get("/api/claude-code/sessions/s1/transcript").json()["entries"]
    refs = next(e for e in entries if e["kind"] == "tool_call")["sent_files"]

    def fetch(ref):
        return client.get(f"/api/claude-code/sessions/s1/transcript/file?offset={ref['offset']}&n={ref['n']}")

    ok = fetch(refs[0])
    assert ok.status_code == 200 and ok.content == PNG
    assert ok.headers["content-type"] == "image/png" and ok.headers["x-content-type-options"] == "nosniff"
    out = fetch(refs[1])
    assert out.status_code == 403 and out.json()["detail"]["reason"] == "out_of_root"
    typed = fetch(refs[2])
    assert typed.status_code == 415 and typed.json()["detail"]["reason"] == "type_not_allowed"
    # An offset past anything seek() takes is a validation error, not a 500.
    huge = client.get(f"/api/claude-code/sessions/s1/transcript/file?offset={2**63}&n=0")
    assert huge.status_code == 422
    missing = client.get(f"/api/claude-code/sessions/s1/transcript/file?offset={refs[0]['offset']}&n=7")
    assert missing.status_code == 404 and missing.json()["detail"]["reason"] == "file_not_found"


# ------------------------------------------------- send folders (#1748)


def test_a_network_or_device_root_is_skipped_without_touching_the_filesystem(
    proj: Path, monkeypatch
):
    def _boom(self, *args, **kwargs):  # pragma: no cover - failing is the point
        raise AssertionError(f"resolve() reached for {self}")

    unc = [r"\\host\share", "//host/share", r"\\?\C:\x", r"\\.\pipe\p"]
    real = Path.resolve

    def _guarded(self, *args, **kwargs):
        if str(self).replace("/", "\\").startswith("\\\\"):
            return _boom(self)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _guarded)
    assert session_outbox.allowed_roots(str(proj), unc) == [proj.resolve()]
    for raw in unc:
        with pytest.raises(OutboxRefusal) as info:
            session_outbox.resolve_root(raw)
        assert info.value.reason == "out_of_root"


def test_a_file_in_an_added_folder_reads_and_one_outside_is_still_out_of_root(
    tmp_path: Path, proj: Path
):
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "note.md").write_text("# hi\n", encoding="utf-8")
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "note.md").write_text("# no\n", encoding="utf-8")
    roots = session_outbox.allowed_roots(str(proj), [str(drop)])
    _, _, kind, _ = session_outbox.read_source(str(drop / "note.md"), str(proj), roots)
    assert kind == "markdown"
    assert _refusal(other / "note.md", proj, roots) == "out_of_root"


def test_a_sibling_sharing_a_prefix_is_not_inside_an_added_folder(tmp_path: Path, proj: Path):
    drop = tmp_path / "drop"
    evil = tmp_path / "drop-evil"
    drop.mkdir()
    evil.mkdir()
    (evil / "x.txt").write_text("x", encoding="utf-8")
    roots = session_outbox.allowed_roots(str(proj), [str(drop)])
    assert _refusal(evil / "x.txt", proj, roots) == "out_of_root"


def test_check_root_accepts_a_folder_and_expands_variables(tmp_path: Path, monkeypatch):
    (tmp_path / "ok").mkdir()
    monkeypatch.setenv("OUTBOX_TEST_BASE", str(tmp_path))
    session_outbox.check_root("%OUTBOX_TEST_BASE%/ok")
    with pytest.raises(ValueError, match="already in the list"):
        session_outbox.check_root(str(tmp_path / "ok"), ["%OUTBOX_TEST_BASE%/ok"])
