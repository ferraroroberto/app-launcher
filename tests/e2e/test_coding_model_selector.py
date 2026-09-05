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


def _quota_payload(harness: str, state: str = "available") -> dict:
    label = "Codex" if harness == "codex" else "Claude"
    if state in {"unknown", "unsupported", "error"}:
        observations = []
    elif harness == "codex":
        observations = [
            {
                "bucket": "default", "state": state, "shared_account": False,
                "windows": [{
                    "id": "primary", "duration_minutes": 10080,
                    "used_percentage": 0, "resets_at": "2026-09-09T18:00:00Z",
                    "state": state,
                }],
            },
            {
                "bucket": "reserve", "state": state, "shared_account": False,
                "windows": [{
                    "id": "secondary", "duration_minutes": 1440,
                    "used_percentage": 64, "resets_at": None, "state": state,
                }],
            },
            {
                "bucket": "spark-custom", "state": "unknown", "shared_account": False,
                "windows": [{
                    "id": "primary", "duration_minutes": 300,
                    "used_percentage": None, "resets_at": None, "state": "unknown",
                }],
            },
        ]
    else:
        observations = [{
            "bucket": "claude-code", "state": state, "shared_account": False,
            "windows": [{
                "id": "five_hour", "duration_minutes": 300,
                "used_percentage": 42, "resets_at": "2026-09-09T18:00:00Z",
                "state": state,
            }],
        }]
    return {
        "schema_version": 1, "harness": harness,
        "provider": "openai" if harness == "codex" else "anthropic",
        "label": label, "state": state, "reason": state,
        "checked_at": "2026-09-09T17:00:01Z", "observations": observations,
        "available": state == "available", "stale": state == "stale",
        "updated_at": "2026-09-09T17:00:00Z",
        "five_hour": None, "seven_day": None,
    }


def test_selected_provider_quota_states_switch_without_overflow(
    authed_page: Page, base_url: str
) -> None:
    """#847: selected-source badges survive every state and a late response."""
    codex = _quota_payload("codex")
    claude = _quota_payload("claude")
    board_requests: list[str] = []

    authed_page.add_init_script(
        """
        (payloads => {
          const claude = payloads.claude;
          const codex = payloads.codex;
          const nativeFetch = window.fetch.bind(window);
          window.fetch = function (input, init) {
            const url = new URL(typeof input === 'string' ? input : input.url, location.href);
            if (url.pathname !== '/api/rate-limits') return nativeFetch(input, init);
            const selection = url.searchParams.get('quota_selection') || 'claude:sonnet';
            const isCodex = selection.startsWith('codex:');
            const delay = window.__quotaRace && !isCodex ? 250 : 5;
            const payload = isCodex ? codex : claude;
            return new Promise(resolve => setTimeout(() => resolve(new Response(
              JSON.stringify(payload), {status: 200, headers: {'Content-Type': 'application/json'}}
            )), delay));
          };
        })(%s)
        """ % _json.dumps({"claude": claude, "codex": codex}),
    )

    def board_route(route):
        selection = parse_qs(urlparse(route.request.url).query).get(
            "quota_selection", ["claude:sonnet"]
        )[0]
        board_requests.append(selection)
        quota = codex if selection.startswith("codex:") else claude
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
                "rate_limits": quota,
            }),
        )

    authed_page.route(re.compile(r".*/api/board\?.*"), board_route)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    expect(authed_page.locator("#codingModelBtn")).to_be_visible(timeout=5_000)

    # Two overlapping requests reproduce the provider-switch race. The late
    # Claude response must not overwrite the newer Codex selection.
    authed_page.evaluate(
        """async () => {
          window.__quotaRace = true;
          const module = await import('/static/sessions.js');
          await Promise.all([
            module.fetchRateLimits('claude:sonnet'),
            module.fetchRateLimits('codex:gpt-5.6-luna'),
          ]);
          window.__quotaRace = false;
        }"""
    )
    coding = authed_page.locator("#codingUsage")
    expect(coding).to_have_attribute("data-harness", "codex")
    expect(coding).to_contain_text("Codex · Default · Primary · 1w · 0% used")
    expect(coding).to_contain_text("Reserve · Secondary · 1d · 64% used")
    expect(coding).to_contain_text("Spark custom · Primary · 5h · usage unknown")
    expect(coding).not_to_contain_text("Claude")
    expected_local = authed_page.evaluate(
        """() => new Intl.DateTimeFormat([], {
          month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'
        }).format(new Date('2026-09-09T18:00:00Z'))"""
    )
    expect(coding).to_contain_text("resets " + expected_local)

    # The same renderer keeps state words distinct from a measured zero.
    for source_state, expected in (
        ("unknown", "Codex quota unknown"),
        ("unsupported", "Codex quota unsupported"),
        ("error", "Codex quota unavailable"),
    ):
        authed_page.evaluate(
            """async payload => {
              const module = await import('/static/dom-utils.js');
              module.renderUsageBadgeRow(
                document.getElementById('codingUsage'),
                document.getElementById('codingUsageSession'),
                document.getElementById('codingUsageWeekly'), payload
              );
            }""",
            _quota_payload("codex", source_state),
        )
        expect(coding).to_have_attribute("data-state", source_state)
        expect(coding).to_have_text(expected)

    authed_page.evaluate(
        """async payload => {
          const module = await import('/static/dom-utils.js');
          module.renderUsageBadgeRow(
            document.getElementById('codingUsage'),
            document.getElementById('codingUsageSession'),
            document.getElementById('codingUsageWeekly'), payload
          );
        }""",
        _quota_payload("codex", "stale"),
    )
    expect(coding).to_have_attribute("data-state", "stale")
    expect(coding).to_contain_text("0% used")
    expect(coding).to_contain_text("stale")

    # Both authored themes retain a wrapping row with no page overflow.
    for theme in ("light", "dark"):
        authed_page.locator("html").evaluate("(el, value) => { el.dataset.theme = value; }", theme)
        widths = stable_read(lambda: authed_page.locator("body").evaluate(
            "el => el.clientWidth && el.scrollWidth ? [el.clientWidth, el.scrollWidth] : null"
        ))
        assert widths is not None
        assert widths[1] <= widths[0], f"{theme} quota row widens the viewport: {widths}"

    # Board follows its own dispatch-model control, independently of Coding.
    authed_page.locator("#tabBoard").click()
    board_select = authed_page.locator("#boardDispatchModel")
    board_select.select_option("codex:gpt-5.6-luna")
    board_usage = authed_page.locator("#boardUsage")
    expect(board_usage).to_have_attribute("data-harness", "codex", timeout=5_000)
    expect(board_usage).to_contain_text("0% used")
    board_select.select_option("claude:sonnet")
    expect(board_usage).to_have_attribute("data-harness", "claude", timeout=5_000)
    expect(board_usage).not_to_contain_text("Codex")
    assert "codex:gpt-5.6-luna" in board_requests
    assert "claude:sonnet" in board_requests


def test_quota_selection_owns_polls_until_config_save_settles(
    authed_page: Page, base_url: str
) -> None:
    """#847: a periodic poll cannot race a pending provider config save."""
    config = _config("sonnet", "claude:sonnet")
    payloads = {
        "claude": _quota_payload("claude"),
        "codex": _quota_payload("codex"),
    }
    authed_page.add_init_script(
        """
        (fixture => {
          const nativeFetch = window.fetch.bind(window);
          let config = fixture.config;
          const saves = [];
          const reads = [];
          let delayNextConfigRead = false;
          window.__quotaPolls = [];
          window.__quotaSaves = saves;
          window.__quotaReads = reads;
          window.__delayNextConfigRead = function () {
            delayNextConfigRead = true;
          };
          window.__settleQuotaRead = function () {
            const read = reads.shift();
            if (!read) throw new Error('no pending config read');
            read.resolve(read.response);
          };
          window.__settleQuotaSave = function (outcome) {
            const save = saves.shift();
            if (!save) throw new Error('no pending quota save');
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
              const selection = url.searchParams.get('quota_selection') || 'claude:sonnet';
              window.__quotaPolls.push(selection);
              const payload = selection.startsWith('codex:')
                ? fixture.payloads.codex : fixture.payloads.claude;
              return Promise.resolve(new Response(JSON.stringify(payload), {
                status: 200, headers: {'Content-Type': 'application/json'}
              }));
            }
            return nativeFetch(input, init);
          };
        })(%s)
        """ % _json.dumps({"config": config, "payloads": payloads}),
    )
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    combo = authed_page.locator("#codingModelCombo")
    trigger = authed_page.locator("#codingModelBtn")
    coding = authed_page.locator("#codingUsage")
    expect(combo).to_have_attribute("data-value", "claude:sonnet")

    trigger.click()
    authed_page.locator(
        "#codingModelMenu button[data-value='codex:gpt-5.6-luna']"
    ).click()
    expect(combo).to_have_attribute("data-value", "codex:gpt-5.6-luna")
    expect(coding).to_have_attribute("data-harness", "codex")
    authed_page.wait_for_function("window.__quotaSaves.length === 1")

    # The application's ordinary timer calls with no override while POST +
    # readback are pending. It must follow the synchronous control, not stale
    # state.config. Waiting for a new captured request exercises the exact
    # stamped module instance imported by main.js.
    poll_count = authed_page.evaluate("window.__quotaPolls.length")
    authed_page.wait_for_function(
        "count => window.__quotaPolls.length > count", arg=poll_count,
        timeout=10_000,
    )
    assert authed_page.evaluate("window.__quotaPolls.at(-1)").startswith("codex:")
    expect(combo).to_have_attribute("data-value", "codex:gpt-5.6-luna")
    expect(coding).to_have_attribute("data-harness", "codex")

    # A successful save settles on the persisted Codex choice, and subsequent
    # timer polls continue to use it after the pending owner is released.
    authed_page.evaluate("window.__settleQuotaSave('ok')")
    authed_page.wait_for_function("window.__quotaSaves.length === 0")
    expect(combo).to_have_attribute("data-value", "codex:gpt-5.6-luna")
    poll_count = authed_page.evaluate("window.__quotaPolls.length")
    authed_page.wait_for_function(
        "count => window.__quotaPolls.length > count", arg=poll_count,
        timeout=10_000,
    )
    assert authed_page.evaluate("window.__quotaPolls.at(-1)").startswith("codex:")

    # A newer rapid selection can arrive after an older POST starts its config
    # readback. Even if that GET captured the older persisted value, releasing
    # it cannot repaint or take quota ownership from the newer selection.
    trigger.click()
    authed_page.locator(
        "#codingModelMenu button[data-value='claude:fable']"
    ).click()
    authed_page.wait_for_function("window.__quotaSaves.length === 1")
    authed_page.evaluate("window.__delayNextConfigRead()")
    authed_page.evaluate("window.__settleQuotaSave('ok')")
    authed_page.wait_for_function("window.__quotaReads.length === 1")
    trigger.click()
    authed_page.locator(
        "#codingModelMenu button[data-value='codex:gpt-5.6-luna']"
    ).click()
    expect(combo).to_have_attribute("data-value", "codex:gpt-5.6-luna")
    expect(coding).to_have_attribute("data-harness", "codex")
    authed_page.evaluate("window.__settleQuotaRead()")
    authed_page.wait_for_function("window.__quotaSaves.length === 1")
    expect(combo).to_have_attribute("data-value", "codex:gpt-5.6-luna")
    expect(coding).to_have_attribute("data-harness", "codex")
    authed_page.evaluate("window.__settleQuotaSave('ok')")
    authed_page.wait_for_function("window.__quotaSaves.length === 0")
    expect(combo).to_have_attribute("data-value", "codex:gpt-5.6-luna")
    expect(coding).to_have_attribute("data-harness", "codex")

    # A failed older save cannot repaint a newer selection while its persisted
    # readback is delayed. If the newest save then fails too, both the control
    # and badge reconcile explicitly to persisted server truth.
    trigger.click()
    authed_page.locator(
        "#codingModelMenu button[data-value='claude:fable']"
    ).click()
    expect(combo).to_have_attribute("data-value", "claude:fable")
    expect(coding).to_have_attribute("data-harness", "claude")
    authed_page.wait_for_function("window.__quotaSaves.length === 1")
    authed_page.evaluate("window.__delayNextConfigRead()")
    authed_page.evaluate("window.__settleQuotaSave('fail')")
    authed_page.wait_for_function("window.__quotaReads.length === 1")
    trigger.click()
    authed_page.locator(
        "#codingModelMenu button[data-value='claude:sonnet']"
    ).click()
    expect(combo).to_have_attribute("data-value", "claude:sonnet")
    expect(coding).to_have_attribute("data-harness", "claude")
    authed_page.evaluate("window.__settleQuotaRead()")
    authed_page.wait_for_function("window.__quotaSaves.length === 1")
    expect(combo).to_have_attribute("data-value", "claude:sonnet")
    expect(coding).to_have_attribute("data-harness", "claude")
    authed_page.evaluate("window.__settleQuotaSave('fail')")
    expect(combo).to_have_attribute("data-value", "codex:gpt-5.6-luna")
    expect(coding).to_have_attribute("data-harness", "codex")
