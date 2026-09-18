"""The markdown renderer is shared, and verified against both consumers (#1006).

`renderMarkdown` used to live in `app/webapp/static/life-os.js` while the
Coding tab's Chat pane (`session-transcript.js`) imported it from there.
That was not only a naming problem. `.fleet.toml`'s `lifeos` e2e surface
narrows a `life-os.js`-only diff to the Life OS tests, and those do **not**
include `tests/e2e/test_session_transcript.py` — so a change to the shared
renderer was routed away from its other consumer and never exercised
against it.

These tests pin the fix as a property rather than as a convention: the
renderer lives in its own module, both tabs import it from there, and that
module belongs to no single e2e surface, so the router escalates a change to
it to the full suite covering both.
"""

from __future__ import annotations

import pathlib

from scripts.classify_e2e import classify, load_config

STATIC = pathlib.Path(__file__).resolve().parents[1] / "app" / "webapp" / "static"
MARKDOWN_MODULE = "app/webapp/static/markdown.js"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_the_renderer_lives_in_its_own_module():
    source = _read("markdown.js")
    assert "export function renderMarkdown" in source
    assert "function inlineMd" in source


def test_the_feature_module_no_longer_defines_it():
    life_os = _read("life-os.js")
    assert "function renderMarkdown" not in life_os, (
        "the renderer moved to markdown.js; a second copy here would drift"
    )
    assert "function inlineMd" not in life_os


def test_both_tabs_import_it_from_the_shared_module():
    for name in ("life-os.js", "session-transcript.js"):
        source = _read(name)
        assert "from './markdown.js'" in source, f"{name} must import the shared renderer"
        assert "renderMarkdown } from './life-os.js'" not in source, (
            f"{name} must not reach into the Life OS feature module for it"
        )


def test_a_renderer_change_is_verified_against_both_consumers():
    """The routing property the move exists to restore.

    Declaring `markdown.js` inside any one `[[e2e.surface]]` would narrow a
    change to that surface's tests and re-open the blind spot for the other
    consumer, so it deliberately belongs to none and routes to `full`.
    """
    cfg = load_config()
    for surface in cfg.surfaces:
        assert MARKDOWN_MODULE not in surface.paths, (
            f"markdown.js is claimed by the {surface.name!r} surface, which would narrow "
            "verification away from its other consumer"
        )
    result = classify([MARKDOWN_MODULE], cfg)
    assert result.tier == "full", (
        f"a renderer-only change must run the full suite, got {result.tier!r} "
        f"({result.surface!r})"
    )
