"""Row verbs are the vendored plain icon-button, nothing app-local (#1589).

Fleet decision fleet-config#1420: every visible verb on a list row is an
unpainted icon-button (`icon-button action-row-verb`). The app-local
`.row-verb-plain` (#1501) and the outlined `--stop` alias are retired, so a
markup string or a selector that brings either back fails here. The rendered
look (no fill, no border, light and dark) is pinned in the e2e suite.
"""

from __future__ import annotations

from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "app" / "webapp" / "static"
_RETIRED = ("row-verb-plain", "action-row-verb--stop")


def _app_sources() -> list[Path]:
    return [
        p
        for pattern in ("*.js", "*.css", "*.html")
        for p in _STATIC.glob(pattern)
    ]


def test_no_retired_row_verb_class_is_emitted_or_styled() -> None:
    sources = _app_sources()
    assert sources, "no static sources found -- wrong path?"
    hits = [
        f"{p.name}: {name}"
        for p in sources
        for name in _RETIRED
        if name in p.read_text(encoding="utf-8")
    ]
    assert not hits, "retired row-verb class still in use: " + ", ".join(hits)
