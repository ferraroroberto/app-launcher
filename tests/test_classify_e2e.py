"""Unit tests for the diff-proportionate e2e router (issues #568, #955).

The mechanism is project-scaffolding's `scripts/classify_e2e.py`, copied
byte-verbatim; its own suite proves the mechanism. These tests pin *this*
repo's declaration: they load the real `.fleet.toml` `[e2e]` table and assert
that representative paths land in the tier their rule intends, so an edit that
silently under-routes a real browser surface fails here. Pure path->tier
classification; no git, no browser. Also pins the concrete #565 incident (a
vendored SVG sprite + a new pure-Python unit test) that motivated routing: it
must route to the fast ``static`` / Chromium-only tier.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from scripts.classify_e2e import Category, E2EConfig, _classify_one, classify, load_config
from src.session_host_paths import declared_session_host_paths

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
REAL_FLEET_TOML = REPO_ROOT / ".fleet.toml"


@pytest.fixture(scope="module")
def cfg() -> E2EConfig:
    return load_config(REAL_FLEET_TOML)


def test_real_fleet_toml_declares_usable_e2e_table(cfg: E2EConfig) -> None:
    """A table the classifier can't use would silently fail-safe every diff to full."""
    assert cfg.source == "declared"
    assert cfg.static_pytest_target == "tests/e2e/test_smoke.py"
    assert cfg.static_browsers == ("chromium",)
    assert cfg.full_pytest_target == "tests/e2e"


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
        ("src/session_host_pty.py", Category.FULL),  # future split, not on disk yet
        ("src/vt_snapshot.py", Category.FULL),  # declared session-host path (#881)
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
def test_classify_one(cfg: E2EConfig, path: str, expected: Category) -> None:
    cat, _label = _classify_one(path, cfg.rules)
    assert cat is expected, f"{path} -> {cat.name}, expected {expected.name}"


# --------------------------------------------------------------- tier routing
def test_static_only_routes_to_chromium_smoke(cfg: E2EConfig) -> None:
    """The #565 diff: vendored sprite + one pure-Python unit test."""
    r = classify([
        "app/webapp/static/_vendored/icons/icons-sprite.html",
        "tests/test_icon_sprite_coverage.py",
    ], cfg)
    assert r.tier == "static"
    assert r.browsers == ["chromium"]
    assert r.pytest_target == "tests/e2e/test_smoke.py"
    assert r.reasons  # non-empty: names the triggering path


def test_js_change_routes_to_full(cfg: E2EConfig) -> None:
    r = classify(["app/webapp/static/apps.js"], cfg)
    assert r.tier == "full"
    assert r.browsers == []          # suite default = both projections
    assert r.pytest_target == "tests/e2e"


def test_css_change_routes_to_full(cfg: E2EConfig) -> None:
    r = classify(["app/webapp/static/styles.css"], cfg)
    assert r.tier == "full"


def test_mixed_static_and_js_routes_to_full(cfg: E2EConfig) -> None:
    """Fail-safe: a static asset AND a .js file -> full suite, not narrow."""
    r = classify([
        "app/webapp/static/_vendored/icons/icons-sprite.html",
        "app/webapp/static/apps.js",
    ], cfg)
    assert r.tier == "full"


def test_backend_python_only_skips_browser(cfg: E2EConfig) -> None:
    r = classify(["src/board.py", "tests/test_board.py"], cfg)
    assert r.tier == "skip"
    assert r.pytest_target == ""


def test_docs_only_skips_browser(cfg: E2EConfig) -> None:
    r = classify(["README.md", "docs/architecture.mmd"], cfg)
    assert r.tier == "skip"


def test_backslash_paths_are_normalized(cfg: E2EConfig) -> None:
    r = classify(["app\\webapp\\static\\icon-512.png"], cfg)
    assert r.tier == "static"


def test_session_host_python_forces_full(cfg: E2EConfig) -> None:
    """A backend .py *on* the session-host path still gets full coverage."""
    r = classify(["src/session_host.py", "src/board.py"], cfg)
    assert r.tier == "full"


def test_session_host_reason_keeps_the_label_the_gate_warns_on(cfg: E2EConfig) -> None:
    """verify-before-ship.ps1 prints its "not live after tray.bat --restart"
    warning when E2E_REASON matches "session-host" (#615), so the rules' labels
    are part of that contract, not decoration."""
    for path in ("src/session_host.py", "app/session_host/server.py", "src/audit.py", "launcher.py"):
        r = classify([path], cfg)
        assert any("session-host" in reason for reason in r.reasons), (path, r.reasons)


# ------------------------------------------------------------------ fail-safe
def test_unclassified_is_full(cfg: E2EConfig) -> None:
    r = classify(["random/thing.xyz"], cfg)
    assert r.tier == "full"
    assert r.pytest_target == "tests/e2e"


def test_empty_diff_is_full(cfg: E2EConfig) -> None:
    """No changed files -> can't prove narrow -> fail-safe full."""
    r = classify([], cfg)
    assert r.tier == "full"
    assert r.pytest_target == "tests/e2e"
    assert r.reasons


@pytest.mark.parametrize(
    "toml",
    [
        "layer = \"enabling\"\n",                   # no [e2e] table
        "[e2e]\nfull_pytest_target = \"tests/e2e\"\n",  # table, no usable rule
        "[e2e\nthis is not toml",                    # unparsable
    ],
    ids=["missing", "empty", "invalid"],
)
def test_unusable_declaration_routes_every_diff_full(tmp_path: pathlib.Path, toml: str) -> None:
    """A broken `.fleet.toml` must widen routing, never narrow it: even a
    docs-only diff that would normally skip runs the whole suite."""
    fleet_toml = tmp_path / ".fleet.toml"
    fleet_toml.write_text(toml, encoding="utf-8")
    r = classify(["README.md"], load_config(fleet_toml))
    assert r.tier == "full"
    assert r.pytest_target == "tests/e2e"


# --------------------------------------------------------- real-tree drift guard
def test_real_session_host_files_route_full(cfg: E2EConfig) -> None:
    """Every real session-host file on disk must classify FULL: each
    `src/session_host*.py`, plus every path CLAUDE.md's `## session-host`
    block declares (a declared directory contributes every file under it).

    Guards against layout drift: a new session-host module added on disk, or
    declared in CLAUDE.md, without `.fleet.toml` being taught about it would
    silently narrow e2e coverage while every hand-written test above stays
    green. The glob alone missed `src/vt_snapshot.py` — declared, but not
    `session_host*`-named — which routed to no browser suite at all (#881).
    Walking the declaration too means the list CLAUDE.md already maintains
    for `stale_relevant` (#635) is the list routing is held to.
    """
    declared = declared_session_host_paths(REPO_ROOT / "CLAUDE.md")
    assert declared, "expected CLAUDE.md to declare the session-host paths"

    files = set((REPO_ROOT / "src").glob("session_host*.py"))
    for path in declared:
        target = REPO_ROOT / path
        if path.endswith("/"):
            files.update(f for f in target.rglob("*") if f.is_file())
        else:
            assert target.is_file(), f"{path} is declared in CLAUDE.md but missing on disk"
            files.add(target)
    assert REPO_ROOT / "src" / "session_host.py" in files

    for f in sorted(files):
        rel = f.relative_to(REPO_ROOT).as_posix()
        cat, _label = _classify_one(rel, cfg.rules)
        assert cat is Category.FULL, f"{rel} -> {cat.name}, expected FULL (layout drift?)"


def test_real_static_asset_routes_static(cfg: E2EConfig) -> None:
    """Sanity: a real image under app/webapp/static/ still classifies STATIC."""
    real_png = REPO_ROOT / "app/webapp/static/icon-512.png"
    assert real_png.is_file(), "fixture file moved/renamed; update this test"
    cat, _label = _classify_one("app/webapp/static/icon-512.png", cfg.rules)
    assert cat is Category.STATIC


def test_real_webapp_js_routes_full(cfg: E2EConfig) -> None:
    """Sanity: a real app/webapp/static/*.js file still classifies FULL."""
    real_js = REPO_ROOT / "app/webapp/static/apps.js"
    assert real_js.is_file(), "fixture file moved/renamed; update this test"
    cat, _label = _classify_one("app/webapp/static/apps.js", cfg.rules)
    assert cat is Category.FULL


# ------------------------------------------------------------------- surfaces
# #955: `.fleet.toml` declares [[e2e.surface]] entries that narrow a would-be
# full diff to one tab's tests. Each surface is pinned with a path that narrows
# and shared paths that must keep the whole suite.
_BOARD_TARGETS = (
    "tests/e2e/test_board_tab.py tests/e2e/test_board_chief.py tests/e2e/test_coding_chief.py "
    "tests/e2e/test_session_rename.py tests/e2e/test_shared_session_title.py "
    "tests/e2e/test_coding_model_selector.py tests/e2e/test_terminal_bar_overflow.py "
    "tests/e2e/test_primary_nav.py tests/e2e/test_bottom_tab_bar.py tests/e2e/test_smoke.py"
)
_LIFEOS_TARGETS = (
    "tests/e2e/test_life_os_tab.py tests/e2e/test_markdown_link_rendering.py "
    "tests/e2e/test_collapsible_other_tabs.py tests/e2e/test_coding_model_selector.py "
    "tests/e2e/test_terminal_bar_overflow.py tests/e2e/test_primary_nav.py "
    "tests/e2e/test_bottom_tab_bar.py tests/e2e/test_smoke.py"
)

# What an e2e test says when it exercises a surface's modules: the module file,
# its API prefix, its tab/pane ids, or its DOM id/class prefix. Any test module
# matching must be in that surface's targets, or a surface edit would narrow
# past a test it can break.
_SURFACE_MARKERS = {
    "board": re.compile(r"(?<![A-Za-z])board[A-Z-]|board(-dispatch)?\.js|/api/board|tabBoard|paneBoard"),
    "lifeos": re.compile(r"(?<![A-Za-z])(lifeOs|lifeos-)|life-os\.js|/api/life-os|tabLifeOS|paneLifeOS"),
}


def _route(cfg: E2EConfig, *paths: str) -> tuple[str, str]:
    r = classify(list(paths), cfg)
    return r.tier, r.pytest_target


def test_real_surfaces_are_usable(cfg: E2EConfig) -> None:
    """One malformed entry disables every surface silently (only a note in E2E_REASON)."""
    assert cfg.surfaces_note == "", cfg.surfaces_note
    assert {s.name for s in cfg.surfaces} == set(_SURFACE_MARKERS)


@pytest.mark.parametrize(
    "path,surface,targets",
    [
        ("app/webapp/static/board.js", "board", _BOARD_TARGETS),
        ("app/webapp/static/board-dispatch.js", "board", _BOARD_TARGETS),
        ("app/webapp/routers/board_chief.py", "board", _BOARD_TARGETS),
        ("tests/e2e/test_board_tab.py", "board", _BOARD_TARGETS),
        ("app/webapp/static/life-os.js", "lifeos", _LIFEOS_TARGETS),
        ("app/webapp/routers/life_os_files.py", "lifeos", _LIFEOS_TARGETS),
        ("tests/e2e/test_life_os_tab.py", "lifeos", _LIFEOS_TARGETS),
    ],
)
def test_surface_path_narrows_to_its_surface(cfg: E2EConfig, path: str, surface: str, targets: str) -> None:
    r = classify([path], cfg)
    assert (r.tier, r.surface, r.pytest_target) == ("surface", surface, targets)
    assert r.browsers == []  # both projections, same as full


def test_surface_diff_with_backend_python_still_narrows(cfg: E2EConfig) -> None:
    """`none` paths (backend src/, unit tests, docs) ride along without widening."""
    assert _route(cfg, "app/webapp/static/board.js", "src/board.py",
                  "tests/test_board.py", "README.md") == ("surface", _BOARD_TARGETS)


@pytest.mark.parametrize(
    "shared",
    [
        "app/webapp/static/styles.css",
        "app/webapp/static/index.html",
        "app/webapp/static/main.js",
        "app/webapp/static/state.js",
        "app/webapp/static/claude-options.js",
        "app/webapp/server.py",
        "app/webapp/routers/_helpers.py",
        "src/session_host.py",
        "src/launcher.py",
        "app/session_host/server.py",
        "tests/e2e/conftest.py",
        "tests/conftest.py",
        "app/webapp/static/icon-512.png",   # static, owned by no surface
    ],
)
def test_shared_path_keeps_the_whole_suite(cfg: E2EConfig, shared: str) -> None:
    """Shared CSS/JS, the session host and the conftests belong to no surface:
    alone or riding along with a surface change, they run everything."""
    assert classify([shared], cfg).tier != "surface"
    assert _route(cfg, "app/webapp/static/board.js", shared) == ("full", "tests/e2e")


def test_session_host_declared_paths_are_in_no_surface(cfg: E2EConfig) -> None:
    declared = declared_session_host_paths(REPO_ROOT / "CLAUDE.md")
    for surface in cfg.surfaces:
        for path in declared:
            assert not surface.matches(path), (surface.name, path)
            assert not any(p.startswith(path) for p in surface.paths + surface.prefixes), (surface.name, path)


def test_multi_surface_diff_keeps_the_whole_suite(cfg: E2EConfig) -> None:
    assert _route(cfg, "app/webapp/static/board.js",
                  "app/webapp/static/life-os.js") == ("full", "tests/e2e")


def test_unclassified_path_keeps_the_whole_suite(cfg: E2EConfig) -> None:
    assert _route(cfg, "app/webapp/static/board.js", "random/thing.xyz") == ("full", "tests/e2e")


def test_editing_the_routing_map_keeps_the_whole_suite(cfg: E2EConfig) -> None:
    """A diff that edits `.fleet.toml` (the surface map) is never judged by it."""
    assert _route(cfg, "app/webapp/static/board.js", ".fleet.toml") == ("full", "tests/e2e")


def test_every_test_exercising_a_surface_is_in_its_targets(cfg: E2EConfig) -> None:
    suite = REPO_ROOT / "tests" / "e2e"
    by_name = {s.name: s for s in cfg.surfaces}
    for name, marker in _SURFACE_MARKERS.items():
        users = {
            f"tests/e2e/{f.name}"
            for f in suite.glob("test_*.py")
            if marker.search(f.read_text(encoding="utf-8"))
        }
        assert users, f"marker for {name} matches no test; stale marker?"
        missing = users - set(by_name[name].pytest_targets)
        assert not missing, f"{sorted(missing)} exercise surface {name!r} but are not in its pytest_targets"
