"""Coding-tab launch model selector (issue #540).

The Projects card's <summary> gained a board-style model dropdown
(``#codingModelCombo`` — a <button> trigger + a <span role="listbox"> of
option buttons, NOT a native <select>, which WebKit's HTML parser cannot
survive inside a <summary>) that stays in sync with the options-card
model picker (``#claudeModel``). The provider-qualified combo also offers
the explicit Codex Luna/Terra/Sol/Astra choices.

Hermetic: /api/config is route-mocked with a tiny stateful handler that
stores ``claude_model`` on POST and echoes it on GET, exactly as the real
``patchConfig`` round-trip does — so the sync is exercised without mutating
the live disposable webapp's config.
"""

from __future__ import annotations

import json as _json
import re
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, expect


pytestmark = pytest.mark.smoke


def _open_projects(page: Page) -> None:
    """The launch model combo sits in the Projects card's toolbar since
    #1132 (out of its <summary>), so the card has to be open to reach it."""
    page.locator("details.projects-card").evaluate("el => { el.open = true; }")


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
        "fleet_config_dir": "",
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
        "codex": {
            "model": "gpt-5.6-luna",
            "models_available": [
                {"value": "gpt-5.6-luna", "label": "Luna", "available": True},
                {"value": "gpt-5.6-sol", "label": "Sol", "available": True},
                {"value": "gpt-6-astra", "label": "Astra", "available": False,
                 "unavailable_reason": "Not rolled out"},
            ],
            "effort": "xhigh",
            "efforts_available": ["xhigh"],
            "permission_mode": "auto",
            "permission_modes_available": ["auto", "skip"],
            "computed_flags": "--model gpt-5.6-luna",
        },
        # Copilot's payload carries no model/models_available since #1017 —
        # the launcher sends no --model for it, so there is nothing to pick.
        "copilot": {
            "skip_permissions": False,
            "computed_flags": "",
        },
        # Pi's list is padded with synthetic entries so one picker in the
        # panel is still long enough to prove the shared portaled menu stays
        # viewport-constrained. That check used to ride on Copilot's 22-entry
        # catalogue, which #1017 removed; the constraint belongs to the
        # shared model-combo controller, not to any one agent, so it moved
        # here rather than leaving with the catalogue.
        "pi": {
            "model": "anthropic/sonnet",
            "models_available": [
                {"value": "anthropic/sonnet", "label": "Sonnet", "available": True},
                {"value": "openai/sol", "label": "Sol", "available": True},
                {"value": "openai/astra", "label": "Astra", "available": False,
                 "unavailable_reason": "Not rolled out"},
            ] + [
                {"value": f"filler/model-{index:02d}",
                 "label": f"Filler {index:02d}", "available": True}
                for index in range(12)
            ],
            "effort": "medium",
            "efforts_available": ["low", "medium", "high"],
            "trust_mode": "ask",
            "trust_modes_available": ["ask", "trust"],
            "computed_flags": "--model anthropic/sonnet",
        },
    }


def _mock_config(page: Page) -> dict:
    """Route /api/config with a stateful GET/POST pair mimicking patchConfig.
    Returns the mutable state dict so a test can read the last-persisted model."""
    state = {
        "model": "sonnet", "choice": "claude:sonnet",
        "codex_model": "gpt-5.6-luna",
        "pi_model": "anthropic/sonnet", "patches": [],
    }

    def _route(route):
        req = route.request
        if req.method == "POST":
            body = _json.loads(req.post_data or "{}")
            state["patches"].append(body)
            if "claude_model" in body:
                state["model"] = body["claude_model"]
            if "coding_model_choice" in body:
                state["choice"] = body["coding_model_choice"]
                if state["choice"].startswith("claude:"):
                    state["model"] = state["choice"].split(":", 1)[1]
            for key in ("codex_model", "pi_model"):
                if key in body:
                    state[key] = body[key]
            route.fulfill(status=200, content_type="application/json", body="{}")
        else:
            body = _config(state["model"], state["choice"])
            body["codex"]["model"] = state["codex_model"]
            if state["codex_model"] == "gpt-5.6-sol":
                body["codex"].update(
                    effort="high", efforts_available=["low", "high"],
                    computed_flags="--model gpt-5.6-sol",
                )
            body["pi"]["model"] = state["pi_model"]
            body["pi"]["computed_flags"] = "--model " + state["pi_model"]
            route.fulfill(
                status=200, content_type="application/json",
                body=_json.dumps(body),
            )

    page.route(re.compile(r".*/api/config$"), _route)
    return state


@pytest.mark.iphone
def test_coding_model_combo_syncs_with_settings_control(
    authed_page: Page, base_url: str
) -> None:
    """Picking a model in the Projects-summary combo updates the options-card
    picker (and vice versa), and both persist the same
    ``claude_model`` — the #540 no-double-setting contract."""
    state = _mock_config(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_projects(authed_page)
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

    # Pointer-opened menus keep focus on their trigger. A following navigation
    # key must enter the listbox and skip unavailable options just like a
    # keyboard-opened menu.
    for key, expected in (
        ("ArrowDown", "claude:sonnet"),
        ("ArrowUp", "codex:gpt-5.6-luna"),
        ("Home", "claude:sonnet"),
        ("End", "codex:gpt-5.6-luna"),
    ):
        trigger.click()
        expect(trigger).to_have_attribute("aria-expanded", "true")
        trigger.press(key)
        assert (
            authed_page.evaluate("document.activeElement.dataset.value") == expected
        )
        authed_page.keyboard.press("Escape")
        expect(trigger).to_have_attribute("aria-expanded", "false")

    # Header → settings: pick Fable; the settings picker follows and persists.
    trigger.click()
    authed_page.locator("#codingModelMenu button[data-value='claude:fable']").click()
    expect(authed_page.locator("#claudeModel")).to_have_attribute(
        "data-value", "fable", timeout=5_000
    )
    expect(trigger).to_have_text("Claude · Fable")
    assert state["model"] == "fable"

    # Settings → header: expand the options card and click Opus; the dropdown
    # trigger follows and Opus is persisted.
    authed_page.locator("#codingOptions").evaluate("el => { el.open = true; }")
    authed_page.locator("#claudeModel .model-combo-trigger").click()
    authed_page.locator("#claudeModelMenu [data-value='opus']").click()
    expect(combo).to_have_attribute("data-value", "claude:opus", timeout=5_000)
    expect(trigger).to_have_text("Claude · Opus")
    assert state["model"] == "opus"

    # Every settings picker is populated through the same controller. Pointer
    # choices persist through the real POST→GET round-trip and dependent Codex
    # effort options repaint from that readback.
    authed_page.locator("#codexModel .model-combo-trigger").click()
    expect(authed_page.locator("#codexModelMenu [data-value='gpt-6-astra']")).to_be_disabled()
    authed_page.locator("#codexModelMenu [data-value='gpt-5.6-sol']").click()
    expect(authed_page.locator("#codexModel")).to_have_attribute(
        "data-value", "gpt-5.6-sol"
    )
    expect(authed_page.locator("#codexEffort")).to_have_value("high", timeout=5_000)

    # A long menu is held to the viewport (dom-utils.js positionMenu caps its
    # height at the room beside the trigger). Whether 15 options (480px) need
    # capping depends on where the trigger sits, and that used to be wherever
    # the steps above left the scroll: with the trigger high in a 739px
    # iPhone viewport all 480px fit, the menu was rightly left whole, and the
    # check read 480 > 480 (#1188). Pin the geometry instead: a viewport
    # shorter than the menu, the trigger at its roomiest spot (the top).
    pi_trigger = authed_page.locator("#piModel .model-combo-trigger")
    viewport = authed_page.viewport_size
    authed_page.set_viewport_size({"width": viewport["width"], "height": 400})
    pi_trigger.evaluate("el => el.scrollIntoView({block: 'start'})")
    pi_trigger.click()
    pi_menu = authed_page.locator("#piModelMenu")
    expect(pi_menu.locator("[role='option']")).to_have_count(15)
    client, scroll, bottom, room = pi_menu.evaluate(
        "el => [el.clientHeight, el.scrollHeight, el.getBoundingClientRect().bottom,"
        " document.documentElement.clientHeight]"
    )
    assert scroll > client, f"long model menu is not viewport constrained: {client}/{scroll}"
    assert bottom <= room, f"model menu runs off the viewport: bottom {bottom} > {room}"
    expect(authed_page.locator("#piModelMenu [data-value='openai/astra']")).to_be_disabled()
    authed_page.locator("#piModelMenu [data-value='openai/sol']").click()
    expect(authed_page.locator("#piModel")).to_have_attribute("data-value", "openai/sol")
    expect(authed_page.locator("#piFlagsPreview")).to_have_text("pi --model openai/sol")
    authed_page.set_viewport_size(viewport)

    # Keyboard traversal skips disabled Astra, selects exactly once, and
    # Escape restores focus and the collapsed ARIA state.
    codex_trigger = authed_page.locator("#codexModel .model-combo-trigger")
    patch_count = len(state["patches"])
    codex_trigger.focus()
    codex_trigger.press("Home")
    authed_page.keyboard.press("End")
    assert authed_page.evaluate("document.activeElement.dataset.value") == "gpt-5.6-sol"
    authed_page.keyboard.press("Enter")
    authed_page.wait_for_timeout(100)
    assert len(state["patches"]) == patch_count + 1
    codex_trigger.press("Enter")
    authed_page.keyboard.press("Escape")
    expect(codex_trigger).to_have_attribute("aria-expanded", "false")
    assert codex_trigger.evaluate("el => document.activeElement === el")

    patch_count = len(state["patches"])
    codex_trigger.press("ArrowDown")
    assert authed_page.evaluate("document.activeElement.dataset.value") == "gpt-5.6-luna"
    authed_page.keyboard.press("ArrowDown")
    assert authed_page.evaluate("document.activeElement.dataset.value") == "gpt-5.6-sol"
    authed_page.keyboard.press("ArrowDown")
    assert authed_page.evaluate("document.activeElement.dataset.value") == "gpt-5.6-luna"
    authed_page.keyboard.press("Space")
    authed_page.wait_for_timeout(100)
    assert len(state["patches"]) == patch_count + 1
    expect(authed_page.locator("#codexModelMenu [data-value='gpt-5.6-luna']")).to_have_attribute(
        "aria-selected", "true"
    )

    codex_trigger.click()
    expect(codex_trigger).to_have_attribute("aria-expanded", "true")
    authed_page.locator("#codexFlagsPreview").dispatch_event("pointerdown")
    expect(codex_trigger).to_have_attribute("aria-expanded", "false")
    assert codex_trigger.evaluate("el => document.activeElement === el")

    # A reload reads every stored value back without a programmatic onChange.
    persisted_patch_count = len(state["patches"])
    authed_page.reload(wait_until="domcontentloaded")
    expect(authed_page.locator("#codexModel")).to_have_attribute(
        "data-value", "gpt-5.6-luna"
    )
    expect(authed_page.locator("#piModel")).to_have_attribute("data-value", "openai/sol")
    assert len(state["patches"]) == persisted_patch_count

    expect(
        authed_page.locator("#codingModelMenu button[data-value='codex:gpt-6-astra']")
    ).to_be_disabled()


@pytest.mark.iphone
def test_server_catalog_populates_shared_model_selectors(
    authed_page: Page, base_url: str
) -> None:
    """#845/#851: every model surface uses the shared catalog-backed picker."""
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_projects(authed_page)

    coding = authed_page.locator("#codingModelMenu button[data-value]")
    expect(coding).to_have_count(7, timeout=5_000)
    expect(
        authed_page.locator("#codingModelMenu [data-value='codex:gpt-5.6-luna']")
    ).to_have_text("Codex · Luna")
    expect(
        authed_page.locator("#codingModelMenu [data-value='codex:gpt-6-astra']")
    ).to_be_enabled()

    shared = (
        "#codingModelCombo, #claudeModel, #codexModel, #piModel, "
        "#lifeOsModelCombo, #lifeOsConvosModelCombo, #boardDispatchModel, "
        "#chiefModelSelect"
    )
    expect(authed_page.locator(shared)).to_have_count(8)
    for selector in shared.split(", "):
        root = authed_page.locator(selector)
        expect(root).to_have_class(re.compile(r"\bmodel-combo\b"))
        expect(root.locator(".model-combo-trigger")).to_have_count(1)
        expect(root.locator(".model-combo-menu[role='listbox']")).to_have_count(1)

    board = authed_page.locator("#boardDispatchModel")
    expect(board.locator("[role='option']")).to_have_count(7)
    expect(
        board.locator("[data-value='codex:gpt-6-astra']")
    ).to_be_enabled()

    authed_page.locator("#codingOptions").evaluate("el => { el.open = true; }")
    for theme in ("light", "dark"):
        authed_page.evaluate(
            "value => document.documentElement.dataset.theme = value", theme
        )
        signatures = authed_page.locator(
            "#claudeModel .model-combo-trigger, #codexModel .model-combo-trigger, "
            "#piModel .model-combo-trigger"
        ).evaluate_all(
            """nodes => nodes.map(node => {
              const style = getComputedStyle(node);
              return [style.backgroundColor, style.color, style.borderColor,
                      style.borderRadius, style.height, style.fontSize].join('|');
            })"""
        )
        assert len(set(signatures)) == 1, f"{theme} settings picker style drift: {signatures}"


def test_model_selection_owns_polls_until_config_save_settles(
    authed_page: Page, base_url: str
) -> None:
    """#847: a rapid reselection cannot be repainted by an older save.

    Quota rows stopped following this selection in #860; what still has to
    hold is that the picker itself settles on persisted server truth no
    matter how the concurrent POST + readback interleave.
    """
    config = _config("sonnet", "claude:sonnet")
    authed_page.add_init_script(
        """
        (fixture => {
          const nativeFetch = window.fetch.bind(window);
          let config = fixture.config;
          const saves = [];
          const reads = [];
          let delayNextConfigRead = false;
          window.__modelSaves = saves;
          window.__modelReads = reads;
          window.__delayNextConfigRead = function () {
            delayNextConfigRead = true;
          };
          window.__settleModelRead = function () {
            const read = reads.shift();
            if (!read) throw new Error('no pending config read');
            read.resolve(read.response);
          };
          window.__settleModelSave = function (outcome) {
            const save = saves.shift();
            if (!save) throw new Error('no pending model save');
            if (outcome === 'ok') {
              if (save.patch.coding_model_choice) {
                config = {...config, coding_model_choice: save.patch.coding_model_choice};
              }
              save.resolve(new Response('{}', {
                status: 200, headers: {'Content-Type': 'application/json'}
              }));
            } else {
              save.resolve(new Response(JSON.stringify({detail: 'fixture save failed'}), {
                status: 500, headers: {'Content-Type': 'application/json'}
              }));
            }
          };
          window.fetch = function (input, init) {
            const url = new URL(typeof input === 'string' ? input : input.url, location.href);
            if (url.pathname === '/api/config') {
              if ((init && init.method) === 'POST') {
                const patch = JSON.parse(init.body || '{}');
                return new Promise(resolve => saves.push({patch, resolve}));
              }
              const response = new Response(JSON.stringify(config), {
                status: 200, headers: {'Content-Type': 'application/json'}
              });
              if (delayNextConfigRead) {
                delayNextConfigRead = false;
                return new Promise(resolve => reads.push({response, resolve}));
              }
              return Promise.resolve(response);
            }
            if (url.pathname === '/api/rate-limits') {
              return Promise.resolve(new Response('{"quota_lines": []}', {
                status: 200, headers: {'Content-Type': 'application/json'}
              }));
            }
            return nativeFetch(input, init);
          };
        })(%s)
        """ % _json.dumps({"config": config}),
    )
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_projects(authed_page)
    combo = authed_page.locator("#codingModelCombo")
    trigger = authed_page.locator("#codingModelBtn")
    expect(combo).to_have_attribute("data-value", "claude:sonnet")

    trigger.click()
    authed_page.locator(
        "#codingModelMenu button[data-value='codex:gpt-5.6-luna']"
    ).click()
    expect(combo).to_have_attribute("data-value", "codex:gpt-5.6-luna")
    authed_page.wait_for_function("window.__modelSaves.length === 1")

    # Quota polling stopped depending on model selection in #860. Keep this
    # test deterministic and focused on the queued config save/readback race;
    # test_usage_meter.py covers the selection-independent quota poll.
    expect(combo).to_have_attribute("data-value", "codex:gpt-5.6-luna")

    # A successful save settles on the persisted Codex choice.
    authed_page.evaluate("window.__settleModelSave('ok')")
    authed_page.wait_for_function("window.__modelSaves.length === 0")
    expect(combo).to_have_attribute("data-value", "codex:gpt-5.6-luna")

    # A newer rapid selection can arrive after an older POST starts its config
    # readback. Even if that GET captured the older persisted value, releasing
    # it cannot repaint over the newer selection.
    trigger.click()
    authed_page.locator(
        "#codingModelMenu button[data-value='claude:fable']"
    ).click()
    authed_page.wait_for_function("window.__modelSaves.length === 1")
    authed_page.evaluate("window.__delayNextConfigRead()")
    authed_page.evaluate("window.__settleModelSave('ok')")
    authed_page.wait_for_function("window.__modelReads.length === 1")
    trigger.click()
    authed_page.locator(
        "#codingModelMenu button[data-value='codex:gpt-5.6-luna']"
    ).click()
    expect(combo).to_have_attribute("data-value", "codex:gpt-5.6-luna")
    authed_page.evaluate("window.__settleModelRead()")
    authed_page.wait_for_function("window.__modelSaves.length === 1")
    expect(combo).to_have_attribute("data-value", "codex:gpt-5.6-luna")
    authed_page.evaluate("window.__settleModelSave('ok')")
    authed_page.wait_for_function("window.__modelSaves.length === 0")
    expect(combo).to_have_attribute("data-value", "codex:gpt-5.6-luna")

    # A failed older save cannot repaint a newer selection while its persisted
    # readback is delayed. If the newest save then fails too, the control
    # reconciles explicitly to persisted server truth.
    trigger.click()
    authed_page.locator(
        "#codingModelMenu button[data-value='claude:fable']"
    ).click()
    expect(combo).to_have_attribute("data-value", "claude:fable")
    authed_page.wait_for_function("window.__modelSaves.length === 1")
    authed_page.evaluate("window.__delayNextConfigRead()")
    authed_page.evaluate("window.__settleModelSave('fail')")
    authed_page.wait_for_function("window.__modelReads.length === 1")
    trigger.click()
    authed_page.locator(
        "#codingModelMenu button[data-value='claude:sonnet']"
    ).click()
    expect(combo).to_have_attribute("data-value", "claude:sonnet")
    authed_page.evaluate("window.__settleModelRead()")
    authed_page.wait_for_function("window.__modelSaves.length === 1")
    expect(combo).to_have_attribute("data-value", "claude:sonnet")
    authed_page.evaluate("window.__settleModelSave('fail')")
    expect(combo).to_have_attribute("data-value", "codex:gpt-5.6-luna")
