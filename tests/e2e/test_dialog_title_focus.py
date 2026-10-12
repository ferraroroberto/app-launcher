"""A dialog title draws no focus ring when its sheet opens (#1537).

``showModal()`` moves focus to the title (``<h2 tabindex="-1" autofocus>``) so
the keyboard stays down and the sheet is announced. iOS matches
``:focus-visible`` on that script focus, so the app-wide ring drew a blue
rectangle round the title, read as a stray selection. Contract:

  * on open the title is the focused element and its outline is ``none`` —
    for the titles outside ``.settings-sheet-head`` too (Sort and filter, the
    job and schedule sheets, the confirm dialog), which the old rule missed;
  * a control inside the sheet reached by Tab still wears the ring.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

# (dialog, title) — one settings sheet plus every titled dialog that sits
# outside ``.settings-sheet-head``.
_TITLED_DIALOGS = [
    ("launchDefaultsSheet", "launchDefaultsSheetTitle"),
    ("lifeOsConvoSheet", "lifeOsConvoSheetTitle"),
    ("jobSheet", "jobSheetTitle"),
    ("jobsAgendaSheet", "jobsAgendaSheetTitle"),
    ("confirmDialog", "confirmDialogTitle"),
]


@pytest.mark.iphone
@pytest.mark.parametrize("dialog_id, title_id", _TITLED_DIALOGS)
def test_dialog_title_has_no_ring_but_tabbed_control_does(
    authed_page: Page, base_url: str, dialog_id: str, title_id: str
) -> None:
    authed_page.goto(base_url, wait_until="domcontentloaded")
    authed_page.evaluate(f"document.getElementById('{dialog_id}').showModal()")

    title = authed_page.locator(f"#{title_id}")
    expect(title).to_be_focused()
    expect(title).to_have_css("outline-style", "none")

    # Keyboard focus on a real control inside the sheet keeps its ring.
    authed_page.keyboard.press("Tab")
    control = authed_page.locator(f"#{dialog_id} :focus")
    expect(control).to_have_count(1)
    expect(control).not_to_have_id(title_id)
    expect(control).to_have_css("outline-style", "solid")

    authed_page.evaluate(f"document.getElementById('{dialog_id}').close()")
