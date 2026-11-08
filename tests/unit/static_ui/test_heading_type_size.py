"""A section heading is never smaller than the text it introduces (#1623).

The fleet design rule TYPE-07 reads every visible heading and fails it when its
computed size is below the next prose sibling. Both sub-headings were drawn at
the 12px caption size over 14px ``body-sm`` text. They are now body-sized
(``--font-body``); every fold summary (Backlog buckets, plan card, Your turn)
shares ``.board-plan-heading`` and sets no size of its own, so all read alike.
"""

from __future__ import annotations

import re

import pytest

from tests._css import stylesheet_text


def _css() -> str:
    return re.sub(r"/\*.*?\*/", "", stylesheet_text(), flags=re.S)


def _declaration(selector: str, prop: str) -> str:
    for sel_text, body in re.findall(r"([^{}]+)\{([^{}]*)\}", _css()):
        if sel_text.strip() == selector:
            m = re.search(rf"(?<![\w-]){prop}:\s*([^;]+);", body)
            assert m, f"{selector} sets no {prop}"
            return m.group(1).strip()
    raise AssertionError(f"no rule for {selector!r} in the stylesheets")


@pytest.mark.parametrize("selector", [".board-plan-heading", ".context-filter-subhead"])
def test_sub_heading_is_body_sized(selector: str) -> None:
    assert _declaration(selector, "font-size") == "var(--font-body)"


def test_fold_summary_takes_the_shared_heading_size() -> None:
    with pytest.raises(AssertionError, match="sets no font-size"):
        _declaration(".board-plan-fold-summary", "font-size")
