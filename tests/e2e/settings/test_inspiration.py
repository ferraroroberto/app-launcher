"""Settings > Inspiration: the scan's source list, the digest line and the X login.

Hermetic: every /api/inspiration route is mocked, so no test reads or writes the
owner's real source list or opens a login window. Covered: the lists and the
latest-digest lines, adding a source (the PUT carries the revision and the rows
it was given), a refusal showing the server's message in the open form with
nothing saved, removing a row from its kebab behind the confirm sheet, the note,
the login button reporting an open window and then a closed one, and the
unavailable state. The sheet reads the list each time it opens, so one page
walks every state by changing the mock and reopening it.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.ui import open_row_menu, open_settings_sheet

pytestmark = pytest.mark.smoke

CHANNEL = "UC" + "a" * 22


def _state(s: dict) -> dict:
    return {
        "available": True, "reason": "", "exists": True, "editable": True, "problem": "",
        "revision": f"r{s['rev']}", "likes": s["likes"],
        "newsletter": [{"index": i, **e} for i, e in enumerate(s["newsletter"])],
        "youtube": [{"index": i, **e} for i, e in enumerate(s["youtube"])],
        "x": [{"index": i, **e} for i, e in enumerate(s["x"])],
        "digest": {"state": "ok", "date": "2026-11-09", "kept": 4, "items": 31, "chips": [
            {"text": "Newsletters 1/1 read", "ok": True},
            {"text": "X skipped: not logged in (1 of 1)", "ok": False}]},
    }


def _mock(page: Page, s: dict) -> dict:
    """Route-mock the inspiration routes from ``s``; returns the PUT bodies and login POSTs seen."""
    seen: dict = {"puts": [], "logins": 0}

    def _sources(route):
        req = route.request
        if s.get("unavailable"):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"available": False, "reason": s["unavailable"]}))
        elif req.method == "GET":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_state(s)))
        else:
            body = req.post_data_json
            seen["puts"].append(body)
            if s.get("refuse"):
                route.fulfill(status=422, content_type="application/json", body=json.dumps({"detail": s["refuse"]}))
                return
            for key in ("newsletter", "youtube", "x"):
                s[key] = [{k: v for k, v in e.items() if k != "index"} for e in body[key]]
            s["likes"] = body["likes"]
            s["rev"] += 1
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_state(s)))

    def _login(route):
        if route.request.method == "POST":
            seen["logins"] += 1
            s["login"] = "running"
        route.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"state": s["login"], "detail": "", "started_at": 1, "ended_at": None}))

    page.route(re.compile(r".*/api/inspiration/sources$"), _sources)
    page.route(re.compile(r".*/api/inspiration/login-x$"), _login)
    return seen


def _close(page: Page) -> None:
    page.locator("#inspirationSheet [data-sheet-close]").last.click()
    expect(page.locator("#inspirationSheet")).to_be_hidden()


def _group(page: Page, kind: str):
    return page.locator(f'#inspirationSheet .opt-group[data-kind="{kind}"]')


def _login_flow(page: Page, s: dict, seen: dict) -> None:
    """Log into X opens the window and says so; closing it is reported."""
    page.locator("#inspirationLoginBtn").click()
    expect(page.locator("#inspirationLoginStatus")).to_contain_text("Chrome window is open")
    expect(page.locator("#inspirationLoginBtn")).to_be_disabled()
    assert seen["logins"] == 1
    s["login"] = "closed"
    expect(page.locator("#inspirationLoginStatus")).to_contain_text("login window was closed", timeout=6000)
    expect(page.locator("#inspirationLoginBtn")).to_be_enabled()


def _unavailable_state(page: Page, s: dict) -> None:
    """The reason, no lists, and the row says Unavailable."""
    s["unavailable"] = "This fleet-config checkout has no inspiration-scan skill yet."
    open_settings_sheet(page, "inspirationSheet")
    expect(page.locator("#inspirationReason")).to_have_text(s["unavailable"])
    expect(page.locator("#inspirationContent")).to_be_hidden()
    _close(page)
    expect(page.locator('[data-settings-value="inspiration"]')).to_have_text("Unavailable")


def test_inspiration_lists_edits_refuses_removes_and_logs_into_x(authed_page: Page, base_url: str) -> None:
    page = authed_page
    s: dict = {"rev": 1, "likes": "small tools", "login": "idle",
               "newsletter": [{"name": "Letter One", "feed": "https://one.test/feed"}],
               "youtube": [{"name": "Channel One", "channel_id": CHANNEL}],
               "x": [{"handle": "someone"}]}
    seen = _mock(page, s)
    page.goto(base_url, wait_until="domcontentloaded")
    open_settings_sheet(page, "inspirationSheet")

    # The lists, the note and the digest status.
    expect(page.locator("#inspirationDigest")).to_have_text("2026-11-09 · 4 ideas kept from 31 items")
    expect(page.locator("#inspirationDigestLines li")).to_have_text(
        ["Newsletters 1/1 read", "X skipped: not logged in (1 of 1)"])
    expect(page.locator("#inspirationLikes")).to_have_value("small tools")
    expect(_group(page, "newsletter").locator(".action-row")).to_have_count(1)
    expect(_group(page, "x").locator(".action-row-title")).to_have_text("@someone")
    _close(page)
    expect(page.locator('[data-settings-value="inspiration"]')).to_have_text("3 sources")

    # Adding sends the whole list with the revision the sheet loaded.
    open_settings_sheet(page, "inspirationSheet")
    news = _group(page, "newsletter")
    news.locator("[data-add]").click()
    news.locator('input[name="name"]').fill("Letter Two")
    news.locator('input[name="feed"]').fill("https://two.test/rss")
    news.locator("[data-form] button[type=submit]").click()
    expect(page.locator("#toast")).to_contain_text("Saved")
    expect(news.locator(".action-row")).to_have_count(2)
    sent = seen["puts"][-1]
    assert sent["revision"] == "r1" and sent["likes"] == "small tools"
    assert sent["newsletter"] == [
        {"index": 0, "name": "Letter One", "feed": "https://one.test/feed"},
        {"name": "Letter Two", "feed": "https://two.test/rss"}]
    assert s["newsletter"][1]["feed"] == "https://two.test/rss"

    # A refusal keeps the form open under the server's own message; nothing is saved.
    s["refuse"] = "'bad handle' is not an X handle."
    x = _group(page, "x")
    x.locator("[data-add]").click()
    x.locator('input[name="handle"]').fill("bad handle")
    x.locator("[data-form] button[type=submit]").click()
    expect(x.locator("[data-error]")).to_have_text("'bad handle' is not an X handle.")
    expect(x.locator("[data-form]")).to_be_visible()
    assert len(s["x"]) == 1
    x.locator("[data-cancel]").click()
    expect(x.locator("[data-form]")).to_be_hidden()
    s["refuse"] = ""

    # Editing a row opens the form filled in.
    _group(page, "youtube").locator(".action-row-main").click()
    expect(_group(page, "youtube").locator('input[name="channel_id"]')).to_have_value(CHANNEL)
    _group(page, "youtube").locator("[data-cancel]").click()

    # Remove sits in the kebab, behind the confirm sheet; its ✕ is a "no".
    news.locator(".action-row", has_text="Letter Two").locator(".action-row-kebab").click()
    open_row_menu(page).get_by_role("menuitem", name="Remove").click()
    confirm = page.locator("#confirmDialog")
    expect(confirm).to_be_visible()
    confirm.locator("#confirmDialogClose").click()
    expect(confirm).to_be_hidden()
    assert len(s["newsletter"]) == 2
    news.locator(".action-row", has_text="Letter Two").locator(".action-row-kebab").click()
    open_row_menu(page).get_by_role("menuitem", name="Remove").click()
    confirm.locator("#confirmDialogOk").click()
    expect(news.locator(".action-row")).to_have_count(1)
    assert [e["name"] for e in s["newsletter"]] == ["Letter One"]

    # The note saves with its own button.
    page.locator("#inspirationLikes").fill("agents I can leave running")
    page.locator("#inspirationLikesSave").click()
    expect(page.locator("#toast")).to_contain_text("Note saved")
    assert s["likes"] == "agents I can leave running"

    _login_flow(page, s, seen)
    _close(page)

    _unavailable_state(page, s)
