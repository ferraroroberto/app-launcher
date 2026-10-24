"""Life OS archived conversations: list, read and restore from the zombie backup.

A fictional backup under ``tmp_path``; the title writer and the search-db rebuild
are stubbed, so nothing here reads the real backup, Life OS tree or home.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import life_os_zombie_backup

SKILL = "journal-daily"
DAY = "2026-09-14"
FERRY = "2026-09-13-0800-ferry-plans.md"
QUIET = "2026-09-13-0900-quiet-start.md"
GREETING = "**Claude**: Hello, shall we plan the island ferry?"


def _archive(root: Path, capture: str, title: str, day: str = DAY) -> None:
    folder = root / day / SKILL
    folder.mkdir(parents=True, exist_ok=True)
    (folder / capture).write_text(f'<!-- capture agent="claude" -->\n{GREETING}\n', encoding="utf-8")
    row = {"skill": SKILL, "file": capture, "date": capture[:10], "title": title}
    (folder / f"{capture}.manifest.json").write_text(json.dumps({
        "version": 1, "skill": SKILL, "capture": capture, "agent": "claude", "sid": "",
        "moved_at": f"{day}T04:00:00+02:00", "transcript_project": "", "index_json_row": row,
        "index_md_block": None, "title": title,
    }), encoding="utf-8")


@pytest.fixture
def archive(life_os_client, tmp_path, monkeypatch):
    from app.webapp import middleware
    monkeypatch.setattr(middleware, "LOOPBACK_HOSTS", frozenset({"testclient"}))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    root = tmp_path / "zombie-backup"
    monkeypatch.setattr(life_os_zombie_backup, "backup_root", lambda: root)
    monkeypatch.setattr(life_os_zombie_backup, "resync_search_index", lambda cfg: None)
    titles = []

    class Done:
        returncode = 0
    monkeypatch.setattr(life_os_zombie_backup, "run_title_cli",
                        lambda cfg, skill, file, title: titles.append(title) or Done())
    _archive(root, FERRY, "Ferry plans")
    _archive(root, QUIET, "", day="2026-09-10")
    client, _app, overrides = life_os_client
    conv = overrides["life_os_dir"] / ".claude" / "skills" / SKILL / "conversations"
    return client, root, conv, titles


def test_lists_newest_run_first(archive):
    client, *_ = archive
    rows = client.get("/api/life-os/archived").json()["archived"]
    assert [(r["day"], r["capture"], r["title"]) for r in rows] == [
        (DAY, FERRY, "Ferry plans"), ("2026-09-10", QUIET, "")]


@pytest.mark.parametrize("query,skill,expected", [
    ("FERRY", "", [FERRY, QUIET]),     # the title, then the capture's own text
    ("quiet", "", [QUIET]),            # the file name
    ("nothing like it", "", []),
    ("", "other-skill", []),
])
def test_filters_by_query_and_skill(archive, query, skill, expected):
    client, *_ = archive
    rows = client.get("/api/life-os/archived", params={"q": query, "skill": skill}).json()["archived"]
    assert [r["capture"] for r in rows] == expected


def test_reads_one_as_chat_entries(archive):
    client, *_ = archive
    body = client.get(f"/api/life-os/archived/{DAY}/{SKILL}/{FERRY}/transcript").json()
    assert body["available"] and body["agent"] == "claude"
    assert [e["kind"] for e in body["entries"]] == ["assistant"]


@pytest.mark.parametrize("path,status", [
    (f"not-a-day/{SKILL}/{FERRY}", 400),
    (f"{DAY}/%2e%2e/{FERRY}", 400),
    (f"{DAY}/{SKILL}/%2e%2e", 400),
    (f"{DAY}/{SKILL}/C:", 400),
    (f"{DAY}/{SKILL}/nope.md", 404),
    (f"{DAY}/other-skill/{FERRY}", 404),
    (f"2026-01-01/{SKILL}/{FERRY}", 404),
])
def test_unknown_or_escaping_parts_are_refused(archive, path, status):
    client, *_ = archive
    assert client.get(f"/api/life-os/archived/{path}/transcript").status_code == status
    assert client.post(f"/api/life-os/archived/{path}/restore").status_code == status


def test_an_absolute_path_never_reaches_the_backup(archive):
    client, *_ = archive
    # An encoded separator decodes before routing, so it can only miss the route.
    resp = client.get(f"/api/life-os/archived/{DAY}/{SKILL}/%2Fetc%2Fpasswd/transcript")
    assert resp.status_code == 404


def test_restore_puts_it_back_with_its_title(archive):
    client, root, conv, titles = archive
    resp = client.post(f"/api/life-os/archived/{DAY}/{SKILL}/{FERRY}/restore")
    assert resp.status_code == 200 and resp.json()["status"] == "restored"
    assert (conv / FERRY).read_text("utf-8").endswith(GREETING + "\n")
    assert titles == ["Ferry plans"]
    assert FERRY in [r["file"] for r in json.loads((conv / "index.json").read_text("utf-8"))]
    assert [r["capture"] for r in client.get("/api/life-os/archived").json()["archived"]] == [QUIET]


def test_a_refused_restore_says_why_and_touches_nothing(archive):
    client, root, conv, _ = archive
    (conv / FERRY).write_text("a newer capture", encoding="utf-8")
    before = sorted((p.as_posix(), p.read_bytes()) for p in root.rglob("*") if p.is_file())
    resp = client.post(f"/api/life-os/archived/{DAY}/{SKILL}/{FERRY}/restore")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "a capture with this name already exists"
    assert (conv / FERRY).read_text("utf-8") == "a newer capture"
    assert sorted((p.as_posix(), p.read_bytes()) for p in root.rglob("*") if p.is_file()) == before


def test_a_failed_move_is_its_own_error(archive, monkeypatch):
    client, *_ = archive
    def boom(*args):
        raise OSError("disk gone")
    monkeypatch.setattr(life_os_zombie_backup.shutil, "move", boom)
    resp = client.post(f"/api/life-os/archived/{DAY}/{SKILL}/{FERRY}/restore")
    assert resp.status_code == 500 and resp.json()["detail"] == "a file could not be moved back"


def test_archived_routes_are_passkey_gated():
    from app.webapp.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/life-os/archived") == "passkey"
    assert _terminal_guard_level(f"/api/life-os/archived/{DAY}/{SKILL}/{FERRY}/restore") == "passkey"
