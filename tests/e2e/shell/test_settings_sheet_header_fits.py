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


@pytest.mark.iphone
def test_every_settings_sheet_header_fits_its_box(authed_page: Page, base_url: str) -> None:
    """One page load opens each sheet in turn: a sheet's header lints the same
    whichever page load opened it, so a load per sheet bought nothing."""
    assert len(_SHEET_IDS) >= 10, _SHEET_IDS
    authed_page.goto(base_url, wait_until="domcontentloaded")
    spill: dict = {}
    for sheet_id in _SHEET_IDS:
        authed_page.evaluate(f"document.getElementById('{sheet_id}').showModal()")
        lines = [
            f.line()
            for f in lint_layout(authed_page)
            if f.kind == "overflow" and "settings-sheet-head" in f.element
        ]
        if lines:
            spill[sheet_id] = lines
        authed_page.evaluate(f"document.getElementById('{sheet_id}').close()")
    assert spill == {}
