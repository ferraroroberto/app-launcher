"""Settings › Models in the dropdowns drives every model dropdown.

The feature: Settings › Launch defaults carries one switch per catalog model,
grouped by agent, persisted as `hidden_models`. A switched-off model leaves the
Coding Projects picker, the Life OS Skills picker and the Board dispatch picker
at once; switching it back on returns it to all three with no reload.

The config write is real (the e2e conftest points the webapp at a throwaway
`webapp_config.json`), so the dropdowns are filled from the server's answer,
not a mocked catalog.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from src.model_visibility import DEFAULT_HIDDEN_MODELS
from tests.e2e.conftest import close_settings_sheets, open_settings_sheet

PICKERS = ("#codingModelMenu", "#lifeOsModelCombo", "#boardDispatchModel")


def _offered(page: Page, value: str):
    """`value`'s option in each of the three shared pickers."""
    return [page.locator(f"{picker} [role=option][data-value='{value}']") for picker in PICKERS]


def test_switching_a_model_on_offers_it_in_every_dropdown(authed_page: Page, base_url: str) -> None:
    # The disposable webapp boots from a copy of the real config, so the
    # baseline is set rather than assumed.
    authed_page.request.post(f"{base_url}/api/config", data={"hidden_models": list(DEFAULT_HIDDEN_MODELS)})
    try:
        authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
        for option in _offered(authed_page, "codex:gpt-6-luna"):
            expect(option).to_have_count(1)
        for option in _offered(authed_page, "codex:gpt-5.6-sol"):
            expect(option).to_have_count(0)

        open_settings_sheet(authed_page, "launchDefaultsSheet")
        sol = authed_page.locator('[data-model-toggle="codex:gpt-5.6-sol"]')
        expect(authed_page.locator('[data-model-toggle="codex:gpt-6-luna"]')).to_have_attribute("aria-checked", "true")
        expect(sol).to_have_attribute("aria-checked", "false")
        sol.click()
        expect(sol).to_have_attribute("aria-checked", "true")
        close_settings_sheets(authed_page)

        for option in _offered(authed_page, "codex:gpt-5.6-sol"):
            expect(option).to_have_count(1)
        saved = authed_page.request.get(f"{base_url}/api/config").json()["hidden_models"]
        assert "codex:gpt-5.6-sol" not in saved
    finally:
        authed_page.request.post(f"{base_url}/api/config", data={"hidden_models": list(DEFAULT_HIDDEN_MODELS)})
