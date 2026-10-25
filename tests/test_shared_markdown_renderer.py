"""The markdown renderer is shared, and verified against both consumers (#1006).

`renderMarkdown` used to live in `app/webapp/static/life-os.js` while the
Coding tab's Chat pane (`session-transcript.js`, whose renderer is
`transcript-render.js` since #1629) imported it from there. (The Life OS
content browser that actually calls it moved on again, to
`life-os-content.js`, when #1793 split the module — the import stayed on
`./markdown.js` throughout, which is the property this file pins.)
That was not only a naming problem. `.fleet.toml`'s `lifeos` e2e surface
narrows a `life-os.js`-only diff to the Life OS tests, and those do **not**
include `tests/e2e/test_session_transcript.py` — so a change to the shared
renderer was routed away from its other consumer and never exercised
against it.

These tests pin the fix as a property rather than as a convention: the
renderer is its own module, both tabs import it from there, and that
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


def test_both_tabs_import_it_from_the_shared_module():
    for name in ("life-os/life-os-content.js", "chat/transcript-render.js"):
        source = _read(name)
        assert "from '../markdown.js'" in source, f"{name} must import the shared renderer"
        for feature_module in ("../life-os.js", "./life-os-content.js", "../life-os/life-os-content.js"):
            assert f"renderMarkdown }} from '{feature_module}'" not in source, (
                f"{name} must not reach into a Life OS feature module for it"
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
