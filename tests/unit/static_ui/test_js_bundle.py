"""The ES-module bundle: built once per source state, every served URL stamped."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.webapp import server as server_mod
from src.static_bundles import js_bundle
from src.static_bundles.js_bundle import (
    BundleError,
    build_js_bundle,
    bundle_key,
    link_js_bundle,
)
from src.static_versioning import compute_asset_hashes, fleet_hash_of

_FLEET = "0123abcd"


def _tree(tmp_path: Path) -> Path:
    static = tmp_path / "static"
    (static / "views").mkdir(parents=True)
    (static / "main.js").write_text(
        "import { a } from './a.js';\nimport('./views/lazy.js').then((m) => m.run(a));\n",
        encoding="utf-8",
    )
    (static / "a.js").write_text("export const a = import.meta.url;\n", encoding="utf-8")
    (static / "views" / "lazy.js").write_text(
        "import { a } from '../a.js';\nexport function run(x) { return [a, x]; }\n", encoding="utf-8"
    )
    return static


def _fake_esbuild(calls: list):
    """Writes what esbuild writes for ``_tree``: an entry, a shared chunk, a lazy chunk, a map."""

    def run(static_dir: Path, out_dir: Path) -> None:
        calls.append(out_dir)
        out_dir.mkdir(parents=True)
        (out_dir / "main.js").write_text(
            'import {\n  a\n} from "./chunk-AAAA.js";\nimport("./lazy-BBBB.js");\n'
            "//# sourceMappingURL=main.js.map\n",
            encoding="utf-8",
        )
        (out_dir / "chunk-AAAA.js").write_text("var a = import.meta.url;\nexport { a };\n", encoding="utf-8")
        (out_dir / "lazy-BBBB.js").write_text('import "./chunk-AAAA.js";\n', encoding="utf-8")
        rel = Path("..") / ".." / static_dir.name / "main.js"
        (out_dir / "main.js.map").write_text(
            json.dumps({"version": 3, "sources": [rel.as_posix()], "mappings": ""}), encoding="utf-8"
        )

    return run


def test_link_js_bundle_points_the_entry_script_at_the_bundle() -> None:
    shell = (
        '<script src="/static/vendor/x.js"></script>\n'
        '<script type="module" src="/static/main.js?v=12345678"></script>\n'
    )
    assert link_js_bundle(shell) == (
        '<script src="/static/vendor/x.js"></script>\n'
        '<script type="module" src="/static/js-bundle/main.js"></script>\n'
    )
    assert link_js_bundle('<script type="module" src="/static/main.js"></script>') == (
        '<script type="module" src="/static/js-bundle/main.js"></script>'
    )


def test_a_changed_module_busts_the_cache(tmp_path: Path) -> None:
    """Any module edit moves the fleet hash, so the bundle's URL stamp and its cache key both move."""
    static = _tree(tmp_path)
    before = fleet_hash_of(compute_asset_hashes(static))
    (static / "views" / "lazy.js").write_text("export function run() { return 1; }\n", encoding="utf-8")
    after = fleet_hash_of(compute_asset_hashes(static))
    assert after != before
    assert bundle_key(after) != bundle_key(before)


def test_every_chunk_import_carries_the_stamp(tmp_path: Path) -> None:
    served = build_js_bundle(_tree(tmp_path), tmp_path / "cache", _FLEET, run=_fake_esbuild([]))
    assert served is not None
    main = served["main.js"][0].decode()
    assert f'from "./chunk-AAAA.js?v={_FLEET}"' in main
    assert f'import("./lazy-BBBB.js?v={_FLEET}")' in main
    # A bare import is a load too: unstamped, the chunk would load twice under two URLs.
    assert served["lazy-BBBB.js"][0].decode() == f'import "./chunk-AAAA.js?v={_FLEET}";\n'
    assert served["main.js"][1] == "text/javascript"


def test_the_source_map_names_the_served_sources(tmp_path: Path) -> None:
    served = build_js_bundle(_tree(tmp_path), tmp_path / "cache", _FLEET, run=_fake_esbuild([]))
    body, media_type = served["main.js.map"]
    assert media_type == "application/json"
    assert json.loads(body)["sources"] == ["/static/main.js"]


def test_a_restart_on_the_same_sources_reads_the_cache(tmp_path: Path) -> None:
    static, cache, calls = _tree(tmp_path), tmp_path / "cache", []
    first = build_js_bundle(static, cache, _FLEET, run=_fake_esbuild(calls))
    assert build_js_bundle(static, cache, _FLEET, run=_fake_esbuild(calls)) == first
    assert len(calls) == 1
    build_js_bundle(static, cache, "feedf00d", run=_fake_esbuild(calls))
    assert len(calls) == 2
    assert sorted(p.name for p in cache.iterdir()) == [bundle_key("feedf00d")]


def test_a_failed_build_leaves_no_cache_behind(tmp_path: Path) -> None:
    def broken(static_dir: Path, out_dir: Path) -> None:
        out_dir.mkdir(parents=True)
        raise BundleError("esbuild exited 1")

    cache = tmp_path / "cache"
    with pytest.raises(BundleError):
        build_js_bundle(_tree(tmp_path), cache, _FLEET, run=broken)
    assert list(cache.iterdir()) == []


def test_no_entry_means_no_bundle(tmp_path: Path) -> None:
    assert build_js_bundle(tmp_path, tmp_path / "cache", _FLEET, run=_fake_esbuild([])) is None


def test_the_pinned_esbuild_bundles_a_lazy_view_as_its_own_chunk(tmp_path: Path) -> None:
    """The real bundler, with the server's flags: an entry, a lazy chunk, maps, nothing unstamped."""
    served = build_js_bundle(_tree(tmp_path), tmp_path / "cache", _FLEET)
    assert served is not None
    names = sorted(served)
    assert "main.js" in names and "main.js.map" in names
    assert any(n.startswith("lazy-") and n.endswith(".js") for n in names), names
    for name, (body, _) in served.items():
        if name.endswith(".js"):
            for other in names:
                assert f'"./{other}"' not in body.decode(), f"{name} imports {other} unstamped"


def _start(monkeypatch: pytest.MonkeyPatch, build) -> tuple:
    monkeypatch.setattr(server_mod, "build_js_bundle", build)
    app = FastAPI()
    app.state.asset_fleet_hash = _FLEET
    hashes = {"main.js": _FLEET}
    thread = server_mod._start_js_bundle_build(app, hashes)
    return app, hashes, thread


def test_the_server_serves_the_bundle_and_404s_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = {"main.js": (b"export {};\n", "text/javascript")}
    app, hashes, thread = _start(monkeypatch, lambda *args: bundle)
    thread.join(timeout=10)
    assert app.state.js_bundle is bundle
    assert hashes[f"{js_bundle.BUNDLE_DIR}/main.js"] == _FLEET
    client = TestClient(app)
    resp = client.get(f"/static/{js_bundle.BUNDLE_DIR}/main.js")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/javascript")
    assert "immutable" in resp.headers["cache-control"]
    assert client.get(f"/static/{js_bundle.BUNDLE_DIR}/nope.js").status_code == 404


def test_a_failed_build_falls_back_to_the_separate_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args):
        raise BundleError("npx not found on PATH")

    app, hashes, thread = _start(monkeypatch, fail)
    thread.join(timeout=10)
    assert app.state.js_bundle is None
    assert list(hashes) == ["main.js"]
    assert TestClient(app).get(f"/static/{js_bundle.BUNDLE_DIR}/main.js").status_code == 404


def test_a_crashing_build_is_logged_and_leaves_the_modules_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    def crash(*args):
        raise RuntimeError("boom")

    app, hashes, thread = _start(monkeypatch, crash)
    thread.join(timeout=10)
    assert app.state.js_bundle is None
    assert list(hashes) == ["main.js"]


def test_the_app_answers_while_a_slow_bundle_build_is_still_running(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    bundle = {"main.js": (b"export {};\n", "text/javascript")}

    def slow(*args):
        assert release.wait(timeout=30), "the build was never released"
        return bundle

    app, hashes, thread = _start(monkeypatch, slow)
    try:
        assert thread.is_alive()
        client = TestClient(app)
        # A retryable 503 meanwhile, and no page links the bundle yet.
        pending = client.get(f"/static/{js_bundle.BUNDLE_DIR}/main.js")
        assert pending.status_code == 503
        assert pending.headers["cache-control"] == "no-store"
        assert app.state.js_bundle is None
        assert f"{js_bundle.BUNDLE_DIR}/main.js" not in hashes
    finally:
        release.set()
    thread.join(timeout=10)
    assert app.state.js_bundle is bundle
    assert TestClient(app).get(f"/static/{js_bundle.BUNDLE_DIR}/main.js").status_code == 200
