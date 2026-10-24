"""Every sheet's primary has an accessible name in the markup (#1501, A11Y-01).

``#jobSheetRun`` and ``#confirmDialogOk`` used to be empty in ``index.html``
and named only when script ran, so the design review read an unnamed primary
in the empty job sheet and the confirm dialog. Script may still rewrite a
label; the markup must never leave one blank. The check parses the page with
the stdlib parser, so it runs in the non-browser suite.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from src.shell_html import assemble_shell

_INDEX = Path(__file__).resolve().parents[1] / "app" / "webapp" / "static" / "index.html"


class _SaveButtons(HTMLParser):
    """Collects each ``.detail-save-btn`` button's visible text and aria-label."""

    def __init__(self) -> None:
        super().__init__()
        self.buttons: list[dict] = []
        self._open: dict | None = None
        self._svg_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = dict(attrs)
        if tag == "button" and "detail-save-btn" in (a.get("class") or "").split():
            self._open = {"id": a.get("id") or "", "aria": a.get("aria-label") or "", "text": ""}
        elif tag == "svg" and self._open is not None:
            self._svg_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "svg" and self._svg_depth:
            self._svg_depth -= 1
        elif tag == "button" and self._open is not None:
            self.buttons.append(self._open)
            self._open = None

    def handle_data(self, data: str) -> None:
        if self._open is not None and not self._svg_depth:
            self._open["text"] += data


def _buttons() -> list[dict]:
    parser = _SaveButtons()
    parser.feed(assemble_shell(_INDEX))
    return parser.buttons


def test_no_detail_save_btn_is_empty() -> None:
    buttons = _buttons()
    assert len(buttons) > 10, f"found only {len(buttons)} sheet primaries; wrong page?"
    empty = [b["id"] or "(no id)" for b in buttons if not (b["text"].strip() or b["aria"].strip())]
    assert not empty, f"sheet primaries with no name in the markup: {empty}"


def test_the_two_once_unnamed_primaries_have_defaults() -> None:
    by_id = {b["id"]: b for b in _buttons() if b["id"]}
    assert by_id["jobSheetRun"]["text"].strip() == "Run now"
    assert by_id["confirmDialogOk"]["text"].strip() == "Confirm"
