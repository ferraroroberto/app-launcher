"""Phone-speed results belong to the Apps tab; nothing in the Board's code may read or draw them."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
_STATIC = PROJECT_ROOT / "app" / "webapp" / "static"

# What only the perf view uses: the ledger reader, its client module, its CSS
# hooks and its route.
_PERF_MARKERS = ("perf_ledger", "apps-perf", "app-perf", "/api/apps/perf")


def _board_sources() -> list[Path]:
    files = [
        _STATIC / "board.js",
        _STATIC / "views" / "board-pane.html",
        _STATIC / "views" / "board-answers-dialog.html",
        _STATIC / "styles" / "board.css",
    ]
    files += sorted((_STATIC / "board").rglob("*.js"))
    files += sorted((PROJECT_ROOT / "app" / "webapp" / "routers").glob("board*.py"))
    files += sorted((PROJECT_ROOT / "src" / "board").rglob("*.py"))
    return files


def test_the_board_sources_are_found() -> None:
    # A moved folder must fail here, not turn the check below into a pass over nothing.
    names = {p.name for p in _board_sources()}
    assert {"board.js", "board-pane.html", "board.css", "board.py"} <= names


def test_no_board_source_mentions_the_perf_view() -> None:
    hits = [
        f"{path.relative_to(PROJECT_ROOT)}: {marker}"
        for path in _board_sources()
        for marker in _PERF_MARKERS
        if marker in path.read_text(encoding="utf-8")
    ]
    assert not hits, hits
