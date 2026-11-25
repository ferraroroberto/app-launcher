"""Run the repo's request-budget browser tests on WebKit, as the Mac nightly does.

The local gate drives the browser suite on Chromium only; the Mac nightly runs
it on WebKit, where timer and poll behaviour differ enough to cross a budget
edge (a page at 61 requests in a visible minute against a budget of 60). This
stage runs just the request-budget tests (docs/polling-budget.md) on WebKit so
that class of overrun fails before the push instead of after it.

The tests are the test files that call `measure_request_budget(` (the probe
helper itself, `_request_budget.py`, is not one). A repo whose budget tests sit
in files that also hold unrelated tests names the files instead:

    [webkit_budget]
    targets = ["tests/e2e/test_request_budget.py"]

The last stdout line is the verdict, one of:

  WEBKIT_BUDGET=pass            exit 0  the tests ran on WebKit and passed
  WEBKIT_BUDGET=fail            exit 1  a budget test failed on WebKit
  WEBKIT_BUDGET=unknown         exit 3  NOT CONFIRMED: Playwright missing, a
                                        malformed `[webkit_budget] targets`, or a
                                        pytest run that never reported
  WEBKIT_BUDGET=not-installed   exit 4  WebKit's browser build is not installed
                                        here, so nothing ran (never a pass:
                                        `python -m playwright install webkit`)
  WEBKIT_BUDGET=no-tests        exit 5  the repo has no request-budget test

A caller treats 0 as the only pass; 4 and 5 are "not run" states it prints as
such, and 1 and 3 fail the gate.

Vendor-verbatim (manifest key `webkit_budget_check`), stdlib-only apart from the
Playwright the repo's e2e suite already needs.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_UNKNOWN = 3
EXIT_NOT_INSTALLED = 4
EXIT_NO_TESTS = 5

PROBE_HELPER = "_request_budget.py"
# The probe runs a fake-clock minute per case; a hung browser must end the gate, not stall it.
RUN_TIMEOUT_S = 900
_MARKER = "measure_request_budget("
_SKIP_DIRS = frozenset({".venv", "venv", "node_modules", ".git", "__pycache__", ".tox"})


def _verdict(word: str, detail: str = "") -> None:
    if detail:
        print(detail)
    print(f"WEBKIT_BUDGET={word}")


def declared_targets(root: Path) -> list[str] | None:
    """`[webkit_budget] targets` from `.fleet.toml`, or None when not declared.

    Raises ValueError for a table that is present but not a list of strings.
    """
    config = root / ".fleet.toml"
    if not config.is_file():
        return None
    with config.open("rb") as handle:
        table = tomllib.load(handle).get("webkit_budget", {})
    if not isinstance(table, dict) or "targets" not in table:
        return None
    targets = table["targets"]
    if not isinstance(targets, list) or not all(isinstance(t, str) and t for t in targets):
        raise ValueError("[webkit_budget] targets must be a list of non-empty path strings")
    return list(targets)


def discover_targets(root: Path) -> list[str]:
    """Every test file under `tests/` that calls `measure_request_budget(`, as repo-relative posix paths."""
    tests = root / "tests"
    found: list[str] = []
    if not tests.is_dir():
        return found
    for path in sorted(tests.rglob("*.py")):
        relative = path.relative_to(root)
        if _SKIP_DIRS.intersection(relative.parts) or path.name == PROBE_HELPER:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _MARKER in text:
            found.append(relative.as_posix())
    return found


def webkit_installed() -> bool | None:
    """Whether Playwright's WebKit build is on disk; None when Playwright cannot say."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as playwright:
            return Path(playwright.webkit.executable_path).exists()
    except Exception as exc:  # noqa: BLE001 -- any driver failure is "cannot say", never "installed"
        print(f"⚠️ could not ask Playwright where its WebKit build is: {exc}", file=sys.stderr)
        return None


def run(root: Path, python: str) -> int:
    try:
        targets = declared_targets(root)
    except (ValueError, tomllib.TOMLDecodeError) as exc:
        _verdict("unknown", f"❌ NOT CONFIRMED: {exc}")
        return EXIT_UNKNOWN
    if targets is None:
        targets = discover_targets(root)
    if not targets:
        _verdict("no-tests", "ℹ️ no test calls measure_request_budget(, so there is nothing to run on WebKit")
        return EXIT_NO_TESTS
    missing = [t for t in targets if not (root / t).exists()]
    if missing:
        _verdict("unknown", f"❌ NOT CONFIRMED: declared [webkit_budget] targets that do not exist: {', '.join(missing)}")
        return EXIT_UNKNOWN

    installed = webkit_installed()
    if installed is None:
        _verdict("unknown", "❌ NOT CONFIRMED: Playwright is not importable here, so WebKit's presence is unknown")
        return EXIT_UNKNOWN
    if not installed:
        _verdict("not-installed",
                 "⚠️ WebKit is not installed: the request budget was NOT run on it "
                 "(install with: python -m playwright install webkit)")
        return EXIT_NOT_INSTALLED

    print(f"ℹ️ request budget on WebKit: {' '.join(targets)}")
    try:
        # no-window-exempt: the run streams its output into the gate's own console, which a windowless child would not share
        result = subprocess.run(  # noqa: S603
            [python, "-m", "pytest", *targets, "--browser", "webkit"], cwd=root, check=False, timeout=RUN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _verdict("unknown", f"❌ NOT CONFIRMED: the WebKit run did not finish within {RUN_TIMEOUT_S} s")
        return EXIT_UNKNOWN
    if result.returncode == 0:
        _verdict("pass")
        return EXIT_PASS
    if result.returncode == 1:
        _verdict("fail", "❌ a request-budget test failed on WebKit")
        return EXIT_FAIL
    _verdict("unknown", f"❌ NOT CONFIRMED: pytest ended with exit {result.returncode} (not a plain pass or fail)")
    return EXIT_UNKNOWN


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="the repo root (default: the current directory)")
    args = parser.parse_args(argv)
    return run(args.root.resolve(), sys.executable)


if __name__ == "__main__":
    sys.exit(main())
