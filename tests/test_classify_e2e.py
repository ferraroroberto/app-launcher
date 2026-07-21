"""Unit tests for the diff-proportionate e2e router (issue #568).

Pure path->tier classification; no git, no browser. Also pins the concrete
#565 incident (a vendored SVG sprite + a new pure-Python unit test) that
motivated the issue: it must route to the fast ``static`` / Chromium-only tier.
"""

from __future__ import annotations

import pytest

from scripts.classify_e2e import Category, _classify_one, classify


# ------------------------------------------------------- per-file categories
@pytest.mark.parametrize(
    "path,expected",
    [
        # static assets under the webapp static dir
        ("app/webapp/static/_vendored/icons/icons-sprite.html", Category.STATIC),
        ("app/webapp/static/icon-512.png", Category.STATIC),
        ("app/webapp/static/favicon.ico", Category.STATIC),
        ("app/webapp/static/manifest.webmanifest", Category.STATIC),
        ("app/webapp/static/icons/foo.svg", Category.STATIC),
        # real browser surface -> FULL
        ("app/webapp/static/apps.js", Category.FULL),
        ("app/webapp/static/styles.css", Category.FULL),
        ("app/webapp/static/index.html", Category.FULL),          # real page, not vendored
        ("app/webapp/static/_vendored/nav/nav.css", Category.FULL),  # vendored CSS is layout
        ("app/webapp/server.py", Category.FULL),
        ("app/webapp/routers/board.py", Category.FULL),
        ("app/session_host/server.py", Category.FULL),
        ("src/session_host.py", Category.FULL),
        ("src/session_client.py", Category.FULL),
        ("src/launcher.py", Category.FULL),
        ("launcher.py", Category.FULL),
        ("tests/e2e/test_board_tab.py", Category.FULL),
        ("tests/conftest.py", Category.FULL),
        # no browser impact -> NONE
        ("src/board.py", Category.NONE),
        ("src/jobs.py", Category.NONE),
        ("tests/test_classify_e2e.py", Category.NONE),
        ("tests/test_icon_sprite_coverage.py", Category.NONE),
        ("docs/architecture.mmd", Category.NONE),
        ("README.md", Category.NONE),
        ("scripts/verify-before-ship.ps1", Category.NONE),
        ("config/apps.sample.json", Category.NONE),
        (".github/workflows/e2e.yml", Category.NONE),
        (".fleet.toml", Category.NONE),
        ("tray.bat", Category.NONE),
        # unrecognized -> fail-safe FULL
        ("some/weird/new_dir/thing.xyz", Category.FULL),
        ("app/cli/commands/launch.py", Category.FULL),  # app/** off static -> full (safe)
    ],
)
def test_classify_one(path: str, expected: Category) -> None:
    cat, _label = _classify_one(path)
    assert cat is expected, f"{path} -> {cat.name}, expected {expected.name}"


# --------------------------------------------------------------- tier routing
def test_static_only_routes_to_chromium_smoke() -> None:
    """The #565 diff: vendored sprite + one pure-Python unit test."""
    r = classify([
        "app/webapp/static/_vendored/icons/icons-sprite.html",
        "tests/test_icon_sprite_coverage.py",
    ])
    assert r.tier == "static"
    assert r.browsers == ["chromium"]
    assert r.pytest_target == "tests/e2e/test_smoke.py"
    assert r.reasons  # non-empty: names the triggering path


def test_js_change_routes_to_full() -> None:
    r = classify(["app/webapp/static/apps.js"])
    assert r.tier == "full"
    assert r.browsers == []          # suite default = both projections
    assert r.pytest_target == "tests/e2e"


def test_css_change_routes_to_full() -> None:
    r = classify(["app/webapp/static/styles.css"])
    assert r.tier == "full"


def test_mixed_static_and_js_routes_to_full() -> None:
    """Fail-safe: a static asset AND a .js file -> full suite, not narrow."""
    r = classify([
        "app/webapp/static/_vendored/icons/icons-sprite.html",
        "app/webapp/static/apps.js",
    ])
    assert r.tier == "full"


def test_backend_python_only_skips_browser() -> None:
    r = classify(["src/board.py", "tests/test_board.py"])
    assert r.tier == "skip"
    assert r.pytest_target == ""


def test_docs_only_skips_browser() -> None:
    r = classify(["README.md", "docs/architecture.mmd"])
    assert r.tier == "skip"


def test_unclassified_is_full() -> None:
    r = classify(["random/thing.xyz"])
    assert r.tier == "full"


def test_empty_diff_is_full() -> None:
    """No changed files -> can't prove narrow -> fail-safe full."""
    r = classify([])
    assert r.tier == "full"
    assert r.reasons


def test_backslash_paths_are_normalized() -> None:
    r = classify(["app\\webapp\\static\\icon-512.png"])
    assert r.tier == "static"


def test_session_host_python_forces_full() -> None:
    """A backend .py *on* the session-host path still gets full coverage."""
    r = classify(["src/session_host.py", "src/board.py"])
    assert r.tier == "full"
