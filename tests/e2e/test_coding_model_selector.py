"""Coding-tab launch model selector (issue #540).

The Projects card's <summary> gained a board-style model dropdown
(``#codingModelCombo`` — a <button> trigger + a <span role="listbox"> of
option buttons, NOT a native <select>, which WebKit's HTML parser cannot
survive inside a <summary>) that stays in sync with the options-card
segmented control (``#claudeModel``). The provider-qualified combo also offers
the explicit Codex Luna/Terra/Sol/Astra choices.

Hermetic: /api/config is route-mocked with a tiny stateful handler that
stores ``claude_model`` on POST and echoes it on GET, exactly as the real
``patchConfig`` round-trip does — so the sync is exercised without mutating
the live disposable webapp's config.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke


def _config(model: str, choice: str) -> dict:
    """A minimal /api/config payload — enough for fetchConfig +
    renderClaudeSubsection; the other agent subsections are omitted (each
    render* returns early when its key is absent). ``models_available``
    includes Haiku so the test proves it's filtered out of both controls."""
    return {
        "projects_dir": "E:/automation",
        "projects_ignore": [],
        "apps_scan_root": "",
        "life_os_dir": "",
        "claude_config_dir": "",
        "coding_model_choice": choice,
        "model_catalog": {
            "claude": [
                {"value": value, "label": value.title(), "available": True, "efforts": []}
                for value in ("sonnet", "opus", "fable")
            ],
            "codex": [
                {"value": "gpt-5.6-luna", "label": "Luna", "available": True, "efforts": ["xhigh"]},
                {"value": "gpt-6-astra", "label": "Astra", "available": False, "efforts": ["high"]},
            ],
        },
        "claude": {
            "model": model,
            "models_available": ["opus", "sonnet", "haiku", "fable"],
            "effort": "off",
            "efforts_available": ["off", "low", "medium", "high"],
            "permission_mode": "auto",
            "permission_modes_available": ["auto", "skip"],
            "verbose": False,
            "debug": False,
            "computed_flags": "",
        },
    }


def _mock_config(page: Page) -> dict:
    """Route /api/config with a stateful GET/POST pair mimicking patchConfig.
    Returns the mutable state dict so a test can read the last-persisted model."""
    state = {"model": "sonnet", "choice": "claude:sonnet"}

    def _route(route):
        req = route.request
        if req.method == "POST":
            body = _json.loads(req.post_data or "{}")
            if "claude_model" in body:
                state["model"] = body["claude_model"]
            if "coding_model_choice" in body:
                state["choice"] = body["coding_model_choice"]
                if state["choice"].startswith("claude:"):
                    state["model"] = state["choice"].split(":", 1)[1]
            route.fulfill(status=200, content_type="application/json", body="{}")
        else:
            route.fulfill(
                status=200, content_type="application/json",
                body=_json.dumps(_config(state["model"], state["choice"])),
            )

    page.route(re.compile(r".*/api/config$"), _route)
    return state


def test_coding_model_combo_syncs_with_segmented_control(
    authed_page: Page, base_url: str
) -> None:
    """Picking a model in the Projects-summary combo updates the options-card
    segmented control (and vice versa), and both persist the same
    ``claude_model`` — the #540 no-double-setting contract."""
    state = _mock_config(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    # Coding (#tabClaude) is the default active tab.
    combo = authed_page.locator("#codingModelCombo")
    trigger = authed_page.locator("#codingModelBtn")
    expect(trigger).to_be_visible(timeout=5_000)
    # Boot default is Sonnet.
    expect(combo).to_have_attribute("data-value", "claude:sonnet")
    expect(trigger).to_have_text("Claude · Sonnet")

    # Haiku is filtered out of BOTH controls despite being in models_available.
    expect(
        authed_page.locator("#codingModelMenu button[data-value='claude:haiku']")
    ).to_have_count(0)
    expect(
        authed_page.locator("#claudeModel button[data-value='haiku']")
    ).to_have_count(0)

    # Combo → segmented: open the dropdown, pick Fable; the segmented control's
    # Fable button becomes active and the config persisted Fable.
    trigger.click()
    authed_page.locator("#codingModelMenu button[data-value='claude:fable']").click()
    expect(
        authed_page.locator("#claudeModel button[data-value='fable']")
    ).to_have_class(re.compile(r"\bactive\b"), timeout=5_000)
    expect(trigger).to_have_text("Claude · Fable")
    assert state["model"] == "fable"

    # Segmented → combo: expand the options card and click Opus; the dropdown
    # trigger follows and Opus is persisted.
    authed_page.locator("#codingOptions").evaluate("el => { el.open = true; }")
    authed_page.locator("#claudeModel button[data-value='opus']").click()
    expect(combo).to_have_attribute("data-value", "claude:opus", timeout=5_000)
    expect(trigger).to_have_text("Claude · Opus")
    assert state["model"] == "opus"

    expect(
        authed_page.locator("#codingModelMenu button[data-value='codex:gpt-6-astra']")
    ).to_be_disabled()


def test_server_catalog_populates_coding_and_board_selectors(
    authed_page: Page, base_url: str
) -> None:
    """#845: the real backend catalog replaces each one-option bootstrap."""
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    coding = authed_page.locator("#codingModelMenu button[data-value]")
    expect(coding).to_have_count(7, timeout=5_000)
    expect(
        authed_page.locator("#codingModelMenu [data-value='codex:gpt-5.6-luna']")
    ).to_have_text("Codex · Luna")
    expect(
        authed_page.locator("#codingModelMenu [data-value='codex:gpt-6-astra']")
    ).to_be_enabled()

    board = authed_page.locator("#boardDispatchModel option")
    expect(board).to_have_count(7)
    expect(
        authed_page.locator("#boardDispatchModel option[value='codex:gpt-6-astra']")
    ).to_be_enabled()
