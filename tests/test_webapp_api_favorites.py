"""POST /api/claude-code/favorites + the /api/apps is_favorite flag (#250).

Covers the Coding-tab favorites backend: a project's favorite state toggles
idempotently, persists to webapp_config, and surfaces on the live claude-code
rows of /api/apps. The frontend partition/filter is pinned separately by the
e2e suite (tests/e2e/test_coding_favorites.py)."""

from __future__ import annotations

from pathlib import Path


def _make_project(app, name: str) -> str:
    """Create a project dir under the configured projects_dir, return its
    scanner id as /api/apps reports it."""
    projects_dir = Path(app.state.webapp_config.projects_dir)
    (projects_dir / name).mkdir()
    return name


def _coding_rows(client) -> dict:
    body = client.get("/api/apps").json()
    return {a["id"]: a for a in body["apps"] if a["kind"] == "claude-code"}


class TestFavoritesEndpoint:
    def test_star_then_unstar_round_trips(self, webapp_client):
        client, app, _ = webapp_client
        _make_project(app, "alphaproj")
        rows = _coding_rows(client)
        assert "alphaproj" in rows
        # Default: not a favorite anywhere.
        assert rows["alphaproj"]["is_favorite"] is False
        assert app.state.webapp_config.coding_favorites == []

        # Star it.
        resp = client.post(
            "/api/claude-code/favorites",
            json={"id": "alphaproj", "favorite": True},
        )
        assert resp.status_code == 200
        assert resp.json()["coding_favorites"] == ["alphaproj"]
        assert app.state.webapp_config.coding_favorites == ["alphaproj"]
        # And it surfaces on the live row.
        assert _coding_rows(client)["alphaproj"]["is_favorite"] is True

        # Unstar it.
        resp = client.post(
            "/api/claude-code/favorites",
            json={"id": "alphaproj", "favorite": False},
        )
        assert resp.status_code == 200
        assert resp.json()["coding_favorites"] == []
        assert app.state.webapp_config.coding_favorites == []
        assert _coding_rows(client)["alphaproj"]["is_favorite"] is False

    def test_persists_to_disk(self, webapp_client):
        """A toggle writes through webapp_config so it survives a reload —
        the whole point of favorites outliving the process."""
        from src.webapp_config import load_webapp_config

        client, app, _ = webapp_client
        _make_project(app, "betaproj")
        client.post(
            "/api/claude-code/favorites",
            json={"id": "betaproj", "favorite": True},
        )
        # Re-read from disk (DEFAULT_CONFIG_PATH is monkeypatched to tmp).
        assert load_webapp_config().coding_favorites == ["betaproj"]

    def test_double_star_is_idempotent(self, webapp_client):
        """A double-tap from the phone can't duplicate the id."""
        client, app, _ = webapp_client
        _make_project(app, "gammaproj")
        for _ in range(2):
            client.post(
                "/api/claude-code/favorites",
                json={"id": "gammaproj", "favorite": True},
            )
        assert app.state.webapp_config.coding_favorites == ["gammaproj"]

    def test_missing_id_is_400(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.post("/api/claude-code/favorites", json={"favorite": True})
        assert resp.status_code == 400

    def test_favorite_for_absent_project_is_kept(self, webapp_client):
        """The endpoint stores ids, not paths — a starred id for a project
        that isn't currently on disk is preserved (the dir may return), and
        simply never matches a live row until it does."""
        client, app, _ = webapp_client
        resp = client.post(
            "/api/claude-code/favorites",
            json={"id": "not-on-disk", "favorite": True},
        )
        assert resp.status_code == 200
        assert app.state.webapp_config.coding_favorites == ["not-on-disk"]
