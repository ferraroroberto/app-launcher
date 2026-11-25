"""Every control on Settings > Chief carries a one-line plain explanation.

Each switch, field, model picker and button in the sheet's body names a
``field-help`` line through ``aria-describedby``, so a new chief setting
without one fails here.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

SHEET = Path(__file__).resolve().parents[3] / "app/webapp/static/views/chief-sheet.html"


class _ChiefSheet(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0  # >0 while inside the chief sheet's body
        self.controls: list[tuple[str, str | None]] = []
        self.help_ids: set[str] = set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.depth:
            self.depth += tag not in ("input",)
            if a.get("class") == "field-help" and a.get("id"):
                self.help_ids.add(a["id"])
            is_control = tag == "input" or (
                tag == "button" and a.get("role") != "option"
            )
            if is_control:
                self.controls.append((a.get("data-track") or a.get("id") or tag,
                                      a.get("aria-describedby")))
        elif a.get("class") == "settings-sheet-body" and self._in_chief:
            self.depth = 1
        if tag == "dialog":
            self._in_chief = a.get("id") == "chiefSheet"

    def handle_endtag(self, tag):
        if self.depth:
            self.depth -= 1

    _in_chief = False


def test_every_chief_control_is_explained():
    sheet = _ChiefSheet()
    sheet.feed(SHEET.read_text(encoding="utf-8"))
    assert len(sheet.controls) >= 12, sheet.controls
    unexplained = [name for name, ref in sheet.controls if ref not in sheet.help_ids]
    assert unexplained == []
