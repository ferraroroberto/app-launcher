"""Settings › Launch defaults › Claude Code: the Effort row follows the model (#1711).

Every model has its own default effort, so the single Effort select shows and
edits the override of the model selected in the Model combo above it. Its first
choice, "Model default (<level>)", clears the override; "Don't send" omits
``--effort``. Hermetic: /api/config is route-mocked before ``goto()`` with a
stateful GET/POST pair that applies a ``claude_effort`` POST to the selected
model the way the real endpoint does.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.ui import close_settings_sheets, open_agent_sheet
from tests.e2e.fixtures.waits import stable_eval, wait_until


pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_DEFAULTS = {"opus": "medium", "sonnet": "high", "fable": "high"}


def _config(state: dict) -> dict:
    """The slice of /api/config the Claude subsection renders from."""
    model = state["model"]
    overrides = state["overrides"]
    return {
        "projects_dir": "E:/automation",
        "projects_ignore": [],
        "apps_scan_root": "",
        "life_os_dir": "",
        "fleet_config_dir": "",
        "coding_model_choice": f"claude:{model}",
        "model_catalog": {
            "claude": [
                {"value": value, "label": value.title(), "available": True,
                 "efforts": [], "default_effort": _DEFAULTS[value]}
                for value in _DEFAULTS
            ],
            "codex": [],
        },
        "claude": {
            "model": model,
            "models_available": list(_DEFAULTS),
            "effort": overrides.get(model, _DEFAULTS[model]),
            "model_efforts": overrides,
            "default_effort": _DEFAULTS[model],
            "efforts_available": ["off", "low", "medium", "high", "xhigh", "max"],
            "permission_mode": "auto",
            "permission_modes_available": ["auto", "skip"],
            "verbose": False,
            "debug": False,
            "computed_flags": "",
        },
    }


def _mock_config(page: Page) -> dict:
    state = {"model": "opus", "overrides": {}, "patches": []}

    def _route(route):
        req = route.request
        if req.method == "POST":
            body = _json.loads(req.post_data or "{}")
            state["patches"].append(body)
            model = body.get("claude_model") or state["model"]
            if "claude_effort" in body:
                if body["claude_effort"]:
                    state["overrides"][model] = body["claude_effort"]
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
    page.locator("#claudeModel .model-combo-trigger").click()
    page.locator(f"#claudeModelMenu [data-value='{model}']").click()
    expect(page.locator("#claudeModel")).to_have_attribute("data-value", model, timeout=5_000)


def test_effort_row_follows_the_model_and_edits_its_override(
    authed_page: Page, base_url: str
) -> None:
    state = _mock_config(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    open_agent_sheet(authed_page, "claude")
    effort = authed_page.locator("#claudeEffort")
    chosen = effort.locator("option:checked")

    # Opus starts on its own default, named in the first choice.
    expect(effort).to_have_value("")
    expect(chosen).to_have_text("Model default (Medium)")
    # The offered levels: the default choice, the levels, and Don't send last.
    expect(effort.locator("option")).to_have_text(
        ["Model default (Medium)", "Low", "Medium", "High", "Extra high", "Max", "Don\u2019t send"]
    )

    # Switching the model re-renders the row for that model.
    _pick_model(authed_page, "sonnet")
    expect(effort).to_have_value("")
    expect(chosen).to_have_text("Model default (High)")

    # An override is POSTed as the bare field and lands on the selected model.
    effort.select_option("low")
    wait_until(authed_page, lambda: {"claude_effort": "low"} in state["patches"],
               "the Sonnet effort override POST")
    expect(effort).to_have_value("low")
    assert state["overrides"] == {"sonnet": "low"}

    # It belongs to Sonnet only: Opus still shows its default, Sonnet its override.
    _pick_model(authed_page, "opus")
    expect(effort).to_have_value("")
    expect(chosen).to_have_text("Model default (Medium)")
    _pick_model(authed_page, "sonnet")
    expect(effort).to_have_value("low")

    # "Model default" clears it with an empty string.
    effort.select_option("")
    wait_until(authed_page, lambda: {"claude_effort": ""} in state["patches"],
               "the clear-override POST")
    expect(effort).to_have_value("")
    assert state["overrides"] == {}
    close_settings_sheets(authed_page)


def test_effort_row_fits_the_phone_width(authed_page: Page, base_url: str) -> None:
    """The longer "Model default (...)" text must not push the select out of
    its sheet (the new risk this row introduced)."""
    _mock_config(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    open_agent_sheet(authed_page, "claude")
    box = stable_eval(
        authed_page.locator("#claudeEffort"),
        "el => { const r = el.getBoundingClientRect(); const s = el.closest('dialog').getBoundingClientRect();"
        " return [r.left, r.right, s.left, s.right, el.scrollWidth, el.clientWidth]; }",
    )
    left, right, sheet_left, sheet_right, scroll_width, client_width = box
    assert sheet_left <= left and right <= sheet_right, box
    assert scroll_width <= client_width, box
