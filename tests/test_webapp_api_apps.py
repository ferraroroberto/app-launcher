"""/api/apps surface — list, scan, save, rename, delete."""

from __future__ import annotations

import json
from datetime import datetime

from src.registry import AppEntry, Registry, save_registry


def _seed_registry(tmp_registry_path, apps):
    """Helper: persist a synthetic registry to the tmp path the conftest
    pointed DEFAULT_REGISTRY_PATH at, so subsequent ``load_registry()``
    calls inside route handlers pick it up."""
    save_registry(
        Registry(scan_root="C:\\stub", apps=apps), path=tmp_registry_path
    )


class TestGetApps:
    def test_empty_registry_returns_empty_list(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/api/apps")
        assert resp.status_code == 200
        body = resp.json()
        assert body["apps"] == []

    def test_lists_seeded_entries(self, webapp_client):
        client, _, overrides = webapp_client
        _seed_registry(
            overrides["tmp_registry_path"],
            [
                AppEntry(
                    id="alpha",
                    name="Alpha",
                    kind="streamlit",
                    bat_path="C:\\stub\\alpha.bat",
                    added_at=datetime.now().isoformat(),
                ),
                AppEntry(
                    id="beta",
                    name="Beta",
                    kind="claude-code",
                    project_dir="C:\\stub\\beta",
                    added_at=datetime.now().isoformat(),
                ),
            ],
        )
        resp = client.get("/api/apps")
        assert resp.status_code == 200
        names = {a["name"] for a in resp.json()["apps"]}
        assert names == {"Alpha", "Beta"}


class TestScanApps:
    def test_returns_new_key_with_list(self, webapp_client, monkeypatch):
        client, _, _ = webapp_client
        # discover_new is imported at module level into routers/apps.py
        from app.webapp.routers import apps as apps_router
        monkeypatch.setattr(apps_router, "discover_new", lambda **_: [])
        resp = client.post("/api/apps/scan")
        assert resp.status_code == 200
        assert resp.json() == {"new": []}


class TestSaveApps:
    def test_400_on_empty_ids(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.post("/api/apps/save", json={"ids": []})
        assert resp.status_code == 400

    def test_persists_selected_ids(self, webapp_client, monkeypatch):
        client, _, _ = webapp_client
        from app.webapp.routers import apps as apps_router
        candidate = AppEntry(
            id="freshapp",
            name="Fresh App",
            kind="streamlit",
            bat_path="C:\\stub\\fresh.bat",
            added_at=datetime.now().isoformat(),
        )
        monkeypatch.setattr(apps_router, "discover_new", lambda **_: [candidate])
        resp = client.post("/api/apps/save", json={"ids": ["freshapp"]})
        assert resp.status_code == 200
        added = resp.json()["added"]
        assert len(added) == 1
        assert added[0]["id"] == "freshapp"


class TestRenameApp:
    def test_404_on_unknown_id(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.patch("/api/apps/nope", json={"name": "Whatever"})
        assert resp.status_code == 404

    def test_400_on_empty_name(self, webapp_client, overrides=None):
        client, _, overrides = webapp_client
        _seed_registry(
            overrides["tmp_registry_path"],
            [
                AppEntry(
                    id="alpha",
                    name="Alpha",
                    kind="streamlit",
                    bat_path="C:\\stub\\alpha.bat",
                    added_at=datetime.now().isoformat(),
                ),
            ],
        )
        resp = client.patch("/api/apps/alpha", json={"name": "   "})
        assert resp.status_code == 400

    def test_round_trips(self, webapp_client):
        client, _, overrides = webapp_client
        _seed_registry(
            overrides["tmp_registry_path"],
            [
                AppEntry(
                    id="alpha",
                    name="Alpha",
                    kind="streamlit",
                    bat_path="C:\\stub\\alpha.bat",
                    added_at=datetime.now().isoformat(),
                ),
            ],
        )
        resp = client.patch("/api/apps/alpha", json={"name": "Renamed"})
        assert resp.status_code == 200
        assert resp.json()["app"]["name"] == "Renamed"


class TestDeleteApp:
    def test_404_on_unknown_id(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.delete("/api/apps/nope")
        assert resp.status_code == 404

    def test_removes_existing_entry(self, webapp_client):
        client, _, overrides = webapp_client
        _seed_registry(
            overrides["tmp_registry_path"],
            [
                AppEntry(
                    id="alpha",
                    name="Alpha",
                    kind="streamlit",
                    bat_path="C:\\stub\\alpha.bat",
                    added_at=datetime.now().isoformat(),
                ),
            ],
        )
        resp = client.delete("/api/apps/alpha")
        assert resp.status_code == 200
        assert resp.json()["removed"] == "alpha"
        # And it's gone from /api/apps.
        listing = client.get("/api/apps").json()
        assert all(a["id"] != "alpha" for a in listing["apps"])
