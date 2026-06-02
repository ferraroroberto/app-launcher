"""Life OS tab e2e (issue #102).

Browser-side coverage: the tab renders skill tiles from
``/api/life-os/skills``, the ``opus`` + ``☁️ Detached`` toggles are wired,
and tapping launch POSTs ``/api/life-os/skills/<id>/launch`` with the
toggle state — proving the bare ``/skill`` launch path is reached with
the right model/mode. Hermetic via route-mocks, like the Jobs e2e tests.

The server-side security (Cloudflare refusal, Tailscale gate, path-jail)
is covered by the in-process pytest API suite (tests/test_webapp_api_life_os.py),
which can set client headers/host directly — over loopback the e2e
browser bypasses the gate entirely, so those checks belong there.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

_FAKE_SKILLS = {
    "available": True,
    "life_os_dir": "E:/automation/life-os",
    "skills": [
        {
            "id": "journal-daily",
            "name": "journal-daily",
            "command": "journal-daily",
            "description": "Turns a transcript into a journal.",
            "skill_md": ".claude/skills/journal-daily/SKILL.md",
        },
        {
            "id": "sparring-work",
            "name": "sparring-work",
            "command": "sparring-work",
            "description": "Sparring partner for work relationships.",
            "skill_md": ".claude/skills/sparring-work/SKILL.md",
        },
    ],
}


def _mock_skills(page: Page) -> None:
    page.route(
        re.compile(r".*/api/life-os/skills(\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(_FAKE_SKILLS),
        ),
    )


def test_life_os_tab_renders_skill_tiles(authed_page: Page, base_url: str) -> None:
    _mock_skills(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#tabLifeOS", state="attached", timeout=5_000)
    authed_page.locator("#tabLifeOS").click()

    expect(authed_page.locator("#paneLifeOS")).to_be_visible()
    tiles = authed_page.locator("#lifeOsList li.lifeos-item")
    expect(tiles.first).to_be_visible(timeout=5_000)
    assert tiles.count() == 2
    expect(tiles.first).to_contain_text("journal-daily")
    # The opus + Detached toggles live in the options summary.
    expect(authed_page.locator("#lifeOsOpus")).to_be_attached()
    expect(authed_page.locator("#lifeOsDetached")).to_be_attached()


def test_life_os_launch_posts_mode_and_opus(
    authed_page: Page, base_url: str
) -> None:
    _mock_skills(authed_page)

    captured: dict = {}

    def _capture_launch(route):
        captured["body"] = route.request.post_data or ""
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "launched": "journal-daily", "name": "journal-daily",
                "agent": "claude", "mode": "remote", "opus": True,
                "session": {"session_id": "x", "kind": "remote"},
            }),
        )

    authed_page.route(
        re.compile(r".*/api/life-os/skills/journal-daily/launch$"),
        _capture_launch,
    )

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()
    expect(authed_page.locator("#lifeOsList li.lifeos-item").first).to_be_visible(
        timeout=5_000
    )

    # Flip opus on + Detached on (so it launches detached → no terminal
    # overlay / WS to deal with in the assertion).
    authed_page.evaluate("document.getElementById('lifeOsOpus').checked = true")
    authed_page.evaluate(
        "document.getElementById('lifeOsDetached').checked = true"
    )

    tile = authed_page.locator(
        "#lifeOsList li.lifeos-item[data-id='journal-daily']"
    )
    tile.locator(".lifeos-launch").click()

    # Wait for the launch route to capture the POST body.
    authed_page.wait_for_timeout(400)
    assert "body" in captured, "launch POST was never intercepted"
    payload = _json.loads(captured["body"])
    assert payload == {"mode": "remote", "opus": True}, payload


def test_life_os_browser_full_screen_doc_toggle(
    authed_page: Page, base_url: str
) -> None:
    """📖 Browse shows a full-screen file list; tapping a file opens it
    full-screen with a ✕ close-doc button that's hidden until then, and ✕
    returns to the list. Hermetic — /files + /file are route-mocked."""
    _mock_skills(authed_page)
    authed_page.route(
        re.compile(r".*/api/life-os/skills/journal-daily/files$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "skill": {"id": "journal-daily", "name": "journal-daily"},
                "files": [
                    {"path": ".claude/skills/journal-daily/SKILL.md",
                     "name": "SKILL.md", "category": "skill"},
                    {"path": ".claude/skills/journal-daily/memory/observations.md",
                     "name": "memory/observations.md", "category": "memory"},
                ],
            }),
        ),
    )
    authed_page.route(
        re.compile(r".*/api/life-os/file\?.*$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "path": "x", "name": "SKILL.md",
                "content": "# Heading\n\nbody text", "truncated": False,
            }),
        ),
    )

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()
    tile = authed_page.locator(
        "#lifeOsList li.lifeos-item[data-id='journal-daily']"
    )
    expect(tile).to_be_visible(timeout=5_000)
    tile.locator("button:has-text('📖')").click()

    # File list full-screen; content layer + ✕ hidden.
    expect(authed_page.locator("#lifeOsBrowser")).to_be_visible()
    expect(authed_page.locator(".lifeos-file-btn").first).to_be_visible(
        timeout=5_000
    )
    expect(authed_page.locator("#lifeOsFileContent")).to_be_hidden()
    expect(authed_page.locator("#lifeOsDocClose")).to_be_hidden()

    # Open a file → content + ✕ visible.
    authed_page.locator(".lifeos-file-btn").first.click()
    expect(authed_page.locator("#lifeOsFileContent")).to_be_visible()
    expect(authed_page.locator("#lifeOsFileContent")).to_contain_text(
        "body text"
    )
    expect(authed_page.locator("#lifeOsDocClose")).to_be_visible()

    # ✕ closes the doc → back to the list, ✕ hidden again.
    authed_page.locator("#lifeOsDocClose").click()
    expect(authed_page.locator("#lifeOsFileContent")).to_be_hidden()
    expect(authed_page.locator("#lifeOsDocClose")).to_be_hidden()


def test_life_os_delete_conversation_log_from_doc_toolbar(
    authed_page: Page, base_url: str
) -> None:
    """🗑️ never appears in the browse list; it shows in the document toolbar
    only when the open file is a conversation log. Confirming DELETEs and
    returns to the list, which reloads without the log. Hermetic — /files
    reload drops the log on the 2nd call, DELETE is mocked."""
    _mock_skills(authed_page)

    calls = {"n": 0}

    def _files(route):
        calls["n"] += 1
        convs = [] if calls["n"] > 1 else [{
            "path": ".claude/skills/journal-daily/conversations/trial.md",
            "name": "conversations/trial.md", "category": "conversations",
        }]
        files = convs + [{
            "path": ".claude/skills/journal-daily/memory/observations.md",
            "name": "memory/observations.md", "category": "memory",
        }]
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"skill": {"id": "journal-daily",
                              "name": "journal-daily"}, "files": files}),
        )

    deleted = {"hit": False}

    def _file(route):
        # GET returns content; DELETE records the hit. Same path, two verbs.
        if route.request.method == "DELETE":
            deleted["hit"] = True
            route.fulfill(status=200, content_type="application/json",
                          body=_json.dumps({"deleted": "x"}))
        else:
            route.fulfill(status=200, content_type="application/json",
                          body=_json.dumps({"path": "x", "name": "trial.md",
                                            "content": "log body",
                                            "truncated": False}))

    authed_page.route(
        re.compile(r".*/api/life-os/skills/journal-daily/files$"), _files
    )
    authed_page.route(
        re.compile(r".*/api/life-os/file\?.*$"), _file
    )
    authed_page.on("dialog", lambda d: d.accept())

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()
    authed_page.locator(
        "#lifeOsList li.lifeos-item[data-id='journal-daily'] button:has-text('📖')"
    ).click()

    # No delete control anywhere in the list, and the toolbar 🗑️ stays hidden.
    expect(authed_page.locator(".lifeos-file-btn").first).to_be_visible(
        timeout=5_000
    )
    expect(authed_page.locator(".lifeos-file-del")).to_have_count(0)
    expect(authed_page.locator("#lifeOsDocDelete")).to_be_hidden()

    # Open the memory file → 🗑️ stays hidden (not a conversation log).
    authed_page.locator(
        ".lifeos-file-btn:has-text('memory/observations.md')"
    ).click()
    expect(authed_page.locator("#lifeOsFileContent")).to_be_visible()
    expect(authed_page.locator("#lifeOsDocDelete")).to_be_hidden()
    authed_page.locator("#lifeOsDocClose").click()

    # Open the conversation log → 🗑️ appears in the bar.
    authed_page.locator(
        ".lifeos-file-btn:has-text('conversations/trial.md')"
    ).click()
    expect(authed_page.locator("#lifeOsFileContent")).to_be_visible()
    expect(authed_page.locator("#lifeOsDocDelete")).to_be_visible()

    # Confirm delete → DELETE fires, doc closes back to the list, log gone.
    authed_page.locator("#lifeOsDocDelete").click()
    authed_page.wait_for_timeout(400)
    assert deleted["hit"], "DELETE /api/life-os/file was never called"
    expect(authed_page.locator("#lifeOsFileContent")).to_be_hidden()
    expect(authed_page.locator("#lifeOsDocDelete")).to_be_hidden()
    expect(
        authed_page.locator(".lifeos-file-btn:has-text('conversations/trial.md')")
    ).to_have_count(0)
