"""#1532 — Chat steps read as their descriptions, live, and open clean.

Pinned against a stubbed transcript (every command, path and output
synthetic):

* the step in progress — the newest tool call, no result yet, the agent
  working — reads as its own description on the folded line, with an
  elapsed counter in the duration's place that moves on the client and is
  gone once the result lands (the duration comes back);
* opened, a call is named by its description with its command as the hint;
* opened, a command is a card: a header naming the tool (and a recorded exit
  status), a ``$`` command block with light shell highlighting, and the
  output as a separate block — escape-first, so a command holding
  ``<script>`` and an output holding HTML both read as literal text and
  neither becomes an element;
* at phone width the card wraps inside the pane in both themes, and the
  highlighting resolves to theme colours.

The poll is driven by the fake clock (#1376), never slept out; style reads
use auto-retrying ``expect().to_have_css`` and the geometry read goes
through ``stable_eval`` (#680, #1346).
"""
from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.conftest import OVERLAY_OPEN_MS, stable_eval
from tests.e2e.test_session_mode_toggle import (
    _mock_git_status,
    _mock_sessions_list,
    _row,
    _session_row,
)

pytestmark = pytest.mark.smoke

_SID = "sid-step-card-1532"
# session-transcript.js LIVE_POLL_MS.
_POLL_MS = 3_000

_SCRIPT_CMD = (
    'echo "<script>window.__pwned=1</script>" && ls -la $HOME 2>&1 '
    "| grep -v node_modules --color=never > listing-output.txt"
)
_HTML_OUT = '<b>bold</b><img src=x onerror="window.__pwned=1">'


class _Transcript:
    """The transcript route, both directions: a page read answers every
    entry, a forward read (``after=``) the ones from that offset on, the
    newest held back as pending, as the real reader does."""

    def __init__(self, page: Page) -> None:
        self.entries: list[dict] = []
        self.activity: dict | None = None
        self.forward = 0
        page.route(
            re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/transcript(\?.*)?$"),
            self._handle,
        )

    def _handle(self, route) -> None:
        url = route.request.url
        tail = self.entries[-1]["offset"] if self.entries else 0
        common = {"available": True, "source": "native", "reason": None, "session_id": _SID,
                  "tail": tail, "size": tail + 1, "tool_errors": "reported",
                  "activity": self.activity}
        m = re.search(r"[?&]after=(\d+)", url)
        if m is None:
            body = {**common, "next_cursor": None, "entries": list(self.entries)}
        else:
            self.forward += 1
            sm = re.search(r"[?&]size=(\d+)", url)
            changed = not (sm and int(sm.group(1)) == tail + 1)
            fresh = [e for e in self.entries if e["offset"] >= int(m.group(1))] if changed else []
            body = {**common, "changed": changed, "reset": False,
                    "entries": fresh[:-1], "pending": fresh[-1:]}
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(body))


def _turn(kind: str, text: str, offset: int, ts: str) -> dict:
    return {"kind": kind, "timestamp": ts, "text": text, "truncated": False,
            "sidechain": False, "offset": offset}


def _bash(offset: int, ts: str, command: str, *, label: str, result=None, error=False) -> dict:
    e = {"kind": "tool_call", "timestamp": ts, "name": "Bash",
         "summary": " ".join(command.split()), "label": label,
         "result": result, "result_truncated": False, "sidechain": False, "offset": offset,
         "action": {"verb": "ran", "command": command}}
    if error:
        e["error"] = True
    return e


def _since(page: Page, seconds: int) -> str:
    """An ISO stamp ``seconds`` before the page's (fake) now."""
    return page.evaluate("(s) => new Date(Date.now() - s * 1000).toISOString()", seconds)


def _boot(page: Page, base_url: str) -> _Transcript:
    _mock_git_status(page)
    _mock_sessions_list(page, [_session_row(_SID, kind="remote", agent="claude", title="Step demo")])
    tr = _Transcript(page)
    _fake_clock.install(page)
    page.goto(base_url, wait_until="domcontentloaded")
    return tr


def _open_chat(page: Page) -> None:
    _row(page, _SID).locator(".session-open").click()
    expect(page.locator("#terminalOverlay")).to_be_visible(timeout=OVERLAY_OPEN_MS)
    expect(page.locator("#transcriptList .tr-turn")).not_to_have_count(0)


def test_the_step_in_progress_reads_as_its_description_and_counts_up(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    tr = _boot(page, base_url)
    tr.entries = [
        _turn("user", "run the e2e suite", 100, _since(page, 70)),
        _bash(200, _since(page, 65), "pytest tests/e2e -q", label="Run the e2e suite on Chromium"),
    ]
    tr.activity = {"since": _since(page, 70), "actions": 1,
                   "last": "Run the e2e suite on Chromium", "working": True}
    _open_chat(page)

    step = page.locator("#transcriptList .tr-step")
    line = step.locator("summary.tr-step-line")
    expect(line.locator(".tr-step-now")).to_have_text("Run the e2e suite on Chromium")
    live = line.locator(".tr-step-live")
    expect(live).to_have_text(re.compile(r"^1:0\d$"))
    expect(line.locator(".tr-step-dur")).to_have_count(0)

    # The counter moves on the client: 2 s is under the poll, so no read
    # goes out, yet it has advanced.
    shown = live.text_content()
    before = tr.forward
    _fake_clock.advance(page, 2_000)
    assert tr.forward == before
    expect(live).not_to_have_text(shown)

    # Opened, the call is named by its description, its command the hint.
    line.click()
    item = step.locator(".tr-item").first
    expect(item.locator(".tr-item-name")).to_have_text("Run the e2e suite on Chromium")
    expect(item.locator(".tr-item-hint")).to_have_text("pytest tests/e2e -q")

    # The result lands (the agent still working): the counter goes and the
    # run's duration comes back in its place.
    tr.entries[1]["result"] = "3 passed in 1.2s"
    tr.entries.append(_turn("assistant", "All green.", 300, _since(page, 0)))
    for _ in range(5):
        _fake_clock.advance(page, _POLL_MS + 1)
        if tr.forward > before:
            break
    expect(page.locator("#transcriptList .tr-step-live")).to_have_count(0)
    expect(page.locator("#transcriptList .tr-step-dur")).to_be_visible()


@pytest.mark.iphone
def test_an_opened_command_is_a_clean_card_and_never_markup(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844})
    tr = _boot(page, base_url)
    tr.entries = [
        _turn("user", "list the home folder", 100, _since(page, 30)),
        _bash(200, _since(page, 25), _SCRIPT_CMD, label="List the home folder", result=_HTML_OUT),
        _bash(300, _since(page, 20), "npm test", label="Run the JS tests",
              result="Exit code 1\nboom", error=True),
        _turn("assistant", "Done.", 400, _since(page, 10)),
    ]
    _open_chat(page)
    step = page.locator("#transcriptList .tr-step")
    # The run reads as its newest step, the counts trailing it.
    expect(step.locator(".tr-step-now")).to_have_text("Run the JS tests")
    expect(step.locator(".tr-step-counts")).to_have_text(" · Ran 2 commands")
    step.locator("summary.tr-step-line").click()
    ran, failed = step.locator(".tr-item").nth(0), step.locator(".tr-item").nth(1)
    ran.locator("summary").click()

    card = ran.locator(".tr-cmd-card")
    expect(card.locator(".tr-cmd-tool")).to_have_text("Bash")
    cmd, out = card.locator(".tr-cmd"), card.locator(".tr-out")
    # Two distinct blocks, the command first.
    expect(cmd).to_have_count(1)
    expect(out).to_have_count(1)
    expect(cmd).to_have_text("$ " + _SCRIPT_CMD)
    # Escape-first: both read as their literal text, and nothing in them
    # became an element or ran.
    expect(cmd.locator(".tr-cmd-text")).to_have_text(_SCRIPT_CMD)
    expect(out).to_have_text(_HTML_OUT)
    expect(card.locator("script, b, img")).to_have_count(0)
    assert page.evaluate("() => window.__pwned") is None
    # Light highlighting: the command words, flags, the quoted string,
    # operators and the variable, each its own token.
    expect(cmd.locator(".sh-cmd")).to_have_text(["echo", "ls", "grep"])
    expect(cmd.locator(".sh-flag")).to_have_text(["-la", "-v", "--color=never"])
    expect(cmd.locator(".sh-str")).to_have_text(['"<script>window.__pwned=1</script>"'])
    expect(cmd.locator(".sh-op")).to_have_text(["&&", "2>&1", "|", ">"])
    expect(cmd.locator(".sh-var")).to_have_text(["$HOME"])

    # A recorded exit status reads quietly in the header, not as output.
    failed.locator("summary").click()
    expect(failed.locator(".tr-cmd-exit")).to_have_text("exit 1")
    expect(failed.locator(".tr-out")).to_have_text("boom")

    # At phone width the long command wraps inside the card and nothing pans
    # sideways; the highlighting resolves in both themes, differently.
    seen = {}
    for theme in ("light", "dark"):
        page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
        flag = cmd.locator(".sh-flag").first
        expect(flag).not_to_have_css("color", "rgba(0, 0, 0, 0)")
        seen[theme] = flag.evaluate("el => getComputedStyle(el).color")
        fit = stable_eval(cmd, """el => {
            const r = el.getBoundingClientRect();
            if (!r.width) return null;
            const pane = document.getElementById('transcriptBody');
            const lh = parseFloat(getComputedStyle(el).lineHeight);
            return {cmd: [el.scrollWidth, el.clientWidth], pane: [pane.scrollWidth, pane.clientWidth],
                    lines: Math.round((el.clientHeight - 20) / lh)};
        }""")
        assert fit["cmd"][0] <= fit["cmd"][1] and fit["pane"][0] <= fit["pane"][1], (theme, fit)
        assert fit["lines"] >= 2, (theme, fit)
    assert seen["light"] != seen["dark"], seen
