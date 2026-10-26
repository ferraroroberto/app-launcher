"""Settings › Telegram › Bots: the group of bots and each bot's own sheet."""

from __future__ import annotations

import json as _json
import re
from typing import Any, Dict

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.ui import open_settings_sheet
from tests.e2e.life_os._support import _mock_skills


pytestmark = pytest.mark.smoke


def _profile(pid: str, label: str, **over: Any) -> Dict[str, Any]:
    return {
        "id": pid, "label": label, "skill": "journal-daily", "relay": True,
        "resume_offer": True, "idle_close_minutes": 60, "model": "",
        "skill_found": True, "env_present": True, "running": True,
        "session_id": f"s-{pid}", "restart_due": False,
        "restart_needed": False, "restart_fields": [], **over,
    }


def _mock_bots(page: Page) -> Dict[str, Any]:
    """Serve the channels list from a mutable payload; a PATCH edits it, a
    refusal-marked field answers 400, and every write is recorded."""
    store: Dict[str, Any] = {
        "profiles": [
            _profile("health", "Health"),
            _profile("house", "House", restart_needed=True,
                     restart_fields=["relay", "model"]),
            _profile("job", "Job", relay=False, running=False,
                     restart_needed=None),
        ],
        "problems": [],
        "setup": {
            "file_present": True, "life_os_found": True, "bun": True,
            "plugin": True,
            "skills": [{"id": "journal-daily", "name": "Journal Daily"},
                       {"id": "sparring-work", "name": "Sparring Work"}],
        },
        "patches": [], "restarts": [], "refuse": None,
    }

    def _list(route):
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps({k: store[k] for k in
                                        ("profiles", "problems", "setup")}))

    def _patch(route):
        pid = route.request.url.rsplit("/", 1)[1]
        body = _json.loads(route.request.post_data or "{}")
        store["patches"].append({"id": pid, "body": body})
        if store["refuse"]:
            route.fulfill(status=400, content_type="application/json",
                          body=_json.dumps({"detail": store["refuse"]}))
            return
        profile = next(p for p in store["profiles"] if p["id"] == pid)
        profile.update(body)
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps({"profile": profile, "changed": list(body)}))

    def _restart(route):
        pid = route.request.url.split("/channels/")[1].split("/")[0]
        store["restarts"].append(pid)
        profile = next(p for p in store["profiles"] if p["id"] == pid)
        profile.update(restart_needed=False, restart_fields=[])
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps({"launched": profile["skill"], "profile": pid,
                                        "restarted": True, "session": None}))

    page.route(re.compile(r".*/api/life-os/channels$"), _list)
    page.route(re.compile(r".*/api/life-os/channels/[^/]+$"),
               lambda r: _patch(r) if r.request.method == "PATCH" else r.fallback())
    page.route(re.compile(r".*/api/life-os/channels/[^/]+/restart$"), _restart)
    return store


def _open_bots(page: Page, base_url: str) -> Dict[str, Any]:
    _mock_skills(page)
    store = _mock_bots(page)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabLifeOS").click()
    open_settings_sheet(page, "channelsSheet")
    return store


def _bot_row(page: Page, pid: str):
    return page.locator(f'#channelBots [data-bot-id="{pid}"]')


def test_bots_group_lists_every_bot_and_flags_only_a_known_restart(
    authed_page: Page, base_url: str
) -> None:
    """Each bot is a row opening its own sheet. The chip shows only for a bot
    that is known to need a restart: false and null (unknown) show nothing."""
    page = authed_page
    _open_bots(page, base_url)
    expect(page.locator("#channelBots .bot-row")).to_have_count(3)
    expect(_bot_row(page, "health")).to_contain_text("Relay · closes after 1 h idle")
    expect(_bot_row(page, "job")).to_contain_text("Not running · relay off")
    expect(_bot_row(page, "house").locator(".channel-check-chip")).to_have_text(
        "Restart to apply")
    expect(_bot_row(page, "health").locator(".channel-check-chip")).to_have_count(0)
    expect(_bot_row(page, "job").locator(".channel-check-chip")).to_have_count(0)
    expect(_bot_row(page, "job").locator(".bot-avatar-badge")).to_have_count(0)
    expect(_bot_row(page, "health").locator(".bot-avatar-badge")).to_have_count(1)
    _bot_row(page, "house").click()
    expect(page.locator("#channelBotSheet")).to_be_visible()
    expect(page.locator("#channelBotSheetTitle")).to_have_text("House")
    # Back returns to the Telegram sheet, not out of Settings.
    page.locator("#channelBotSheet [data-sheet-back]").click()
    expect(page.locator("#channelsSheet")).to_be_visible()


def test_bot_fields_save_through_patch_and_survive_a_reload(
    authed_page: Page, base_url: str
) -> None:
    """Every field PATCHes as it changes, toasts, and reads back from the server."""
    page = authed_page
    store = _open_bots(page, base_url)
    _bot_row(page, "health").click()
    name = page.locator("#botNameInput")
    name.fill("Wellbeing")
    name.blur()
    expect(page.locator("#toast")).to_contain_text("Name saved")
    page.locator("#botSkillSelect").select_option("sparring-work")
    page.locator("#botModelSelect").select_option("opus")
    page.locator("#botIdleSeg [data-value='120']").click()
    page.locator("#botResumeToggle").click()
    expect(page.locator("#botResumeToggle")).to_have_attribute("aria-checked", "false")
    sent = [p["body"] for p in store["patches"]]
    assert sent == [
        {"label": "Wellbeing"}, {"skill": "sparring-work"}, {"model": "opus"},
        {"idle_close_minutes": 120}, {"resume_offer": False},
    ]
    page.reload(wait_until="domcontentloaded")
    page.locator("#tabLifeOS").click()
    open_settings_sheet(page, "channelsSheet")
    expect(_bot_row(page, "health")).to_contain_text("Wellbeing")
    expect(_bot_row(page, "health")).to_contain_text("closes after 2 h idle")


def test_resume_offer_and_idle_close_wait_for_relay(
    authed_page: Page, base_url: str
) -> None:
    """With Relay off the resume switch and the idle control are disabled and
    say why; turning Relay on enables them."""
    page = authed_page
    _open_bots(page, base_url)
    _bot_row(page, "job").click()
    expect(page.locator("#botRelayToggle")).to_have_attribute("aria-checked", "false")
    expect(page.locator("#botResumeToggle")).to_be_disabled()
    expect(page.locator("#botIdleSeg .segmented-item").first).to_be_disabled()
    expect(page.locator("#botResumeHelp")).to_have_text("Needs Relay on.")
    page.locator("#botRelayToggle").click()
    expect(page.locator("#botResumeToggle")).to_be_enabled()
    expect(page.locator("#botIdleSeg .segmented-item").first).to_be_enabled()


def test_restart_is_offered_only_when_needed_and_runs_on_a_confirmed_tap(
    authed_page: Page, base_url: str
) -> None:
    """The banner names the changed fields; Restart posts nothing until the
    themed confirm is accepted; an unknown (null) bot shows neither."""
    page = authed_page
    store = _open_bots(page, base_url)
    _bot_row(page, "job").click()
    expect(page.locator("#botRestartBanner")).to_be_hidden()
    expect(page.locator("#botRestartBtn")).to_be_hidden()
    page.locator("#channelBotSheet [data-sheet-back]").click()
    _bot_row(page, "house").click()
    expect(page.locator("#botRestartBanner")).to_contain_text("Relay and model changed")
    page.locator("#botRestartBtn").click()
    expect(page.locator("#confirmDialog")).to_be_visible()
    page.locator("#confirmDialogClose").click()
    assert store["restarts"] == []
    page.locator("#botRestartBtn").click()
    page.locator("#confirmDialogOk").click()
    expect(page.locator("#botRestartBanner")).to_be_hidden()
    assert store["restarts"] == ["house"]


def test_a_refused_save_shows_the_servers_message_under_the_field(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    store = _open_bots(page, base_url)
    store["refuse"] = "skill is not a life-os skill"
    _bot_row(page, "health").click()
    page.locator("#botSkillSelect").select_option("sparring-work")
    expect(page.locator("#botSkillError")).to_have_text("skill is not a life-os skill")
    # The control goes back to what is stored, never the refused value.
    expect(page.locator("#botSkillSelect")).to_have_value("journal-daily")


def test_bot_sheet_carries_no_path_or_token(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _open_bots(page, base_url)
    _bot_row(page, "health").click()
    expect(page.locator("#channelBotSheet")).not_to_contain_text(
        re.compile(r"[A-Za-z]:[\\/]|TELEGRAM_BOT_TOKEN|\d{6,}:[\w-]{20,}")
    )
    expect(page.locator("#botStatus")).to_contain_text("Token file")
