"""POST /api/life-os/favorites + the /api/life-os/skills is_favorite flag (#1070).

The Life OS half of the Coding tab's favorites feature (#250), deliberately
built to the same contract: an idempotent toggle that persists to
webapp_config and surfaces on the next list read. The frontend's star and
its favorites-first partition are pinned by the e2e suite
(tests/e2e/test_life_os_tab.py); this covers the backend, including the part
the e2e cannot reach because it route-mocks the list — that the state
actually reaches disk and survives a webapp restart.
"""

from __future__ import annotations

from pathlib import Path


def _isolate_life_os(client, tmp_path: Path) -> Path:
    """Point life_os_dir at a temp checkout, through the config *file*.

    Assigning app.state.webapp_config in memory is not enough, and is an
    actively dangerous shortcut here: POST /api/life-os/favorites calls
    update_webapp_config(), which reloads from disk and replaces
    app.state.webapp_config wholesale — so an in-memory override silently
    reverts mid-test and the scanner walks the developer's *real* life-os
    checkout. That would make these assertions depend on whichever private
    skills happen to exist on the box. Writing it through /api/config
    persists it, so it survives the round trip.
    """
    root = tmp_path / "life-os"
    (root / ".claude" / "skills").mkdir(parents=True, exist_ok=True)
    resp = client.post("/api/config", json={"life_os_dir": str(root)})
    assert resp.status_code == 200
    return root


def _make_skill(root: Path, skill_id: str) -> None:
    """Create a life-os skill on disk so the scanner reports it."""
    d = root / ".claude" / "skills" / skill_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        "---\nname: " + skill_id + "\ndescription: test skill\n---\n\nbody\n",
        encoding="utf-8",
    )


def _skills(client) -> dict:
    body = client.get("/api/life-os/skills").json()
    return {s["id"]: s for s in body["skills"]}


class TestLifeOsFavoritesEndpoint:
    def test_star_then_unstar_round_trips_and_persists(self, webapp_client, tmp_path):
        from src.webapp_config import load_webapp_config

        client, app, overrides = webapp_client
        root = _isolate_life_os(client, tmp_path)
        _make_skill(root, "journal-daily")

        rows = _skills(client)
        assert list(rows) == ["journal-daily"], list(rows)
        assert rows["journal-daily"]["is_favorite"] is False
        assert app.state.webapp_config.life_os_favorites == []

        resp = client.post(
            "/api/life-os/favorites",
            json={"id": "journal-daily", "favorite": True},
        )
        assert resp.status_code == 200
        assert resp.json()["life_os_favorites"] == ["journal-daily"]
        assert app.state.webapp_config.life_os_favorites == ["journal-daily"]
        assert _skills(client)["journal-daily"]["is_favorite"] is True

        # Reaches disk — a star must survive a webapp restart, which is the
        # whole reason this is a server round trip and not localStorage.
        cfg_path = overrides["tmp_webapp_cfg_path"]
        assert load_webapp_config(cfg_path).life_os_favorites == ["journal-daily"]

        resp = client.post(
            "/api/life-os/favorites",
            json={"id": "journal-daily", "favorite": False},
        )
        assert resp.status_code == 200
        assert app.state.webapp_config.life_os_favorites == []
        assert _skills(client)["journal-daily"]["is_favorite"] is False

    def test_toggle_is_idempotent_and_rejects_a_missing_id(
        self, webapp_client, tmp_path
    ):
        """A double-tap from the phone must not corrupt the list, and a body
        with no id is a 400 rather than a silently stored empty entry."""
        client, app, _ = webapp_client
        _isolate_life_os(client, tmp_path)

        for _ in range(3):
            resp = client.post(
                "/api/life-os/favorites",
                json={"id": "sparring-work", "favorite": True},
            )
            assert resp.status_code == 200
        assert app.state.webapp_config.life_os_favorites == ["sparring-work"]

        # Unstarring an id that is not in the list is also a no-op 200.
        resp = client.post(
            "/api/life-os/favorites", json={"id": "not-starred", "favorite": False}
        )
        assert resp.status_code == 200
        assert app.state.webapp_config.life_os_favorites == ["sparring-work"]

        for bad in ({}, {"id": ""}, {"id": "   ", "favorite": True}):
            assert client.post("/api/life-os/favorites", json=bad).status_code == 400

    def test_a_stale_favorite_is_harmless(self, webapp_client, tmp_path):
        """A skill can be renamed or removed on disk between a star and the
        next scan. The stored id is deliberately not validated against the
        scanner, so a stale entry simply matches nothing — it must not 500,
        and it must not invent a row."""
        client, app, _ = webapp_client
        root = _isolate_life_os(client, tmp_path)
        _make_skill(root, "journal-daily")
        client.post(
            "/api/life-os/favorites", json={"id": "gone-from-disk", "favorite": True}
        )

        body = client.get("/api/life-os/skills").json()
        assert [s["id"] for s in body["skills"]] == ["journal-daily"]
        assert body["skills"][0]["is_favorite"] is False
