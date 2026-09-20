"""The voice-loop spike's expiry condition, as an executable check (#1054).

``spike-voice-loop.*`` is throwaway-by-design and carries a written expiry
condition. The previous condition named a *ticket* -- "delete once #245's voice
mode has shipped" -- and a ``/codebase-audit`` run read that as satisfied the
moment #245 closed, proposing the deletion of ~1,077 lines and a phone-reachable
page. #245 had in fact shipped with the continuous hands-free loop *explicitly
deferred*, so its closing was evidence against the condition, not for it.

#1054 replaced it with a condition that names the **artifact**:

    the set is deleted once a hands-free conversation mode exists
    OUTSIDE ``spike-voice-loop.*``

This module makes the discriminating half of that condition mechanical, so the
next sweep gets an answer from a test run rather than from reading prose.

``barge-in`` is the term that discriminates. It is the one behaviour of the
continuous loop that neither shipped one-way path has any reason to name:
dictation (``voice.js``, speech -> text) and read-aloud
(``terminal-readaloud.js``, one-way narration) are each half a conversation, and
only a real two-way loop has to interrupt its own playback. Its sibling
behaviour ``re-arm`` is deliberately **not** checked -- the word is already used
in the tree for the tray watchdog, the e2e gate's webapp and keyboard-modifier
state, so it cannot discriminate.

**This is a guard, not a regression test.** There is no pre-fix commit on which
it fails; it pins a property that already holds. What it buys is the failure
*later*: when ``barge-in`` shows up in shipped app code, this test goes red and
names #1054, which is the signal to delete the set rather than to widen the
allowlist.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# The spike's own footprint: the files that legitimately say "barge-in" today,
# and that the closing PR of #1054 deletes together.
_SPIKE_FOOTPRINT = {
    "app/webapp/static/spike-voice-loop.html",
    "app/webapp/static/spike-voice-loop.js",
    "app/webapp/static/spike-voice-loop-fsm.js",
    "app/webapp/routers/misc.py",  # the /spike/voice-loop route docstring
    "docs/voice-loop-spike.md",
    "tests/e2e/test_spike_voice_loop.py",
    "tests/test_voice_loop_spike_retention.py",  # this file
}

# The spike file that must still carry the term, so the check cannot quietly
# become vacuous if the prototype is renamed or gutted without being removed.
_ANCHOR = "app/webapp/static/spike-voice-loop.js"

_TEXT_SUFFIXES = {
    ".py", ".js", ".html", ".css", ".md", ".json", ".toml",
    ".yml", ".yaml", ".ps1", ".bat", ".txt",
}
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}

_TERM = "barge-in"


def _files_mentioning_term() -> set[str]:
    """Repo-relative POSIX paths of text files containing ``barge-in``."""
    hits: set[str] = set()
    for path in _REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        if _SKIP_DIRS & set(path.relative_to(_REPO_ROOT).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:  # unreadable file is not evidence either way
            continue
        if _TERM in text.lower():
            hits.add(path.relative_to(_REPO_ROOT).as_posix())
    return hits


def test_barge_in_appears_only_inside_the_spike_footprint() -> None:
    """A ``barge-in`` outside the spike means the expiry condition has fired."""
    outside = sorted(_files_mentioning_term() - _SPIKE_FOOTPRINT)
    assert not outside, (
        "'barge-in' now appears outside the spike-voice-loop.* footprint:\n  "
        + "\n  ".join(outside)
        + "\n\nIf a hands-free conversation mode has shipped there (re-arm, "
        "barge-in, and a conversation-mode entry point the user can open), "
        "the expiry condition in #1054 has fired: delete the spike-voice-loop.* "
        "set -- the three static files, the /spike/voice-loop route, the "
        "Settings footer link and its token-baking branch, "
        "tests/e2e/test_spike_voice_loop.py, docs/voice-loop-spike.md and this "
        "guard -- and close #1054 with that PR. Only if the term is being used "
        "for something unrelated to a voice loop should this allowlist grow."
    )


def test_guard_is_not_vacuous() -> None:
    """The anchor still carries the term, so the check above still checks."""
    anchor = _REPO_ROOT / _ANCHOR
    assert anchor.is_file(), f"{_ANCHOR} is gone -- delete this guard with the set"
    assert _TERM in anchor.read_text(encoding="utf-8").lower(), (
        f"{_ANCHOR} no longer mentions {_TERM!r}; the footprint check above "
        "would now pass vacuously."
    )
