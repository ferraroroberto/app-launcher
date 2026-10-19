"""Settings › Launch defaults › Pi: the Effort row follows the model (#1713).

Pi gets the same per-model effort as Claude (#1711 Step 1): the single Effort
select shows and edits the override of the model selected in the Model combo
above it, and its first choice, "Model default (<level>)", clears the override.
Hermetic: /api/config is route-mocked before ``goto()`` with a stateful GET/POST
pair that applies a ``pi_effort`` POST to the selected model the way the real
endpoint does.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import close_settings_sheets, open_agent_sheet, stable_eval, wait_until


pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_MODELS = {"claude-opus-5": "Opus", "claude-sonnet-5": "Sonnet", "gpt-5.6-sol": "Sol"}
_EFFORTS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"]


def _config(state: dict) -> dict:
    """The slice of /api/config the Pi subsection renders from."""
    model = state["model"]
    overrides = state["overrides"]
    return {
        "projects_dir": "E:/automation",
        "projects_ignore": [],
        "apps_scan_root": "",
        "life_os_dir": "",
        "fleet_config_dir": "",
        "model_catalog": {"claude": [], "codex": []},
        "pi": {
            "model": model,
            "models_available": [
                {"value": value, "label": label, "available": True}
                for value, label in _MODELS.items()
            ],
            "effort": overrides.get(model, "high"),
            "model_efforts": overrides,
            "default_effort": "high",
            "efforts_available": _EFFORTS,
            "trust_mode": "trust",
            "trust_modes_available": ["trust", "ask"],
            "computed_flags": "",
        },
    }


def _mock_config(page: Page) -> dict:
    state = {"model": "claude-opus-5", "overrides": {}, "patches": []}

    def _route(route):
        req = route.request
        if req.method == "POST":
            body = _json.loads(req.post_data or "{}")
            state["patches"].append(body)
            model = body.get("pi_model") or state["model"]
            if "pi_effort" in body:
                if body["pi_effort"]:
                    state["overrides"][model] = body["pi_effort"]
                else:
                    state["overrides"].pop(model, None)
            state["model"] = model
            route.fulfill(status=200, content_type="application/json", body="{}")
        else:
            route.fulfill(status=200, content_type="application/json",
                          body=_json.dumps(_config(state)))

    page.route(re.compile(r".*/api/config$"), _route)
    return state


def _pick_model(page: Page, model: str) -> None:
    page.locator("#piModel .model-combo-trigger").click()
    page.locator(f"#piModelMenu [data-value='{model}']").click()
    expect(page.locator("#piModel")).to_have_attribute("data-value", model, timeout=5_000)


def test_pi_effort_row_follows_the_model_and_edits_its_override(
    authed_page: Page, base_url: str
) -> None:
    state = _mock_config(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    open_agent_sheet(authed_page, "pi")
    effort = authed_page.locator("#piEffort")
    chosen = effort.locator("option:checked")

    # Every Pi model starts on its catalog default, named in the first choice.
    expect(effort).to_have_value("")
    expect(chosen).to_have_text("Model default (High)")
    expect(effort.locator("option")).to_have_text(
        ["Model default (High)", "Off", "Minimal", "Low", "Medium", "High", "Extra high", "Max"]
    )

    # An override is POSTed as the bare field and lands on the selected model.
    effort.select_option("medium")
    wait_until(authed_page, lambda: {"pi_effort": "medium"} in state["patches"],
               "the Opus effort override POST")
    expect(effort).to_have_value("medium")
    assert state["overrides"] == {"claude-opus-5": "medium"}

    # It belongs to Opus only: Sol still shows its default, Opus its override.
    _pick_model(authed_page, "gpt-5.6-sol")
    expect(effort).to_have_value("")
    expect(chosen).to_have_text("Model default (High)")
    _pick_model(authed_page, "claude-opus-5")
    expect(effort).to_have_value("medium")

    # "Model default" clears it with an empty string.
    effort.select_option("")
    wait_until(authed_page, lambda: {"pi_effort": ""} in state["patches"],
               "the clear-override POST")
    expect(effort).to_have_value("")
    assert state["overrides"] == {}
    close_settings_sheets(authed_page)


def test_pi_effort_row_fits_the_phone_width(authed_page: Page, base_url: str) -> None:
    """The longer "Model default (...)" text must not push the select out of
    its sheet."""
    _mock_config(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    open_agent_sheet(authed_page, "pi")
    box = stable_eval(
        authed_page.locator("#piEffort"),
        "el => { const r = el.getBoundingClientRect(); const s = el.closest('dialog').getBoundingClientRect();"
        " return [r.left, r.right, s.left, s.right, el.scrollWidth, el.clientWidth]; }",
    )
    left, right, sheet_left, sheet_right, scroll_width, client_width = box
    assert sheet_left <= left and right <= sheet_right, box
    assert scroll_width <= client_width, box
