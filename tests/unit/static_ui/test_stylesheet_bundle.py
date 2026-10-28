"""The stylesheet bundle keeps the cascade the 31 separate links gave."""

from __future__ import annotations

import re
from pathlib import Path

from src.static_versioning import compute_asset_hashes
from src.stylesheet_bundle import build_bundle, link_bundle, linked_stylesheets
from tests._css import STATIC

_INDEX = (
    "<head>\n"
    '  <link rel="stylesheet" href="/static/a.css?v=12345678">\n'
    "  <!-- note -->\n"
    '  <link rel="stylesheet" href="/static/sub/b.css">\n'
    '  <link rel="icon" href="/static/icon.png">\n'
    "</head>\n"
)


def _tree(tmp_path: Path, index: str = _INDEX) -> Path:
    static = tmp_path / "static"
    (static / "sub").mkdir(parents=True)
    (static / "index.html").write_text(index, encoding="utf-8")
    (static / "a.css").write_text("a{color:red}", encoding="utf-8")
    (static / "sub" / "b.css").write_text("b{color:blue}\n", encoding="utf-8")
    return static


def test_link_bundle_swaps_the_sheets_for_one_link() -> None:
    out = link_bundle(_INDEX)
    assert out == (
        "<head>\n"
        '  <link rel="stylesheet" href="/static/bundle.css">\n'
        "  <!-- note -->\n"
        '  <link rel="icon" href="/static/icon.png">\n'
        "</head>\n"
    )


def test_link_bundle_leaves_a_shell_without_sheets_alone() -> None:
    assert link_bundle("<head></head>") == "<head></head>"


def test_build_bundle_concatenates_in_link_order(tmp_path: Path) -> None:
    bundle = build_bundle(_tree(tmp_path))
    assert bundle == "/* a.css */\na{color:red}\n/* sub/b.css */\nb{color:blue}\n"


def test_build_bundle_is_none_without_links(tmp_path: Path) -> None:
    assert build_bundle(_tree(tmp_path, "<head></head>")) is None


def test_reordering_the_links_moves_the_fleet_hash(tmp_path: Path) -> None:
    static = _tree(tmp_path)
    before = compute_asset_hashes(static)["a.css"]
    swapped = (
        '  <link rel="stylesheet" href="/static/sub/b.css">\n'
        '  <link rel="stylesheet" href="/static/a.css">\n'
    )
    (static / "index.html").write_text(swapped, encoding="utf-8")
    assert compute_asset_hashes(static)["a.css"] != before


def test_the_apps_sheets_survive_concatenation() -> None:
    """A rule that only works at the top of a sheet or against its own path would break."""
    names = linked_stylesheets((STATIC / "index.html").read_text(encoding="utf-8"))
    assert len(names) > 1
    for name in names:
        text = (STATIC / name).read_text(encoding="utf-8")
        assert not re.search(r"@import|@charset", text), name
        # `url(data:...)` is self-contained; anything else resolves against the file's own path.
        assert not re.search(r"url\(\s*(?!['\"]?(?:data:|#))", text), name
