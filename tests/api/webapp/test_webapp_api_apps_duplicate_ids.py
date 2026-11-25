"""By-id Apps routes refuse an ambiguous id instead of acting on the first row."""

from __future__ import annotations

import json

import pytest


def _seed_raw(path, rows) -> None:
    path.write_text(json.dumps({"scan_root": "C:\\stub", "apps": rows}), encoding="utf-8")


def _row(app_id: str, bat: str) -> dict:
    return {"id": app_id, "name": bat, "kind": "tray", "bat_path": bat}


def test_a_duplicate_registry_is_migrated_and_each_row_is_addressable(webapp_client):
    client, _, overrides = webapp_client
    path = overrides["tmp_registry_path"]
    _seed_raw(path, [_row("tray", "C:\\a\\tray.bat"), _row("tray", "C:\\b\\tray.bat")])

    listed = client.get("/api/apps").json()["apps"]
    ids = [a["id"] for a in listed if a["kind"] == "tray"]
    assert len(ids) == 2 and len(set(ids)) == 2

    later = next(a for a in listed if a["bat_path"] == "C:\\b\\tray.bat")
    resp = client.delete(f"/api/apps/{later['id']}")
    assert resp.status_code == 200
    left = client.get("/api/apps").json()["apps"]
    assert [a["bat_path"] for a in left if a["kind"] == "tray"] == ["C:\\a\\tray.bat"]


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.delete("/api/apps/tray"),
        lambda c: c.patch("/api/apps/tray", json={"name": "X"}),
        lambda c: c.patch("/api/apps/tray", json={"autostart": True}),
        lambda c: c.post("/api/apps/tray/launch"),
        lambda c: c.post("/api/apps/tray/refresh"),
    ],
    ids=["delete", "rename", "autostart", "launch", "refresh"],
)
def test_ambiguous_id_is_refused_with_its_reason(webapp_client, monkeypatch, call):
    client, _, _ = webapp_client
    from app.webapp.routers import apps as apps_router
    from src.registry import AppEntry, Registry

    ambiguous = Registry(
        scan_root="",
        apps=[
            AppEntry(id="tray", name="A", kind="tray", bat_path="C:\\a\\tray.bat"),
            AppEntry(id="tray", name="B", kind="tray", bat_path="C:\\b\\tray.bat"),
        ],
    )
    # a load that migrates cannot yield this; the guard is for any registry that slips past it
    monkeypatch.setattr(apps_router, "load_registry", lambda *a, **k: ambiguous)
    resp = call(client)
    assert resp.status_code == 409
    assert "tray" in resp.json()["detail"] and "2 apps" in resp.json()["detail"]
    assert len(ambiguous.apps) == 2
