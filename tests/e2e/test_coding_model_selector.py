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

from tests.e2e.conftest import stable_read

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
        "copilot": {
            "model": "",
            "models_available": [f"copilot-model-{index:02d}" for index in range(14)],
            "skip_permissions": False,
            "computed_flags": "",
        },
        "pi": {
            "model": "anthropic/sonnet",
            "models_available": [
                {"value": "anthropic/sonnet", "label": "Sonnet", "available": True},
                {"value": "openai/sol", "label": "Sol", "available": True},
                {"value": "openai/astra", "label": "Astra", "available": False,
                 "unavailable_reason": "Not rolled out"},
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
        "codex_model": "gpt-5.6-luna", "copilot_model": "",
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
            for key in ("codex_model", "copilot_model", "pi_model"):
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
            body["copilot"]["model"] = state["copilot_model"]
            body["pi"]["model"] = state["pi_model"]
            body["pi"]["computed_flags"] = "--model " + state["pi_model"]
            route.fulfill(
                status=200, content_type="application/json",
                body=_json.dumps(body),
            )

    page.route(re.compile(r".*/api/config$"), _route)
    return state


def test_coding_model_combo_syncs_with_settings_control(
    authed_page: Page, base_url: str
) -> None:
    """Picking a model in the Projects-summary combo updates the options-card
    picker (and vice versa), and both persist the same
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
    expect(authed_page.locator("#codexEffort [data-value='high']")).to_have_class(
        re.compile(r"\bactive\b"), timeout=5_000
    )

    authed_page.locator("#copilotModel .model-combo-trigger").click()
    copilot_menu = authed_page.locator("#copilotModelMenu")
    expect(copilot_menu.locator("[role='option']")).to_have_count(15)
    menu_sizes = copilot_menu.evaluate("el => [el.clientHeight, el.scrollHeight]")
    assert menu_sizes[1] > menu_sizes[0], "long Copilot menu is not viewport constrained"
    copilot_menu.locator("[data-value='copilot-model-13']").click()

    authed_page.locator("#piModel .model-combo-trigger").click()
    expect(authed_page.locator("#piModelMenu [data-value='openai/astra']")).to_be_disabled()
    authed_page.locator("#piModelMenu [data-value='openai/sol']").click()
    expect(authed_page.locator("#piModel")).to_have_attribute("data-value", "openai/sol")
    expect(authed_page.locator("#piFlagsPreview")).to_have_text("pi --model openai/sol")

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
    expect(authed_page.locator("#copilotModel")).to_have_attribute(
        "data-value", "copilot-model-13"
    )
    expect(authed_page.locator("#piModel")).to_have_attribute("data-value", "openai/sol")
    assert len(state["patches"]) == persisted_patch_count

    expect(
        authed_page.locator("#codingModelMenu button[data-value='codex:gpt-6-astra']")
    ).to_be_disabled()


def test_server_catalog_populates_shared_model_selectors(
    authed_page: Page, base_url: str
) -> None:
    """#845/#851: every model surface uses the shared catalog-backed picker."""
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    coding = authed_page.locator("#codingModelMenu button[data-value]")
    expect(coding).to_have_count(7, timeout=5_000)
    expect(
        authed_page.locator("#codingModelMenu [data-value='codex:gpt-5.6-luna']")
    ).to_have_text("Codex · Luna")
    expect(
        authed_page.locator("#codingModelMenu [data-value='codex:gpt-6-astra']")
    ).to_be_enabled()

    shared = (
        "#codingModelCombo, #claudeModel, #codexModel, #copilotModel, #piModel, "
        "#lifeOsModelCombo, #lifeOsConvosModelCombo, #boardDispatchModel, "
        "#chiefModelSelect"
    )
    expect(authed_page.locator(shared)).to_have_count(9)
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
            "#copilotModel .model-combo-trigger, #piModel .model-combo-trigger"
        ).evaluate_all(
            """nodes => nodes.map(node => {
              const style = getComputedStyle(node);
              return [style.backgroundColor, style.color, style.borderColor,
                      style.borderRadius, style.height, style.fontSize].join('|');
            })"""
        )
        assert len(set(signatures)) == 1, f"{theme} settings picker style drift: {signatures}"


def _quota_lines(claude_5h=39, claude_1w=19, codex_5h=0, codex_1w=36) -> list[dict]:
    """The compact rows the backend collapses native buckets down to (#860)."""
    def window(pct, iso):
        return None if pct is None else {"used_percentage": pct, "resets_at": iso}
    return [
        {
            "harness": "claude", "provider": "anthropic", "label": "Claude Code",
            "state": "available", "reason": "native_observation", "stale": False,
            "updated_at": "2026-09-09T17:00:00Z",
            "five_hour": window(claude_5h, "2026-09-09T18:00:00Z"),
            "weekly": window(claude_1w, "2026-09-14T00:00:00Z"),
        },
        {
            "harness": "codex", "provider": "openai", "label": "Codex",
            "state": "available", "reason": "native_observation", "stale": False,
            "updated_at": "2026-09-09T17:00:00Z",
            "five_hour": window(codex_5h, "2026-09-09T21:00:00Z"),
            "weekly": window(codex_1w, "2026-09-16T12:00:00Z"),
        },
    ]


def test_quota_rows_show_both_agents_on_one_line_each(
    authed_page: Page, base_url: str
) -> None:
    """#860: Claude Code above Codex, always both, one nowrap line each.

    The regression this pins is a *display* contract, so it asserts what the
    phone actually shows — order, single-linedness, tier colour, absence of
    the old status dot — not which harness the model picker happens to own.
    """
    lines = _quota_lines()
    board_requests: list[str] = []

    authed_page.add_init_script(
        """
        (payload => {
          const nativeFetch = window.fetch.bind(window);
          window.fetch = function (input, init) {
            const url = new URL(typeof input === 'string' ? input : input.url, location.href);
            if (url.pathname !== '/api/rate-limits') return nativeFetch(input, init);
            return Promise.resolve(new Response(JSON.stringify({quota_lines: payload}), {
              status: 200, headers: {'Content-Type': 'application/json'}
            }));
          };
        })(%s)
        """ % _json.dumps(lines),
    )

    def board_route(route):
        board_requests.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=_json.dumps({
                "generated_at": "2026-09-09T17:00:00Z",
                "columns": {
                    "backlog": [], "claude_turn": [], "your_turn": [],
                    "other": [], "done": [],
                },
                "github": {"fetched_at": "2026-09-09T17:00:00Z", "error": None},
                "sessions_state": {"available": True, "stale": False},
                "active_issues": {"available": True, "count": 0},
                "quota_lines": lines,
            }),
        )

    authed_page.route(re.compile(r".*/api/board(\?.*)?$"), board_route)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    expect(authed_page.locator("#codingModelBtn")).to_be_visible(timeout=5_000)

    coding = authed_page.locator("#codingUsage .quota-line")
    expect(coding).to_have_count(2)
    expect(coding.nth(0)).to_have_attribute("data-harness", "claude", timeout=5_000)
    expect(coding.nth(1)).to_have_attribute("data-harness", "codex")

    expected_5h = authed_page.evaluate(
        """() => new Intl.DateTimeFormat([], {hour: 'numeric', minute: '2-digit'})
             .format(new Date('2026-09-09T18:00:00Z'))"""
    )
    expected_1w = authed_page.evaluate(
        """() => new Intl.DateTimeFormat([], {month: 'short', day: 'numeric'})
             .format(new Date('2026-09-14T00:00:00Z'))"""
    )
    expect(coding.nth(0)).to_have_text(
        "Claude Code · 5h 39% ↻ " + expected_5h + " · 1w 19% ↻ " + expected_1w
    )
    expect(coding.nth(1)).to_contain_text("Codex · 5h 0% ")
    expect(coding.nth(1)).to_contain_text("1w 36% ")

    # One line each is a CSS guarantee, not a lucky width: nowrap makes
    # wrapping impossible, and the row must never widen the page.
    for index in (0, 1):
        expect(coding.nth(index)).to_have_css("white-space", "nowrap")
    # Pseudo-elements are out of reach of to_have_css, so this one read stays
    # raw. Safe under the #680 convention: renderQuotaLines() mutates these two
    # fixed spans in place and never replaceChildren()s the row, so the poll
    # cannot swap the node out from under the read.
    dots = authed_page.evaluate(
        """() => Array.from(document.querySelectorAll('#codingUsage .quota-line'))
             .map(el => getComputedStyle(el, '::before').content)"""
    )
    assert dots == ["none", "none"], f"status dot came back: {dots}"

    # Tier colour rides the line itself, taken from the worse of its windows.
    for index in (0, 1):
        expect(coding.nth(index)).to_have_class("quota-line good")

    # Selecting the other harness changes neither the rows nor their order.
    authed_page.locator("#codingModelBtn").click()
    authed_page.locator(
        "#codingModelMenu button[data-value='codex:gpt-5.6-luna']"
    ).click()
    expect(coding).to_have_count(2)
    expect(coding.nth(0)).to_contain_text("Claude Code")
    expect(coding.nth(1)).to_contain_text("Codex")

    for theme in ("light", "dark"):
        authed_page.locator("html").evaluate(
            "(el, value) => { el.dataset.theme = value; }", theme
        )
        widths = stable_read(lambda: authed_page.locator("body").evaluate(
            "el => el.clientWidth && el.scrollWidth ? [el.clientWidth, el.scrollWidth] : null"
        ))
        assert widths is not None
        assert widths[1] <= widths[0], f"{theme} quota row widens the viewport: {widths}"

    # The Board carries the identical pair, and never a selection-scoped query.
    authed_page.locator("#tabBoard").click()
    board = authed_page.locator("#boardUsage .quota-line")
    expect(board.nth(0)).to_contain_text("Claude Code", timeout=5_000)
    expect(board.nth(1)).to_contain_text("Codex")
    assert board_requests and not any("quota_selection" in u for u in board_requests)


def test_quota_rows_degrade_per_agent_without_collapsing(
    authed_page: Page, base_url: str
) -> None:
    """#860: an unreadable source dims its own row; the other keeps its numbers."""
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    expect(authed_page.locator("#codingModelBtn")).to_be_visible(timeout=5_000)

    hot = _quota_lines(claude_5h=91, claude_1w=64)
    hot[1] = {**hot[1], "state": "error", "five_hour": None, "weekly": None}
    rendered = authed_page.evaluate(
        """async payload => {
          const module = await import('/static/dom-utils.js');
          const root = document.getElementById('codingUsage');
          module.renderQuotaLines(root, payload);
          return Array.from(root.querySelectorAll('.quota-line')).map(el => ({
            hidden: el.hidden, cls: el.className, text: el.textContent,
          }));
        }""",
        hot,
    )
    assert [row["hidden"] for row in rendered] == [False, False]
    # 91% in the 5h window outranks the calmer weekly one.
    assert rendered[0]["cls"] == "quota-line danger"
    assert rendered[0]["text"].startswith("Claude Code · 5h 91% ")
    # Codex has never been measured on this page, so there is nothing to fall
    # back to and the row says so outright.
    assert rendered[1]["cls"] == "quota-line muted"
    assert rendered[1]["text"] == "Codex · quota unavailable"


def test_quota_rows_keep_the_last_reading_when_a_poll_goes_unknown(
    authed_page: Page, base_url: str
) -> None:
    """#860: an expired shard must not blank the numbers you came to read.

    Claude's statusline shard is only rewritten when a session paints, and
    expires after ten minutes, so an idle stretch routinely returns a bare
    ``unknown``. The row keeps its last reading, dimmed and labelled — it
    must never present it as a current, confident value.
    """
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    expect(authed_page.locator("#codingModelBtn")).to_be_visible(timeout=5_000)

    good = _quota_lines()
    gone = [
        {**good[0], "state": "unknown", "reason": "source_absent",
         "five_hour": None, "weekly": None},
        good[1],
    ]
    rendered = authed_page.evaluate(
        """async payloads => {
          const module = await import('/static/dom-utils.js');
          const root = document.getElementById('codingUsage');
          const read = () => Array.from(root.querySelectorAll('.quota-line'))
            .map(el => ({cls: el.className, text: el.textContent,
                         state: el.dataset.state}));
          module.renderQuotaLines(root, payloads.good);
          const before = read();
          module.renderQuotaLines(root, payloads.gone);
          return {before, after: read()};
        }""",
        {"good": good, "gone": gone},
    )
    before, after = rendered["before"], rendered["after"]
    assert before[0]["cls"] == "quota-line good"
    assert "5h 39%" in before[0]["text"] and "stale" not in before[0]["cls"]

    # Same numbers, now dimmed and explicitly not-confirmed.
    assert "5h 39%" in after[0]["text"] and "1w 19%" in after[0]["text"]
    assert after[0]["text"].endswith(" · unknown")
    assert "stale" in after[0]["cls"]
    assert after[0]["state"] == "unknown"
    # The measured agent beside it is untouched.
    assert after[1]["cls"] == "quota-line good"
    assert after[1]["text"].endswith("1w 36% ↻ " + authed_page.evaluate(
        """() => new Intl.DateTimeFormat([], {month: 'short', day: 'numeric'})
             .format(new Date('2026-09-16T12:00:00Z'))"""
    ))


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
          window.__modelPolls = [];
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
              window.__modelPolls.push(url.search);
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
    combo = authed_page.locator("#codingModelCombo")
    trigger = authed_page.locator("#codingModelBtn")
    expect(combo).to_have_attribute("data-value", "claude:sonnet")

    trigger.click()
    authed_page.locator(
        "#codingModelMenu button[data-value='codex:gpt-5.6-luna']"
    ).click()
    expect(combo).to_have_attribute("data-value", "codex:gpt-5.6-luna")
    authed_page.wait_for_function("window.__modelSaves.length === 1")

    # The application's ordinary timers keep running while POST + readback
    # are pending; waiting for a new captured poll exercises the exact
    # stamped module instance imported by main.js, not a fresh import.
    poll_count = authed_page.evaluate("window.__modelPolls.length")
    authed_page.wait_for_function(
        "count => window.__modelPolls.length > count", arg=poll_count,
        timeout=10_000,
    )
    expect(combo).to_have_attribute("data-value", "codex:gpt-5.6-luna")

    # A successful save settles on the persisted Codex choice, and the quota
    # poll keeps asking for both agents rather than the selected one (#860).
    authed_page.evaluate("window.__settleModelSave('ok')")
    authed_page.wait_for_function("window.__modelSaves.length === 0")
    expect(combo).to_have_attribute("data-value", "codex:gpt-5.6-luna")
    poll_count = authed_page.evaluate("window.__modelPolls.length")
    authed_page.wait_for_function(
        "count => window.__modelPolls.length > count", arg=poll_count,
        timeout=10_000,
    )
    assert authed_page.evaluate("window.__modelPolls.at(-1)") == ""

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
