"""The served page is index.html with each view's fragment spliced in.

The shell keeps the head, sprite, nav and mount points; every pane and dialog
lives under ``static/views/``. These pin that split and the assembler that
serves it, in the non-browser suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.webapp.shell_html import assemble_shell, shell_stat_key

_STATIC = Path(__file__).resolve().parents[3] / "app" / "webapp" / "static"
_INDEX = _STATIC / "index.html"


def _page(tmp_path: Path, shell: str, fragments: dict) -> Path:
    (tmp_path / "views").mkdir()
    for name, body in fragments.items():
        (tmp_path / "views" / name).write_text(body, encoding="utf-8", newline="\n")
    index = tmp_path / "index.html"
    index.write_text(shell, encoding="utf-8", newline="\n")
    return index


def test_shell_holds_no_view_markup() -> None:
    shell = _INDEX.read_text(encoding="utf-8")
    assert len(shell.splitlines()) < 1000
    for view_markup in ('id="pane', "<dialog", 'id="terminalOverlay"'):
        assert view_markup not in shell, f"{view_markup} belongs in views/"


def test_every_marker_resolves_and_none_survives_assembly() -> None:
    shell = _INDEX.read_text(encoding="utf-8")
    fragments = {p.name for p in (_STATIC / "views").glob("*.html")}
    marked = {m.split("views/")[1].split(" ")[0] for m in shell.split("@include ")[1:]}
    assert marked == fragments, "every fragment is included exactly where index.html says"
    assert "@include" not in assemble_shell(_INDEX)


def test_marker_line_is_replaced_by_the_fragment_verbatim(tmp_path: Path) -> None:
    index = _page(
        tmp_path,
        "<main>\n    <!-- @include views/a.html -->\n\n  <p>x</p>\n</main>\n",
        {"a.html": "    <section>\n      a\n    </section>\n"},
    )
    assert assemble_shell(index) == "<main>\n    <section>\n      a\n    </section>\n\n  <p>x</p>\n</main>\n"


def test_an_include_outside_views_is_refused(tmp_path: Path) -> None:
    index = _page(tmp_path, "<!-- @include ../secret.html -->\n", {})
    (tmp_path / "secret.html").write_text("nope", encoding="utf-8")
    with pytest.raises(ValueError):
        assemble_shell(index)


def test_the_cache_key_moves_when_a_fragment_changes(tmp_path: Path) -> None:
    index = _page(tmp_path, "<!-- @include views/a.html -->\n", {"a.html": "one\n"})
    before = shell_stat_key(index)
    (tmp_path / "views" / "a.html").write_text("one two\n", encoding="utf-8")
    assert shell_stat_key(index) != before
