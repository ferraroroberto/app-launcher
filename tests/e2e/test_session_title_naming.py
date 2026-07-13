"""Regression pin for #266 — name Coding windows & rows from the conversation.
Extended #396: the same ``sessionTitle()`` now also carries the shared
cross-tab title (fleet-config#302) that both the Coding tab and the Board
tab read.

Two pure-function contracts back the feature, pinned here via dynamic
``import()`` in the page (the same probe pattern as
``test_fullscreen_keyboard_pan.py``):

1. ``sessions.js`` ``sessionTitle()`` smart precedence. A genuine shared
   title (fleet-config#302, ``shared_name`` with ``shared_name_source`` !==
   ``'derived'``) wins outright. Otherwise: only Claude emits a genuine
   per-conversation OSC title; Codex emits ``<folder> | <model>``, Pi emits
   ``π - <folder>``, and Antigravity/Copilot emit nothing. So a real live
   summary wins next, a folder-echo title yields to the first-prompt-derived
   ``prompt_title``, then a *derived* shared name (the generic
   ``<project>-N`` fallback) beats a bare folder echo, and with nothing at
   all we fall back to the launch name.

2. ``terminal.js`` ``mirrorDocTitle()``. The PC mirror window's OS title must
   prepend the human title for the Windows/PTI title bar while still containing
   the ``app-launcher-mirror-<sid>`` marker the launcher's EnumWindows scan
   matches (as a substring) to close/reconcile the window (issue #20).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.smoke

_TITLE_PROBE = r"""
async () => {
  const { sessionTitle } = await import('/static/sessions.js');
  const dir = 'C:/code/app-launcher';   // basename 'app-launcher'
  return {
    // Claude's real summary (glyph stripped, not a folder echo) wins.
    claudeSummary: sessionTitle({
      live_title: '✳ Fixing the login flow bug',
      prompt_title: 'something else', project_dir: dir, name: 'app-launcher',
    }),
    // Codex '<folder> | <model>' is a short folder echo → prompt_title wins.
    codexEcho: sessionTitle({
      live_title: 'app-launcher | gpt-5.5',
      prompt_title: 'add dark mode toggle', project_dir: dir, name: 'app-launcher',
    }),
    // Pi 'π - <folder>' is a folder echo → prompt_title wins.
    piEcho: sessionTitle({
      live_title: 'π - app-launcher',
      prompt_title: 'refactor the parser', project_dir: dir, name: 'app-launcher',
    }),
    // No agent title at all (Antigravity/Copilot) → first-prompt title.
    noLiveTitle: sessionTitle({
      live_title: '', prompt_title: 'wire up the API',
      project_dir: dir, name: 'app-launcher',
    }),
    // Nothing derived yet → fall back to the launch name.
    bareFallback: sessionTitle({
      live_title: '', prompt_title: '',
      project_dir: 'C:/code/myproj', name: 'myproj',
    }),
    // Folder echo but no prompt yet → keep the echo (better than 'session').
    echoNoPrompt: sessionTitle({
      live_title: 'app-launcher | gpt-5.5', prompt_title: '',
      project_dir: dir, name: 'app-launcher',
    }),
    // #396: a genuine shared title (fleet-config#302) wins outright, even
    // over a real live OSC summary — it's the cross-tab source of truth.
    sharedNameWins: sessionTitle({
      shared_name: 'Fixing the chunk merge bug', shared_name_source: null,
      live_title: '✳ Some other in-terminal summary', prompt_title: 'x',
      project_dir: dir, name: 'app-launcher',
    }),
    // A *derived* shared name ("<project>-N") is the generic hook fallback —
    // a real live title still wins over it.
    sharedDerivedYieldsToLive: sessionTitle({
      shared_name: 'app-launcher-2', shared_name_source: 'derived',
      live_title: '✳ Fixing the login flow bug', prompt_title: '',
      project_dir: dir, name: 'app-launcher',
    }),
    // With no live/prompt title at all, a derived shared name still beats
    // the bare project echo.
    sharedDerivedBeatsBareFallback: sessionTitle({
      shared_name: 'app-launcher-2', shared_name_source: 'derived',
      live_title: '', prompt_title: '',
      project_dir: dir, name: 'app-launcher',
    }),
    // No shared name at all (hooks not writing yet, or no match) → falls
    // through to the pre-#396 precedence untouched.
    noSharedNameFallsThrough: sessionTitle({
      shared_name: null, shared_name_source: null,
      live_title: '', prompt_title: 'wire up the API',
      project_dir: dir, name: 'app-launcher',
    }),
    // #458: a manual (launcher-native) rename wins outright — over a genuine
    // shared name, a real live OSC summary, everything. It is the one title
    // channel that works identically for every agent (including detached
    // sessions), so it sits above every auto-derived source.
    manualTitleWinsOverEverything: sessionTitle({
      manual_title: 'My custom rename',
      shared_name: 'Fixing the chunk merge bug', shared_name_source: null,
      live_title: '✳ Some other in-terminal summary', prompt_title: 'x',
      project_dir: dir, name: 'app-launcher',
    }),
    // An empty manual_title (never set, or explicitly cleared) must not
    // shadow the automatic precedence below it.
    emptyManualTitleFallsThrough: sessionTitle({
      manual_title: '',
      live_title: '', prompt_title: 'wire up the API',
      project_dir: dir, name: 'app-launcher',
    }),
  };
}
"""

_MIRROR_PROBE = r"""
async () => {
  const { mirrorDocTitle } = await import('/static/terminal.js');
  return {
    withTitle: mirrorDocTitle('abc123', 'Fixing the login bug'),
    empty: mirrorDocTitle('abc123', ''),
  };
}
"""


def test_session_title_precedence(authed_page: Page, base_url: str) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    r = authed_page.evaluate(_TITLE_PROBE)

    assert r["claudeSummary"] == "Fixing the login flow bug", (
        f"a genuine Claude summary returned {r['claudeSummary']!r} — a real "
        "per-conversation title must win over the first-prompt fallback"
    )
    assert r["codexEcho"] == "add dark mode toggle", (
        f"Codex's '<folder> | <model>' echo returned {r['codexEcho']!r} — a "
        "folder-echo title must yield to the first-prompt title"
    )
    assert r["piEcho"] == "refactor the parser", (
        f"Pi's 'π - <folder>' echo returned {r['piEcho']!r} — it must yield to "
        "the first-prompt title"
    )
    assert r["noLiveTitle"] == "wire up the API", (
        f"an agent with no OSC title returned {r['noLiveTitle']!r} — the "
        "first-prompt title must show (Antigravity/Copilot have no native name)"
    )
    assert r["bareFallback"] == "myproj", (
        f"a session with no titles returned {r['bareFallback']!r}, expected the "
        "launch name 'myproj'"
    )
    assert r["echoNoPrompt"] == "app-launcher | gpt-5.5", (
        f"a folder echo with no prompt yet returned {r['echoNoPrompt']!r} — it "
        "should keep the echo rather than collapse to a generic placeholder"
    )
    assert r["sharedNameWins"] == "Fixing the chunk merge bug", (
        f"a genuine shared title returned {r['sharedNameWins']!r} — the "
        "cross-tab shared name (fleet-config#302) must win outright"
    )
    assert r["sharedDerivedYieldsToLive"] == "Fixing the login flow bug", (
        f"a derived shared name beat a real live title, got "
        f"{r['sharedDerivedYieldsToLive']!r} — the generic <project>-N "
        "fallback must yield to a genuine live OSC summary"
    )
    assert r["sharedDerivedBeatsBareFallback"] == "app-launcher-2", (
        f"a derived shared name lost to the bare project fallback, got "
        f"{r['sharedDerivedBeatsBareFallback']!r} — it should still beat "
        "a bare project echo when nothing else is available"
    )
    assert r["noSharedNameFallsThrough"] == "wire up the API", (
        f"with no shared name at all, precedence should be unchanged from "
        f"pre-#396 behavior, got {r['noSharedNameFallsThrough']!r}"
    )
    assert r["manualTitleWinsOverEverything"] == "My custom rename", (
        f"a manual rename returned {r['manualTitleWinsOverEverything']!r} — "
        "issue #458's launcher-native override must win over every "
        "auto-derived source, including a genuine shared_name"
    )
    assert r["emptyManualTitleFallsThrough"] == "wire up the API", (
        f"an unset manual_title returned "
        f"{r['emptyManualTitleFallsThrough']!r} — it must not shadow the "
        "automatic precedence beneath it"
    )


def test_mirror_doc_title_keeps_marker_and_human_title(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    r = authed_page.evaluate(_MIRROR_PROBE)

    marker = "app-launcher-mirror-abc123"
    assert r["withTitle"] == f"Fixing the login bug — {marker}", (
        f"mirror title was {r['withTitle']!r} — the human title must lead "
        "(visible in the title bar) with the marker trailing"
    )
    assert marker in r["withTitle"], (
        "the close marker vanished from the mirror title — EnumWindows can no "
        "longer find/close the Edge --app window (issue #20)"
    )
    assert r["empty"] == marker, (
        f"with no human title the mirror title was {r['empty']!r}, expected the "
        f"bare marker {marker!r}"
    )
