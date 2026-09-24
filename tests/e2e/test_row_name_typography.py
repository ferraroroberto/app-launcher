"""Regression pin for #1126 — row names, helper copy and label casing.

A rendered UX audit found project, skill and app names breaking *inside*
words on the phone ("gitlab-to-github-migrato / r"): eight rules carried
``word-break: break-all``, which breaks anywhere even when a space is
available. Names now render on one line, truncated with an ellipsis and
never broken, and the full on-disk name rides ``title``. Paths, output tails
and meta lines keep wrapping, now via ``overflow-wrap: anywhere``, which only
breaks a word that can't fit on a line by itself.

The same audit found the helper notes set in italic at caption size (the
least legible text in the app) and mixed casing ("git / bot / you" beside
"Other / Done"; an "options" card beside "Settings"). Both are pinned here.

A name's line count is read from its text node's line boxes, so the check is
"did this text wrap", independent of font metrics. Content is synthetic.
"""
from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import stable_read

pytestmark = pytest.mark.smoke

_PROJECT = "gitlab-to-github-migrator-for-the-closed-company-accounting-archive"
_BRANCH = "feat/1126-a-rather-long-branch-name-for-the-tag"
_APP = "Closed company account reconciliation dashboard with a long title"
_SKILL = "closed-company-accounting-archive-weekly-reconciliation"

# Line boxes of the first non-empty text node under `sel`, plus the title the
# name carries. Null while the element isn't laid out yet (`stable_read`).
_LINES = """
(sel) => {
  const el = document.querySelector(sel);
  if (!el || !el.getClientRects().length) return null;
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  let node = walker.nextNode();
  while (node && !node.textContent.trim()) node = walker.nextNode();
  if (!node) return null;
  const range = document.createRange();
  range.selectNodeContents(node);
  const tops = new Set(
    Array.from(range.getClientRects())
      .filter((r) => r.width > 0)
      .map((r) => Math.round(r.top))
  );
  return { lines: tops.size, title: el.getAttribute('title') || '' };
}
"""


def _json_route(page: Page, pattern, body: dict) -> None:
    page.route(
        pattern,
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(body)),
    )


def _mock(page: Page) -> None:
    _json_route(page, "**/api/apps", {"scan_root": "", "apps": [
        {"id": "longproj", "name": _PROJECT, "kind": "claude-code",
         "project_dir": "", "added_at": "", "is_favorite": False, "repo_url": None},
        {"id": "longapp", "name": _APP, "kind": "streamlit", "bat_path": "",
         "added_at": "2026-01-01T00:00:00", "autostart": False},
    ]})
    _json_route(page, "**/api/apps/running", {"running": []})
    _json_route(page, "**/api/agents", {"agents": [
        {"id": "claude", "label": "Claude Code", "available": True, "fullscreen": False},
    ], "vscode_available": False})
    _json_route(page, re.compile(r".*/api/claude-code/git-status$"), {"projects": [
        {"id": "longproj", "is_git": True, "branch": _BRANCH, "default_branch": "main",
         "on_default_branch": False, "dirty": False},
    ]})
    _json_route(page, re.compile(r".*/api/life-os/recap-status$"), {
        "available": False, "ledger_exists": False, "age_days": None,
        "staleness": "fresh", "proposal_pending": False, "proposal_name": None,
    })
    _json_route(page, re.compile(r".*/api/life-os/skills(\?.*)?$"), {
        "available": True, "life_os_dir": "", "skills": [{
            "id": "longskill", "name": _SKILL, "command": "longskill",
            "description": "Synthetic.", "skill_md": ".claude/skills/longskill/SKILL.md",
        }],
    })


def _assert_one_line(page: Page, sel: str, full_name: str) -> None:
    for width in (320, 430):
        page.set_viewport_size({"width": width, "height": 900})
        page.wait_for_timeout(150)
        m = stable_read(lambda: page.evaluate(_LINES, sel))
        assert m is not None, f"{sel} not rendered at {width}px"
        assert m["lines"] == 1, (
            f"at {width}px {sel} wraps onto {m['lines']} lines — a name must "
            "stay on one line, truncated, never broken mid-word (#1126)"
        )
        assert m["title"].startswith(full_name), (
            f"{sel} is truncated but its title {m['title']!r} does not carry "
            "the full name"
        )


def test_project_skill_and_app_names_stay_on_one_line(
    authed_page: Page, base_url: str
) -> None:
    _mock(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    # Coding tab (the default): a long project name; since #1128 its long
    # branch rides the row's context line instead of sharing the title's.
    authed_page.locator("details.projects-card").evaluate("el => { el.open = true; }")
    project = authed_page.locator('.coding-item[data-id="longproj"] .action-row-title')
    expect(project).to_be_visible()
    expect(authed_page.locator('.coding-item[data-id="longproj"] .action-row-meta')).to_be_visible()
    _assert_one_line(authed_page, '.coding-item[data-id="longproj"] .action-row-title', _PROJECT)

    authed_page.locator("#tabLifeOS").click()
    expect(authed_page.locator("#lifeOsList li.lifeos-item[data-id='longskill']")).to_be_visible()
    _assert_one_line(authed_page, "#lifeOsList li.lifeos-item[data-id='longskill'] .action-row-title", _SKILL)

    authed_page.locator("#tabApps").click()
    card = authed_page.locator(".apps-list-card")
    if card.get_attribute("open") is None:
        card.locator("summary").click()
    expect(authed_page.locator("#appsList .action-row-title")).to_be_visible()
    _assert_one_line(authed_page, "#appsList .action-row-title", _APP)


def test_helper_notes_are_upright_and_labels_sentence_cased(
    authed_page: Page, base_url: str
) -> None:
    _mock(authed_page)
    # Week-only savings: the badge's longest wording (#1191).
    _json_route(authed_page, re.compile(r".*/api/context-filter$"), {
        "mode": {"available": False}, "harnesses": [],
        "stats": {"available": True, "today": {"tokens_saved": 0},
                  "last_7_days": {"tokens_saved": 123456}},
    })
    authed_page.set_viewport_size({"width": 390, "height": 844})
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    # The Code tab's chips say what they are, whole words, on one line (#1191).
    for sel, text in (("#gitStatusBtn", "Git status"),
                      ("#codingFilterSavedBadge",
                       "Context filter saved 123.5k tokens in 7 days")):
        expect(authed_page.locator(sel)).to_have_text(text)
        m = stable_read(lambda: authed_page.evaluate(_LINES, sel))
        assert m is not None and m["lines"] == 1, f"{sel} wraps at 390px: {m}"

    note = authed_page.locator(".launcher-note").first
    expect(note).to_have_css("font-style", "normal")
    expect(note).to_have_css("font-weight", "400")

    for strip, label in (("#boardColBacklog", "Git"), ("#boardColClaude", "Bot"),
                         ("#boardColYours", "You"), ("#boardColOther", "Other"),
                         ("#boardColDone", "Done")):
        text = authed_page.locator(strip).evaluate("el => el.firstChild.textContent.trim()")
        assert text == label, f"{strip} reads {text!r}; strip labels are sentence case"
    expect(authed_page.locator("#codingOptions .collapse-title")).to_have_text("Options")
