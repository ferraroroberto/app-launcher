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

  WEBKIT_BUDGET=pass            exit 0  at least one test ran on WebKit and passed,
                                        and none failed
  WEBKIT_BUDGET=fail            exit 1  a budget test failed on WebKit
  WEBKIT_BUDGET=unknown         exit 3  NOT CONFIRMED: Playwright missing, a
                                        malformed `[webkit_budget] targets`, or a
                                        pytest run that never reported (no
                                        readable JUnit report included)
  WEBKIT_BUDGET=not-installed   exit 4  WebKit's browser build is not installed
                                        here, so nothing ran (never a pass:
                                        `python -m playwright install webkit`)
  WEBKIT_BUDGET=no-tests        exit 5  the repo has no request-budget test
  WEBKIT_BUDGET=no-browser-option exit 6 the repo's pytest has no `--browser`
                                        option (no pytest-playwright, its fixtures
                                        drive raw `playwright.sync_api`), so the
                                        tests cannot be pointed at WebKit from
                                        here; nothing ran
  WEBKIT_BUDGET=not-run         exit 7  the budget tests exist but not one passed:
                                        every test was skipped (or xfailed), or
                                        pytest collected none; the line above says
                                        which

A caller treats 0 as the only pass; 4, 5 and 6 are "not run" states it prints as
such, and 1, 3 and 7 fail the gate.

Vendor-verbatim (manifest key `webkit_budget_check`), stdlib-only apart from the
Playwright the repo's e2e suite already needs.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_UNKNOWN = 3
EXIT_NOT_INSTALLED = 4
EXIT_NO_TESTS = 5
EXIT_NO_BROWSER_OPTION = 6
EXIT_NOT_RUN = 7

PROBE_HELPER = "_request_budget.py"
# The probe runs a fake-clock minute per case; a hung browser must end the gate, not stall it.
RUN_TIMEOUT_S = 900
# `pytest --help` only imports the plugins and conftests, so it ends in seconds; a hang is "cannot say".
PROBE_TIMEOUT_S = 120
# The probe's output is captured and never shown, so its child gets no console window of its own.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
# `--browser` exactly: pytest-playwright also registers `--browser-channel`, which must not count.
_BROWSER_OPTION = re.compile(r"(?<![\w-])--browser(?![\w-])")
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


def has_browser_option(root: Path, python: str, targets: list[str]) -> bool | None:
    """Whether the repo's pytest accepts `--browser`; None when `pytest --help` cannot answer.

    The targets ride along so a `--browser` that a conftest next to them registers is listed too.
    """
    try:
        result = subprocess.run(  # noqa: S603
            [python, "-m", "pytest", *targets, "--help"], cwd=root, check=False, capture_output=True,
            text=True, timeout=PROBE_TIMEOUT_S, creationflags=NO_WINDOW)
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"⚠️ could not ask pytest for its options: {exc}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(f"⚠️ `pytest --help` ended with exit {result.returncode}", file=sys.stderr)
        return None
    return bool(_BROWSER_OPTION.search(result.stdout or ""))


def count_outcomes(report: Path) -> tuple[int, int] | None:
    """(passed, skipped) from the JUnit report pytest wrote; None when it cannot be read.

    pytest exits 0 for a run whose every test was skipped, so only the counts say whether anything passed.
    """
    try:
        suites = list(ET.parse(report).getroot().iter("testsuite"))  # noqa: S314 -- written by our own pytest run
        total = {key: sum(int(suite.attrib[key]) for suite in suites)
                 for key in ("tests", "skipped", "failures", "errors")}
    except (OSError, ET.ParseError, KeyError, ValueError):
        return None
    if not suites:
        return None
    return total["tests"] - total["skipped"] - total["failures"] - total["errors"], total["skipped"]


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

    browser_option = has_browser_option(root, python, targets)
    if browser_option is None:
        _verdict("unknown", "❌ NOT CONFIRMED: `pytest --help` did not answer, so the harness's `--browser` option is unknown")
        return EXIT_UNKNOWN
    if not browser_option:
        _verdict("no-browser-option",
                 "⚠️ this repo's pytest has no --browser option (no pytest-playwright): "
                 "the request budget was NOT run on WebKit")
        return EXIT_NO_BROWSER_OPTION

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
    with tempfile.TemporaryDirectory() as scratch:
        report = Path(scratch) / "webkit-budget.xml"
        try:
            # no-window-exempt: the run streams its output into the gate's own console, which a windowless child would not share
            result = subprocess.run(  # noqa: S603
                [python, "-m", "pytest", *targets, "--browser", "webkit", f"--junitxml={report}"],
                cwd=root, check=False, timeout=RUN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _verdict("unknown", f"❌ NOT CONFIRMED: the WebKit run did not finish within {RUN_TIMEOUT_S} s")
            return EXIT_UNKNOWN
        counts = count_outcomes(report)
    if result.returncode == 1:
        _verdict("fail", "❌ a request-budget test failed on WebKit")
        return EXIT_FAIL
    # 5 is pytest's "no tests collected", which a module-level skip also ends with.
    if result.returncode not in (0, 5):
        _verdict("unknown", f"❌ NOT CONFIRMED: pytest ended with exit {result.returncode} (not a plain pass or fail)")
        return EXIT_UNKNOWN
    if counts is None:
        _verdict("unknown", "❌ NOT CONFIRMED: pytest left no readable JUnit report, so how many tests passed is unknown")
        return EXIT_UNKNOWN
    passed, skipped = counts
    if result.returncode == 0 and passed > 0:
        _verdict("pass")
        return EXIT_PASS
    reason = f"every test was skipped ({skipped} skipped)" if skipped else "no tests collected"
    _verdict("not-run", f"❌ no request-budget test passed on WebKit: {reason}")
    return EXIT_NOT_RUN


def main(argv: list[str] | None = None) -> int:
    # Status lines carry emoji; a cp1252 pipe (a gate capturing without PYTHONUTF8) would crash instead of giving a verdict.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="the repo root (default: the current directory)")
    args = parser.parse_args(argv)
    return run(args.root.resolve(), sys.executable)


if __name__ == "__main__":
    sys.exit(main())
