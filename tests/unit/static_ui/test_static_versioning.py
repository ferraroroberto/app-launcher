"""Unit tests for fleet-hash cache-busting, including subdirectory assets.

Covers issue #395: ``_vendored/`` subdirectory assets (CSS hrefs and JS
imports, both ``./``-subdir and ``../``-parent-relative) must get the
``?v=<hash>`` cache-busting query stamped, not just root-level files.
"""

from __future__ import annotations

from pathlib import Path

from src.static_versioning import (
    compute_asset_hashes,
    rewrite_index_html,
    rewrite_js_imports,
)


def _make_static_tree(tmp_path: Path) -> Path:
    static_dir = tmp_path / "static"
    (static_dir / "_vendored" / "nav").mkdir(parents=True)
    (static_dir / "_vendored" / "icons").mkdir(parents=True)
    (static_dir / "_vendored" / "empty-state").mkdir(parents=True)
    (static_dir / "main.js").write_text(
        "import { icon } from './_vendored/icons/icons.js';\n"
        "import { state } from './state.js';\n",
        encoding="utf-8",
    )
    (static_dir / "state.js").write_text("export const state = {};", encoding="utf-8")
    (static_dir / "_vendored" / "nav" / "nav-tabs.css").write_text(
        "nav{}", encoding="utf-8"
    )
    (static_dir / "_vendored" / "icons" / "icons.js").write_text(
        "export const icon = 1;", encoding="utf-8"
    )
    (static_dir / "_vendored" / "empty-state" / "empty-state.js").write_text(
        "import { icon } from '../icons/icons.js';\n", encoding="utf-8"
    )
    return static_dir


def test_rewrite_index_html_stamps_vendored_subdir_css(tmp_path: Path) -> None:
    static_dir = _make_static_tree(tmp_path)
    hashes = compute_asset_hashes(static_dir)
    body = '<link href="/static/_vendored/nav/nav-tabs.css" rel="stylesheet">'
    stamped = rewrite_index_html(body, hashes)
    assert "/static/_vendored/nav/nav-tabs.css?v=" in stamped


def test_rewrite_js_imports_stamps_subdir_import_from_root(tmp_path: Path) -> None:
    static_dir = _make_static_tree(tmp_path)
    hashes = compute_asset_hashes(static_dir)
    body = "import { icon } from './_vendored/icons/icons.js';\n"
    stamped = rewrite_js_imports(body, hashes, from_dir="")
    assert "./_vendored/icons/icons.js?v=" in stamped


def test_rewrite_js_imports_stamps_parent_relative_import(tmp_path: Path) -> None:
    static_dir = _make_static_tree(tmp_path)
    hashes = compute_asset_hashes(static_dir)
    body = "import { icon } from '../icons/icons.js';\n"
    stamped = rewrite_js_imports(body, hashes, from_dir="_vendored/empty-state")
    assert "../icons/icons.js?v=" in stamped


def test_rewrite_js_imports_root_level_import_unchanged_shape(tmp_path: Path) -> None:
    """No regression: a root-level ``./foo.js`` import still stamps."""
    static_dir = _make_static_tree(tmp_path)
    hashes = compute_asset_hashes(static_dir)
    body = "import { state } from './state.js';\n"
    stamped = rewrite_js_imports(body, hashes, from_dir="")
    assert "./state.js?v=" in stamped


def test_compute_asset_hashes_keyed_by_relpath_avoids_basename_collision(
    tmp_path: Path,
) -> None:
    static_dir = tmp_path / "static"
    (static_dir / "a").mkdir(parents=True)
    (static_dir / "b").mkdir(parents=True)
    (static_dir / "a" / "icons.js").write_text("a", encoding="utf-8")
    (static_dir / "b" / "icons.js").write_text("b", encoding="utf-8")
    hashes = compute_asset_hashes(static_dir)
    assert set(hashes) == {"a/icons.js", "b/icons.js"}


def test_vendor_dir_still_unstamped(tmp_path: Path) -> None:
    static_dir = tmp_path / "static"
    (static_dir / "vendor").mkdir(parents=True)
    (static_dir / "vendor" / "chart.js").write_text("chart", encoding="utf-8")
    hashes = compute_asset_hashes(static_dir)
    body = '<script src="/static/vendor/chart.js"></script>'
    assert rewrite_index_html(body, hashes) == body


def test_unknown_import_passes_through_unchanged(tmp_path: Path) -> None:
    static_dir = _make_static_tree(tmp_path)
    hashes = compute_asset_hashes(static_dir)
    body = "import { foo } from './not-on-disk.js';\n"
    assert rewrite_js_imports(body, hashes, from_dir="") == body
