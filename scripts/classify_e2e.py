"""Diff-proportionate e2e routing for the pre-ship gate (issue #568).

`scripts/verify-before-ship.ps1` used to run the *full* dual-projection
Playwright suite (Chromium + WebKit/iPhone, ~60 files / 370+ nodes, ~10 min)
unconditionally, regardless of what the diff touched. A static-asset-only
change (e.g. #565: a vendored SVG sprite + one pure-Python unit test) paid that
full cost for zero plausible browser exposure.

This module maps the changed-file set of the current branch to a **coverage
tier** so the gate runs an e2e slice *proportionate* to the diff — never
weaker for real UI/behaviour changes, and fail-safe to the full suite whenever
the diff is ambiguous, mixed, or touches anything not confidently narrow.

The path -> category rules live in `_classify_one` — the single obvious,
reviewable place, deliberately mirroring the e2e surface `CLAUDE.md` already
enumerates (the "CI expectations" block: `app/webapp/`, `src/session_host*.py`,
`src/launcher.py`, `tests/e2e/`, static assets). Keep them here; do not scatter
path knowledge into the PowerShell gate.

Tiers (only the *e2e* phase is routed — byte-compile + the non-e2e pytest suite
always run and already cover backend Python):

  * ``skip``    no changed file touches the browser surface -> no browser suite.
  * ``static``  static assets only (images / fonts / webmanifest / vendored
                HTML sprite fragments) -> smoke suite, **Chromium only**.
  * ``full``    anything on the real browser surface (JS, CSS, app pages,
                webapp/session-host/launcher Python, e2e tests), any *mixed*
                diff, and anything unrecognized -> **full dual-projection**
                suite (unchanged behaviour). This is the fail-safe default.

CSS deliberately routes to ``full`` rather than a curated "layout subset":
`styles.css` is global and the WebKit projection is exactly where layout
regressions surface, so a hand-maintained subset would be both drift-prone and
an under-testing risk — and under-testing must never be the outcome of
uncertainty (issue #568's first constraint).

CLI: prints ``E2E_*=`` key/value lines (parsed by the gate) plus a human
summary. Run standalone to see how the current branch would route:

    python scripts/classify_e2e.py            # classify the live diff
    python scripts/classify_e2e.py a.js b.svg  # classify an explicit file list
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from enum import IntEnum
from typing import List, Sequence


class Category(IntEnum):
    """Per-file coverage requirement. Higher wins across the diff."""

    NONE = 0    # no browser impact — backend py, docs, non-e2e tests, config
    STATIC = 1  # static asset — Chromium smoke is enough
    FULL = 2    # real browser surface / unrecognized — full dual suite


# Static-asset file extensions under app/webapp/static/** (no JS/CSS behaviour).
_STATIC_EXTS = {
    "svg", "png", "jpg", "jpeg", "gif", "ico", "webp", "avif",
    "woff", "woff2", "ttf", "eot", "otf", "webmanifest",
}

# Python modules that ARE on the e2e browser surface (the PTY / session-host /
# launcher path CLAUDE.md calls out). Everything else under src/ is covered by
# the non-e2e pytest suite and needs no browser.
#
# Matched as a `session_host` *prefix* (not an exact filename) so a future
# split/extension of the session host (e.g. `src/session_host_pty.py`) is
# caught automatically instead of falling through to the generic `src/*.py`
# -> NONE rule below. See test_classify_e2e.py's real-tree drift guard.
_FULL_SRC_PY_EXACT = ("src/session_client.py", "src/launcher.py")


def _classify_one(path: str) -> tuple[Category, str]:
    """Map one repo-relative (posix) path to its coverage category + a label.

    Ordered most-specific first. The final ``return FULL`` is the fail-safe:
    any path not matched by a rule above is treated as browser-relevant.

    Uses plain prefix/extension checks rather than ``PurePath.full_match`` —
    that glob API only exists on Python 3.13+, and the gate must run on the
    older Python the CI runner ships (caught by CI: full_match AttributeError).
    """
    name = path.rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""

    # --- static assets under the webapp's static dir -> STATIC ---------------
    if path.startswith("app/webapp/static/"):
        if ext in _STATIC_EXTS:
            return Category.STATIC, "static-asset"
        # A vendored HTML *fragment* (e.g. the icons sprite) is pure markup the
        # smoke suite loads — no behaviour of its own. Non-vendored .html
        # (index.html, spike pages) are real app pages -> fall through to FULL.
        if ext == "html" and path.startswith("app/webapp/static/_vendored/"):
            return Category.STATIC, "static-asset"
        # .js / .css / real .html / anything else under static -> browser.
        return Category.FULL, "webapp-static-code"

    # --- the rest of the webapp package (server, routers, manager) -> FULL ---
    if path.startswith("app/webapp/"):
        return Category.FULL, "webapp"

    # --- session-host / launcher on the e2e surface -> FULL -----------------
    if path.startswith("app/session_host/"):
        return Category.FULL, "session-host"
    if path in _FULL_SRC_PY_EXACT or path == "launcher.py":
        return Category.FULL, "session-host/launcher"
    if path.startswith("src/session_host") and ext == "py":
        return Category.FULL, "session-host/launcher"

    # --- the e2e suite itself (and the shared root conftest) -> FULL --------
    if path.startswith("tests/e2e/"):
        return Category.FULL, "e2e-test"
    if path == "tests/conftest.py":
        return Category.FULL, "shared-conftest"

    # --- things with no browser impact -> NONE ------------------------------
    #   backend Python off the session-host path; non-e2e tests; docs; scripts;
    #   config; CI yaml; repo meta files.
    # Deliberate #568 decision, not an oversight: backend `src/` code off the
    # session-host path has no browser surface of its own — it's covered by
    # the non-e2e pytest suite (TestClient etc.), so it skips the browser
    # tier here. Do not "fix" this by routing all of src/ to FULL.
    if path.startswith("src/") and ext == "py":
        return Category.NONE, "backend-python"
    if path.startswith("tests/") and ext == "py":
        return Category.NONE, "non-e2e-test"
    if ext == "md" or path.startswith("docs/"):
        return Category.NONE, "docs"
    if (
        path.startswith("scripts/")
        or path.startswith("config/")
        or path.startswith(".github/")
    ):
        return Category.NONE, "tooling/config"
    if path in {".fleet.toml", ".gitignore", "LICENSE", "AGENTS.md", "CLAUDE.md"} or ext == "bat":
        return Category.NONE, "repo-meta"

    # --- fail-safe: anything unrecognized runs the full suite ---------------
    return Category.FULL, "unclassified"


@dataclass
class Routing:
    tier: str                       # "skip" | "static" | "full"
    browsers: List[str]             # e.g. ["chromium"] or [] (full = suite default)
    pytest_target: str              # e2e path selector ("" when tier == skip)
    reasons: List[str] = field(default_factory=list)  # "label: example/path"


def classify(paths: Sequence[str]) -> Routing:
    """Route a set of changed paths to an e2e tier (see module docstring)."""
    # Bucket each path under its category label, keeping one example per label
    # so the report can show *why* the tier was chosen.
    examples: dict[str, str] = {}
    top = Category.NONE
    for raw in paths:
        path = raw.strip().replace("\\", "/")
        if not path:
            continue
        cat, label = _classify_one(path)
        top = max(top, cat)
        examples.setdefault(f"{cat.name}:{label}", path)

    def reasons_for(cat: Category) -> List[str]:
        out = [
            f"{key.split(':', 1)[1]}: {ex}"
            for key, ex in sorted(examples.items())
            if key.startswith(f"{cat.name}:")
        ]
        return out

    if not examples:
        # Empty diff (e.g. run on a clean tree): can't prove narrow -> full.
        return Routing("full", [], "tests/e2e", ["empty-diff: no changed files"])

    if top == Category.FULL:
        return Routing("full", [], "tests/e2e", reasons_for(Category.FULL))
    if top == Category.STATIC:
        return Routing(
            "static", ["chromium"], "tests/e2e/test_smoke.py",
            reasons_for(Category.STATIC),
        )
    return Routing("skip", [], "", reasons_for(Category.NONE))


# --------------------------------------------------------------------- git diff
def _run_git(args: List[str]) -> List[str]:
    try:
        out = subprocess.run(
            ["git", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return []
    if out.returncode != 0:
        return []
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def _main_ref() -> str:
    """origin/main (or origin/<default>) when present, else main."""
    head = _run_git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"])
    if head:
        # e.g. "refs/remotes/origin/main" -> "origin/main"
        return head[0].replace("refs/remotes/", "", 1)
    if _run_git(["rev-parse", "--verify", "--quiet", "origin/main"]):
        return "origin/main"
    return "main"


def changed_files() -> List[str]:
    """Changed files on the current branch vs main, incl. the working tree.

    Union of: committed since the merge-base with main (``main...HEAD``),
    tracked working-tree edits (staged + unstaged, ``git diff HEAD``), and
    untracked new files. So a *pre-commit* gate run classifies correctly.
    """
    ref = _main_ref()
    files = set()
    files.update(_run_git(["diff", "--name-only", f"{ref}...HEAD"]))
    files.update(_run_git(["diff", "--name-only", "HEAD"]))
    files.update(_run_git(["ls-files", "--others", "--exclude-standard"]))
    return sorted(files)


def main(argv: List[str]) -> int:
    paths = argv[1:] if len(argv) > 1 else changed_files()
    routing = classify(paths)

    # Machine-readable block the PowerShell gate parses (^E2E_ lines only).
    print(f"E2E_TIER={routing.tier}")
    print(f"E2E_BROWSERS={','.join(routing.browsers)}")
    print(f"E2E_PYTEST_TARGET={routing.pytest_target}")
    print(f"E2E_REASON={' | '.join(routing.reasons) if routing.reasons else '(none)'}")

    # Human summary (ignored by the gate parser).
    print("", file=sys.stderr)
    print(f"e2e routing: tier={routing.tier} "
          f"browsers={routing.browsers or 'suite-default'}", file=sys.stderr)
    for reason in routing.reasons:
        print(f"  - {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
