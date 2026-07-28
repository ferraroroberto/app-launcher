"""Basic webapp routes: /healthz, /, /api/status, /api/claude-code/flags."""

from __future__ import annotations

import re


class TestHealthz:
    def test_healthz_ok(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["service"] == "launcher"


class TestIndex:
    def test_index_returns_html(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_index_is_no_cache(self, webapp_client):
        # Index must always be revalidated — without this, the PWA can
        # hold a stale shell that references a JS bundle that no longer
        # exists. Cache hygiene contract for issue #30.
        client, _, _ = webapp_client
        resp = client.get("/")
        assert "no-cache" in resp.headers.get("cache-control", "")

    def test_index_stamps_asset_urls(self, webapp_client):
        # Every /static/<name>.(css|js) referenced from the index must
        # carry an ?v=<8-hex-chars> stamp so iOS can't cache across an
        # asset edit.
        client, _, _ = webapp_client
        resp = client.get("/")
        body = resp.text
        assert "/static/styles.css?v=" in body
        assert "/static/main.js?v=" in body
        # No literal ?v=18 left over from the manual era. Match the complete
        # legacy stamp (the closing quote) — a substring "?v=18" also matches
        # valid 8-hex fleet hashes that happen to begin "18…" (the 8-hex
        # format itself is enforced by the loop below).
        assert '?v=18"' not in body
        # Stamps are 8 hex chars — including subdirectory (_vendored/) URLs
        # (issue #395: subdir assets used to be silently skipped).
        stamps = re.findall(r"/static/[\w\-./]+\.(?:css|js)\?v=([a-f0-9]+)", body)
        assert stamps, "expected at least one stamped asset URL"
        for stamp in stamps:
            assert re.fullmatch(r"[a-f0-9]{8}", stamp), stamp

    def test_index_stamps_vendored_subdir_css(self, webapp_client):
        # Regression net for issue #395: /static/_vendored/**/*.css hrefs
        # must get ?v=<hash> too, not just root-level /static/<file> ones.
        client, _, _ = webapp_client
        resp = client.get("/")
        body = resp.text
        assert "/static/_vendored/nav/nav-tabs.css?v=" in body


class TestStaticCaching:
    def test_js_served_immutable_year(self, webapp_client):
        # Hashed assets get year-long immutable cache — safe because the
        # URL changes on edit.
        client, _, _ = webapp_client
        resp = client.get("/static/main.js")
        assert resp.status_code == 200
        cache_control = resp.headers.get("cache-control", "")
        assert "max-age=31536000" in cache_control
        assert "immutable" in cache_control

    def test_js_imports_get_stamped(self, webapp_client):
        # JS files have their own ES-module imports rewritten at serve
        # time, so editing state.js invalidates everything that imports
        # it transitively (via the shared fleet hash).
        client, _, _ = webapp_client
        resp = client.get("/static/main.js")
        body = resp.text
        # main.js imports ./state.js, ./api.js, etc — all should be stamped.
        assert re.search(r"from\s+['\"]\./state\.js\?v=[a-f0-9]{8}['\"]", body)
        assert re.search(r"from\s+['\"]\./api\.js\?v=[a-f0-9]{8}['\"]", body)

    def test_vendored_subdir_import_gets_stamped(self, webapp_client):
        # Regression net for issue #395: main.js's `./_vendored/icons/
        # icons.js` import used to be silently skipped because the old
        # regex/hash-key only matched root-level filenames.
        client, _, _ = webapp_client
        resp = client.get("/static/main.js")
        body = resp.text
        assert re.search(
            r"from\s+['\"]\./_vendored/icons/icons\.js\?v=[a-f0-9]{8}['\"]", body
        )


class TestVersion:
    def test_version_shape(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/api/version")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {
            "git_sha", "built_at", "asset_hash", "head_sha", "session_host",
        }
        assert isinstance(body["git_sha"], str) and body["git_sha"]
        assert isinstance(body["built_at"], str) and body["built_at"]
        assert isinstance(body["asset_hash"], str)
        assert isinstance(body["head_sha"], str) and body["head_sha"]

    def test_session_host_unreachable_reports_reachable_false(self, webapp_client):
        """Default test-env mock (#615): no session-host listening —
        session_client.identity() returns None, never a false "up to date"."""
        client, _, _ = webapp_client
        body = client.get("/api/version").json()
        assert body["session_host"] == {
            "reachable": False, "git_sha": None, "started_at": None,
            "stale": None, "stale_relevant": None,
        }

    def test_session_host_stale_relevant_true_when_declared_path_touched(
        self, webapp_client, monkeypatch
    ):
        """#635: sha differs AND the diff touched a declared session-host
        path — the actionable "needs restart" case."""
        client, app, overrides = webapp_client
        from app.webapp.routers import misc as misc_router
        # #655: pin resolve_deployed_sha instead of relying on the ambient
        # checkout -- CI's shallow PR checkout can resolve it to "unknown".
        monkeypatch.setattr(misc_router, "resolve_deployed_sha", lambda *a, **k: "cafebee")
        monkeypatch.setattr(misc_router, "_session_host_path_relevance", lambda h, hd: True)
        overrides["session"].identity.return_value = {
            "git_sha": "deadbee", "started_at": "2026-07-24T05:51:34",
        }
        body = client.get("/api/version").json()
        assert body["session_host"]["reachable"] is True
        assert body["session_host"]["git_sha"] == "deadbee"
        assert body["session_host"]["started_at"] == "2026-07-24T05:51:34"
        assert body["session_host"]["stale"] is True
        assert body["session_host"]["stale_relevant"] is True

    def test_session_host_stale_relevant_false_when_only_unrelated_paths_touched(
        self, webapp_client, monkeypatch
    ):
        """#635: sha differs but the diff only touched paths outside the
        session-host's declaration — stale for the repo, not for :8446."""
        client, app, overrides = webapp_client
        from app.webapp.routers import misc as misc_router
        # #655: pin resolve_deployed_sha instead of relying on the ambient
        # checkout -- CI's shallow PR checkout can resolve it to "unknown".
        monkeypatch.setattr(misc_router, "resolve_deployed_sha", lambda *a, **k: "cafebee")
        monkeypatch.setattr(misc_router, "_session_host_path_relevance", lambda h, hd: False)
        overrides["session"].identity.return_value = {
            "git_sha": "deadbee", "started_at": "2026-07-24T05:51:34",
        }
        body = client.get("/api/version").json()
        assert body["session_host"]["stale"] is True
        assert body["session_host"]["stale_relevant"] is False

    def test_session_host_stale_relevant_unknown_when_scope_check_cannot_run(
        self, webapp_client, monkeypatch
    ):
        """#635: sha differs but the scoped diff itself is unresolvable
        (e.g. host sha not in local history) — must read as unknown, never
        a confident "unaffected"."""
        client, app, overrides = webapp_client
        from app.webapp.routers import misc as misc_router
        # #655: pin resolve_deployed_sha instead of relying on the ambient
        # checkout -- CI's shallow PR checkout can resolve it to "unknown".
        monkeypatch.setattr(misc_router, "resolve_deployed_sha", lambda *a, **k: "cafebee")
        monkeypatch.setattr(misc_router, "_session_host_path_relevance", lambda h, hd: None)
        overrides["session"].identity.return_value = {
            "git_sha": "deadbee", "started_at": "2026-07-24T05:51:34",
        }
        body = client.get("/api/version").json()
        assert body["session_host"]["stale"] is True
        assert body["session_host"]["stale_relevant"] is None

    def test_session_host_not_stale_when_sha_matches_deployed(self, webapp_client, monkeypatch):
        client, app, overrides = webapp_client
        from app.webapp.routers import misc as misc_router
        # #655: pin resolve_deployed_sha instead of relying on the ambient
        # checkout -- CI's shallow PR checkout can resolve it to "unknown".
        monkeypatch.setattr(misc_router, "resolve_deployed_sha", lambda *a, **k: "cafebee")
        overrides["session"].identity.return_value = {
            "git_sha": "cafebee", "started_at": "2026-07-27T08:00:00",
        }
        body = client.get("/api/version").json()
        assert body["session_host"]["stale"] is False
        assert body["session_host"]["stale_relevant"] is False

    def test_stale_relevant_compares_against_deployed_ref_not_checkout_branch(
        self, webapp_client, monkeypatch
    ):
        """#641 regression: the primary checkout can transiently sit on an
        unrelated feature branch that never contained a merged, declared-path
        commit. ``stale_relevant`` must compare against the resolved deployed
        ref (``origin/main``), not that transient branch tip — else it
        under-reports risk (reports ``false`` when the answer is ``true``)."""
        client, app, overrides = webapp_client
        from app.webapp.routers import misc as misc_router
        # Simulate: the live checkout's HEAD (head_sha) is on an unrelated
        # branch that does NOT contain the fix; the resolved deployed ref
        # (origin/main) DOES. A pre-#641 implementation compared against
        # head_sha and would have missed this entirely.
        monkeypatch.setattr(misc_router, "resolve_git_sha", lambda *a, **k: "unrelated")
        monkeypatch.setattr(misc_router, "resolve_deployed_sha", lambda *a, **k: "deployed1")
        monkeypatch.setattr(misc_router, "_session_host_path_relevance", lambda h, d: d == "deployed1")
        overrides["session"].identity.return_value = {
            "git_sha": "stalehost", "started_at": "2026-07-27T08:00:00",
        }
        body = client.get("/api/version").json()
        assert body["head_sha"] == "unrelated"
        assert body["session_host"]["stale"] is True
        assert body["session_host"]["stale_relevant"] is True

    def test_unknown_sha_never_reads_as_stale(self, webapp_client):
        """A failed git lookup on either side must degrade to unknown
        (`stale: None`), never a confident but wrong true/false."""
        client, app, overrides = webapp_client
        overrides["session"].identity.return_value = {
            "git_sha": "unknown", "started_at": "2026-07-27T08:00:00",
        }
        body = client.get("/api/version").json()
        assert body["session_host"]["stale"] is None
        assert body["session_host"]["stale_relevant"] is None


class TestStatus:
    def test_status_returns_expected_shape(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/api/status")
        assert resp.status_code == 200
        body = resp.json()
        # /api/status is a kitchen-sink endpoint — narrow assertion to the
        # keys SPA code actually depends on, so future additions don't
        # false-fail the test.
        assert isinstance(body, dict)
        assert "tunnel_url" in body or "tunnel" in body or "scan_roots" in body or "terminal_reachability" in body


class TestAgents:
    def test_agents_shape(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/api/agents")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body["agents"], list) and body["agents"]
        ids = {a["id"] for a in body["agents"]}
        assert {"claude", "antigravity", "copilot"} <= ids
        for a in body["agents"]:
            assert set(a) == {"id", "label", "available", "fullscreen"}
            assert isinstance(a["available"], bool)
            assert isinstance(a["fullscreen"], bool)


class TestClaudeFlags:
    def test_flags_returns_defaults(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/api/claude-code/flags")
        assert resp.status_code == 200
        body = resp.json()
        assert body["model"] == "opus"
        assert body["effort"] == "high"
        assert body["verbose"] is True
        assert body["debug"] is False
        assert body["permission_mode"] == "auto"
        # The always-on flags are surface-area for the SPA's badge; if the
        # tuple ever changes, this test catches it loudly.
        assert "--remote-control" in body["always_on_flags"]
        # The permission flag is user-selectable now — not always-on.
        assert "--dangerously-skip-permissions" not in body["always_on_flags"]
        # Computed flags are a string; sanity-check that the model/effort
        # and the default (auto) permission mode round-trip through the formatter.
        assert "--model opus" in body["computed_flags"]
        assert "--effort high" in body["computed_flags"]
        assert "--permission-mode auto" in body["computed_flags"]


class TestTerminalThemes:
    """GET /api/terminal-themes — the user theme file (issue #381)."""

    def test_missing_file_returns_empty(self, webapp_client, monkeypatch, tmp_path):
        from app.webapp.routers import misc as misc_router

        monkeypatch.setattr(misc_router, "PROJECT_ROOT", tmp_path)
        client, _, _ = webapp_client
        resp = client.get("/api/terminal-themes")
        assert resp.status_code == 200
        assert resp.json() == {"themes": {}}

    def test_valid_file_is_served(self, webapp_client, monkeypatch, tmp_path):
        from app.webapp.routers import misc as misc_router

        (tmp_path / "webapp").mkdir()
        (tmp_path / "webapp" / "terminal-themes.json").write_text(
            '{"light": {"background": "#fdf6e3", "minimumContrastRatio": 5}}',
            encoding="utf-8",
        )
        monkeypatch.setattr(misc_router, "PROJECT_ROOT", tmp_path)
        client, _, _ = webapp_client
        body = client.get("/api/terminal-themes").json()
        assert body["themes"]["light"]["background"] == "#fdf6e3"
        assert body["themes"]["light"]["minimumContrastRatio"] == 5

    def test_invalid_json_is_ignored(self, webapp_client, monkeypatch, tmp_path):
        from app.webapp.routers import misc as misc_router

        (tmp_path / "webapp").mkdir()
        (tmp_path / "webapp" / "terminal-themes.json").write_text(
            "{not json", encoding="utf-8"
        )
        monkeypatch.setattr(misc_router, "PROJECT_ROOT", tmp_path)
        client, _, _ = webapp_client
        resp = client.get("/api/terminal-themes")
        assert resp.status_code == 200
        assert resp.json() == {"themes": {}}

    def test_non_object_json_is_ignored(self, webapp_client, monkeypatch, tmp_path):
        from app.webapp.routers import misc as misc_router

        (tmp_path / "webapp").mkdir()
        (tmp_path / "webapp" / "terminal-themes.json").write_text(
            '["not", "an", "object"]', encoding="utf-8"
        )
        monkeypatch.setattr(misc_router, "PROJECT_ROOT", tmp_path)
        client, _, _ = webapp_client
        assert client.get("/api/terminal-themes").json() == {"themes": {}}
