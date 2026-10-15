"""A section heading is never smaller than the text it introduces (#1623).

The fleet design rule TYPE-07 reads every visible heading and fails it when its
computed size is below the next prose sibling. Both sub-headings were drawn at
the 12px caption size over 14px ``body-sm`` text. They are now body-sized
(``--font-body``); the parked-section summary shares ``.board-plan-heading`` but
is a control, not a heading, so it keeps the caption size.
"""

from __future__ import annotations

import pathlib
import re

import pytest

STATIC = pathlib.Path(__file__).resolve().parents[1] / "app" / "webapp" / "static"


def _css() -> str:
    css = (STATIC / "styles.css").read_text(encoding="utf-8").replace("\r\n", "\n")
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _declaration(selector: str, prop: str) -> str:
    for sel_text, body in re.findall(r"([^{}]+)\{([^{}]*)\}", _css()):
        if sel_text.strip() == selector:
            m = re.search(rf"(?<![\w-]){prop}:\s*([^;]+);", body)
            assert m, f"{selector} sets no {prop}"
            return m.group(1).strip()
    raise AssertionError(f"no rule for {selector!r} in styles.css")


@pytest.mark.parametrize("selector", [".board-plan-heading", ".context-filter-subhead"])
def test_sub_heading_is_body_sized(selector: str) -> None:
    assert _declaration(selector, "font-size") == "var(--font-body)"


def test_parked_summary_control_keeps_the_caption_size() -> None:
    assert _declaration(".board-plan-parked-summary", "font-size") == "var(--font-caption)"
