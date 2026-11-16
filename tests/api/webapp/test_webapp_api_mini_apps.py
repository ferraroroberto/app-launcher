"""The Mini apps group: a folder of ``.bat`` files scanned into ``mini`` rows.

Real temp folders and the real registry file; only the process spawn is stubbed.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def mini_folder(webapp_client, tmp_path: Path) -> Path:
    """A mini apps folder with two bats, a non-bat file and a subfolder bat."""
    _, app, _ = webapp_client
    folder = tmp_path / "mini_apps"
    (folder / "nested").mkdir(parents=True)
    (folder / "clip_helper.bat").write_text("@echo off\r\necho one\r\n", encoding="utf-8")
    (folder / "disk-watch.BAT").write_text("@echo off\r\necho two\r\n", encoding="utf-8")
    (folder / "notes.txt").write_text("not a bat", encoding="utf-8")
    (folder / "nested" / "deep.bat").write_text("@echo off\r\n", encoding="utf-8")
    app.state.webapp_config.mini_apps_root = str(folder)
    return folder


def _scan_and_save(client) -> list[dict]:
    new = client.post("/api/apps/scan").json()["new"]
    ids = [a["id"] for a in new]
    saved = client.post("/api/apps/save", json={"ids": ids})
    assert saved.status_code == 200
    return saved.json()["added"]


def test_scan_lists_the_folders_bats_as_mini_rows(webapp_client, mini_folder: Path):
    client, _, _ = webapp_client
    new = client.post("/api/apps/scan").json()["new"]
    minis = {a["name"]: a for a in new if a["kind"] == "mini"}
    assert set(minis) == {"Clip Helper", "Disk Watch"}
    assert minis["Clip Helper"]["id"] == "mini-clip-helper"
    assert minis["Clip Helper"]["bat_path"] == str(mini_folder / "clip_helper.bat")
    # A new row starts with autostart off.
    assert all(a["autostart"] is False for a in minis.values())


def test_saved_rows_persist_and_the_fold_is_enabled(webapp_client, mini_folder: Path):
    client, _, _ = webapp_client
    _scan_and_save(client)
    body = client.get("/api/apps").json()
    assert body["mini_apps_enabled"] is True
    assert {a["id"] for a in body["apps"] if a["kind"] == "mini"} == {
        "mini-clip-helper", "mini-disk-watch",
    }
    # A second scan finds nothing new.
    assert client.post("/api/apps/scan").json()["new"] == []


def test_without_the_folder_nothing_is_scanned_and_the_fold_is_off(webapp_client):
    client, _, overrides = webapp_client
    stray = overrides["tmp_apps_scan_root"] / "stray.bat"
    stray.write_text("@echo off\r\n", encoding="utf-8")
    new = client.post("/api/apps/scan").json()["new"]
    assert all(a["kind"] != "mini" for a in new)
    body = client.get("/api/apps").json()
    assert body["mini_apps_enabled"] is False
    assert all(a["kind"] != "mini" for a in body["apps"])


def test_a_bat_in_both_roots_lists_once_as_mini(webapp_client, mini_folder: Path):
    client, app, overrides = webapp_client
    # The mini folder sits inside the scan root, and its bat is the kind the
    # tree scan would classify as a web app.
    app.state.webapp_config.apps_scan_root = str(mini_folder.parent)
    (mini_folder / "serve_it.bat").write_text("uvicorn app:app\r\n", encoding="utf-8")
    new = client.post("/api/apps/scan").json()["new"]
    paths = [a["bat_path"] for a in new if a["bat_path"].endswith("serve_it.bat")]
    assert len(paths) == 1
    assert next(a for a in new if a["bat_path"] == paths[0])["kind"] == "mini"


def test_mini_rows_rename_autostart_launch_and_remove(
    webapp_client, mini_folder: Path, monkeypatch
):
    client, _, _ = webapp_client
    _scan_and_save(client)

    renamed = client.patch("/api/apps/mini-clip-helper", json={"name": "Clipboard"})
    assert renamed.status_code == 200 and renamed.json()["app"]["name"] == "Clipboard"

    on = client.patch("/api/apps/mini-clip-helper", json={"autostart": True})
    assert on.json()["app"]["autostart"] is True
    listing = client.get("/api/apps").json()["apps"]
    assert {a["id"]: a["autostart"] for a in listing if a["kind"] == "mini"} == {
        "mini-clip-helper": True, "mini-disk-watch": False,
    }

    from app.webapp.routers import apps as apps_router

    spawns: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        apps_router, "spawn_bat",
        lambda path, stealth=False: spawns.append((str(path), stealth)) or 4321,
    )
    recorded: list[tuple] = []
    monkeypatch.setattr(apps_router.app_runtime, "record_spawn", lambda *a: recorded.append(a))
    for stealth in (False, True):
        resp = client.post("/api/apps/mini-clip-helper/launch", json={"stealth": stealth})
        assert resp.status_code == 200 and resp.json()["kind"] == "mini"
    assert spawns == [(str(mini_folder / "clip_helper.bat"), False)] + [
        (str(mini_folder / "clip_helper.bat"), True)
    ]
    # A launched mini app is tracked, so the Running card lists it.
    assert recorded[0] == ("mini-clip-helper", "Clipboard", "mini", 4321)

    assert client.delete("/api/apps/mini-disk-watch").json() == {"removed": "mini-disk-watch"}
    assert all(a["id"] != "mini-disk-watch" for a in client.get("/api/apps").json()["apps"])
