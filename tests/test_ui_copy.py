"""User-language copy in index.html (#1176).

The design review's judge (J-05, J-07) asked for labels a user would say and
toolbars a newcomer can read. These pin the decisions structurally: every
Settings field carries one help line, dialog field labels hold no internal
identifiers, and the toolbar toggles whose glyph is not a convention carry a
visible word. Parsed from the markup, so no browser is needed.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

_INDEX = Path(__file__).resolve().parents[1] / "app" / "webapp" / "static" / "index.html"


class _Tree(HTMLParser):
    """A flat element list with each element's own text and descendants' text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[dict] = []
        self._stack: list[dict] = []

    def handle_starttag(self, tag, attrs):
        el = {"tag": tag, "attrs": dict(attrs), "text": "", "parents": list(self._stack)}
        self.elements.append(el)
        if tag not in ("input", "br", "img", "meta", "link", "use", "path"):
            self._stack.append(el)

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i]["tag"] == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        for el in self._stack:
            el["text"] += data


def _tree() -> _Tree:
    tree = _Tree()
    tree.feed(_INDEX.read_text(encoding="utf-8"))
    return tree


def _by_id(tree: _Tree, el_id: str) -> dict:
    found = [e for e in tree.elements if e["attrs"].get("id") == el_id]
    assert len(found) == 1, f"#{el_id} not found exactly once"
    return found[0]


def _inside(el: dict, container: dict) -> bool:
    return any(p is container for p in el["parents"])


def test_every_settings_field_has_one_help_line() -> None:
    tree = _tree()
    panel = _by_id(tree, "settingsPanel")
    fields = [e for e in tree.elements if _inside(e, panel)
              and e["tag"] in ("input", "textarea")]
    assert fields, "no Settings fields found"
    for field in fields:
        help_id = field["attrs"].get("aria-describedby")
        assert help_id, f"#{field['attrs'].get('id')} has no help line"
        help_el = _by_id(tree, help_id)
        assert "field-help" in help_el["attrs"].get("class", "")
        assert help_el["text"].strip(), f"#{help_id} is empty"


def test_settings_pane_says_settings_once() -> None:
    tree = _tree()
    pane = _by_id(tree, "paneSettings")
    headings = [e for e in tree.elements if _inside(e, pane)
                and ("home-title" in e["attrs"].get("class", "")
                     or e["tag"] in ("h1", "h2", "h3"))]
    titled = [e for e in headings if e["text"].strip() == "Settings"]
    assert len(titled) == 1, f"'Settings' heads the pane {len(titled)} times"


# An internal name in a label: UPPER_SNAKE, key=value, a code element's text,
# a format token like HH:MM, or a developer abbreviation for a folder.
_JARGON = re.compile(r"[A-Z]{2,}_[A-Z_]+|\w=\w|HH:MM|\bdir\b|\bmutex\b|\bglobs?\b", re.I)


def test_dialog_and_settings_labels_hold_no_internal_names() -> None:
    tree = _tree()
    scopes = [e for e in tree.elements if e["tag"] == "dialog"] + [_by_id(tree, "settingsPanel")]
    labels = [e for e in tree.elements
              if e["tag"] == "span" and e["parents"] and e["parents"][-1]["tag"] in ("label", "div")
              and any(_inside(e, s) for s in scopes)
              and (e["parents"][-1]["tag"] == "label"
                   or "switch-row" in e["parents"][-1]["attrs"].get("class", ""))]
    assert labels, "no field labels found"
    bad = [e["text"].strip() for e in labels if _JARGON.search(e["text"])]
    assert not bad, "field labels with internal names: " + " | ".join(bad)


def test_toolbar_toggles_carry_a_visible_word() -> None:
    tree = _tree()
    for el_id in ("claudeDetached", "claudeResume",
                  "lifeOsDetached", "lifeOsResume"):
        assert _by_id(tree, el_id)["text"].strip(), f"#{el_id} is icon-only"
    # The favourites star is the exception (#1194, the decision on
    # #1176): icon only, with its name kept on aria-label/title.
    fav = _by_id(tree, "favFilterBtn")
    assert not fav["text"].strip(), f"#favFilterBtn shows {fav['text'].strip()!r}"
    assert fav["attrs"].get("aria-label") == "Show only favorites"
    assert fav["attrs"].get("title") == "Show only favorites"


_STATIC = _INDEX.parent


def test_code_and_jobs_copy_use_whole_words_and_real_controls() -> None:
    """#1191: the Code tab's chips spell out what they are. #1201: the empty
    Schedule's own button opens the existing Add job dialog, under the label
    that dialog already carries — one add flow, not a second one."""
    tree = _tree()
    assert _by_id(tree, "gitStatusBtn")["text"].strip() == "Git status"

    badge = _STATIC.joinpath("context-filter.js").read_text(encoding="utf-8")
    assert not re.search(r"\btok\b|\(7d\)", badge), "badge abbreviates"

    agenda = _STATIC.joinpath("jobs-agenda.js").read_text(encoding="utf-8")
    assert "actionLabel: 'Add job'" in agenda
    assert "openJobDialog(null)" in agenda, "the empty Schedule must reuse the Add job flow"
    assert _by_id(tree, "jobDialogTitle")["text"].strip() == "Add job"
