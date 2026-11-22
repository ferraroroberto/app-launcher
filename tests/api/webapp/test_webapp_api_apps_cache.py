"""/api/apps row cache: the poll rebuilds rows only when their files change."""

from __future__ import annotations

from src.registry import AppEntry, Registry, save_registry


class TestRowsCache:
    """The 4 s poll re-reads apps.json and re-scans projects_dir only when
    either changes, or once the cached rows are a minute old."""

    def test_unchanged_files_skip_the_rebuild(self, webapp_client, monkeypatch):
        from app.webapp.routers import apps as apps_router

        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "proj").mkdir()
        scans = []
        real = apps_router._claude_code_entries
        monkeypatch.setattr(
            apps_router, "_claude_code_entries",
            lambda cfg: scans.append(1) or real(cfg),
        )
        for _ in range(3):
            assert client.get("/api/apps").status_code == 200
        assert len(scans) == 1

        # A new project folder changes the dir's child list: rebuilt at once.
        (overrides["tmp_projects_dir"] / "proj-two").mkdir()
        names = {a["name"] for a in client.get("/api/apps").json()["apps"]}
        assert names == {"proj", "proj-two"}
        assert len(scans) == 2

        # A save to apps.json is seen on the next read.
        save_registry(Registry(scan_root="C:\\stub", apps=[
            AppEntry(id="alpha", name="Alpha", kind="streamlit",
                     bat_path="C:\\stub\\alpha.bat"),
        ]), path=overrides["tmp_registry_path"])
        names = {a["name"] for a in client.get("/api/apps").json()["apps"]}
        assert "Alpha" in names
        assert len(scans) == 3

        # Nothing changed but a minute passed: rebuilt (a remote edit inside a
        # project touches neither file).
        apps_router._rows_cache["at"] -= apps_router._ROWS_MAX_AGE_S + 1
        client.get("/api/apps")
        assert len(scans) == 4
