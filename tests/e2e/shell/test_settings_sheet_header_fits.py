"""Every Settings sheet's header row fits its own box.

The ✕ button's invisible 44px hit area (34px box plus a 5px inset each side)
reaches past the header's right edge, so the layout lint read the header as
spilling 5px. The header reserves that inset itself, so the back button and
the ✕ stay where they were.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import Page

from tests.e2e._layout_lint import lint_layout

pytestmark = pytest.mark.smoke

_SHEETS_HTML = Path(__file__).resolve().parents[3] / "app/webapp/static/views/settings-sheets.html"
_SHEET_IDS = re.findall(r'<dialog id="(\w+)" class="detail-dialog settings-sheet"', _SHEETS_HTML.read_text(encoding="utf-8"))


def test_sheet_list_is_not_empty() -> None:
    assert len(_SHEET_IDS) >= 10


@pytest.mark.iphone
@pytest.mark.parametrize("sheet_id", _SHEET_IDS)
def test_settings_sheet_header_does_not_overflow(authed_page: Page, base_url: str, sheet_id: str) -> None:
    authed_page.goto(base_url, wait_until="domcontentloaded")
    authed_page.evaluate(f"document.getElementById('{sheet_id}').showModal()")
    spill = [
        f.line()
        for f in lint_layout(authed_page)
        if f.kind == "overflow" and "settings-sheet-head" in f.element
    ]
    assert spill == []
