"""Test-to-code ratchet: the test suite may not grow faster than the product.

Counts physical lines of code in the files git tracks (plus files not yet added
that git would track), split into two sides:

  * test     -- everything under ``tests/``
  * product  -- everything under ``src/`` and ``app/``

Both sides count the same languages (``.py .js .mjs .cjs .css .html``), so the
JavaScript tests in ``tests/js`` weigh against the JavaScript they test. Data
fixtures (``.json``, ``.jsonl``), images and docs are not code and are left out.

The ratio ``test / product`` is compared with ``ratio_ceiling`` in the
``[test_weight]`` table of ``.fleet.toml``. It fails only when the ratio rises
above the ceiling; the ceiling moves down with ``--update`` (shrink-only, it
refuses to raise) and up only with the explicit ``--raise-ceiling`` flag, which
shows in review as a change to ``.fleet.toml``.

Exit codes (a state is never folded into a pass):

  0  pass: the ratio is at or under the ceiling, or the ceiling was rewritten
  1  the ratio is above the ceiling, or ``--update`` was refused because it
     would raise it
  2  bad configuration: ``.fleet.toml`` unreadable, or ``[test_weight]``
     missing or malformed
  3  NOT CONFIRMED: git unusable, a listed file unreadable, or no product
     lines found. The gate fails on it.

Usage
-----
    python -m scripts.suite_weight_ratchet                  # check (what the gate runs)
    python -m scripts.suite_weight_ratchet --update         # lower the ceiling to today's ratio
    python -m scripts.suite_weight_ratchet --raise-ceiling  # raise it; say why in the PR
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import tomllib
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.git_utils import run_git  # noqa: E402

logger = logging.getLogger(__name__)

TEST_PREFIXES = ("tests/",)
PRODUCT_PREFIXES = ("src/", "app/")
CODE_SUFFIXES = frozenset({".py", ".js", ".mjs", ".cjs", ".css", ".html"})
CEILING_PLACES = Decimal("0.0001")

EXIT_PASS, EXIT_ABOVE, EXIT_CONFIG, EXIT_NOT_CONFIRMED = 0, 1, 2, 3

_CEILING_LINE = re.compile(r'^(ratio_ceiling\s*=\s*)"[^"\n]*"', re.MULTILINE)


class ConfigError(RuntimeError):
    """``.fleet.toml`` or its ``[test_weight]`` table is unusable."""


class NotConfirmed(RuntimeError):
    """A count could not be established."""


@dataclass(frozen=True)
class Weight:
    test_lines: int
    product_lines: int

    @property
    def ratio(self) -> Decimal:
        return Decimal(self.test_lines) / Decimal(self.product_lines)

    def ceiling_value(self) -> Decimal:
        """Today's ratio rounded up, so writing it as the ceiling passes today."""
        return self.ratio.quantize(CEILING_PLACES, rounding=ROUND_CEILING)


def side_of(path: str) -> Optional[str]:
    """``"test"``, ``"product"`` or None for a repo-relative posix path."""
    if PurePosixPath(path).suffix.lower() not in CODE_SUFFIXES:
        return None
    if path.startswith(TEST_PREFIXES):
        return "test"
    if path.startswith(PRODUCT_PREFIXES):
        return "product"
    return None


def count_lines(data: bytes) -> int:
    """Physical lines, counting a last line without a trailing newline."""
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def _git_paths(root: Path, args: list[str]) -> set[str]:
    out = run_git(root, ["ls-files", "-z", *args], warn_on_failure=True)
    if out is None:
        raise NotConfirmed(f"git ls-files {' '.join(args)} failed")
    return {p for p in out.split("\0") if p}


def measure(root: Path) -> Weight:
    """Count both sides over the files git tracks or would track."""
    listed = _git_paths(root, ["--cached", "--others", "--exclude-standard"])
    deleted = _git_paths(root, ["--deleted"])
    totals = {"test": 0, "product": 0}
    for path in sorted(listed - deleted):
        side = side_of(path)
        if side is None:
            continue
        try:
            data = (root / path).read_bytes()
        except OSError as exc:
            raise NotConfirmed(f"cannot read {path}: {exc}") from exc
        totals[side] += count_lines(data)
    if totals["product"] == 0:
        raise NotConfirmed("no product lines found; the ratio has no denominator")
    return Weight(totals["test"], totals["product"])


def read_ceiling(toml_text: str) -> Decimal:
    table = tomllib.loads(toml_text).get("test_weight")
    if not isinstance(table, dict):
        raise ConfigError("[test_weight] table missing from .fleet.toml")
    raw = table.get("ratio_ceiling")
    if not isinstance(raw, str):
        raise ConfigError('[test_weight] ratio_ceiling must be a quoted decimal, e.g. "1.1400"')
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ConfigError(f"[test_weight] ratio_ceiling {raw!r} is not a decimal") from exc
    if not value.is_finite() or value <= 0:
        raise ConfigError(f"[test_weight] ratio_ceiling {raw!r} must be a positive decimal")
    return value


def write_ceiling(toml_text: str, value: Decimal) -> str:
    """``toml_text`` with the one ``ratio_ceiling`` line set to ``value``."""
    new_text, n = _CEILING_LINE.subn(lambda m: f'{m.group(1)}"{value}"', toml_text)
    if n != 1:
        raise ConfigError(f"expected exactly one ratio_ceiling line in .fleet.toml, found {n}")
    return new_text


def run(root: Path, mode: str) -> int:
    toml_path = root / ".fleet.toml"
    try:
        toml_text = toml_path.read_text(encoding="utf-8")
        ceiling = read_ceiling(toml_text)
    except (OSError, tomllib.TOMLDecodeError, ConfigError) as exc:
        logger.error(f"suite weight ratchet: config error: {exc}")
        return EXIT_CONFIG
    try:
        weight = measure(root)
    except NotConfirmed as exc:
        logger.error(f"suite weight ratchet: NOT CONFIRMED: {exc}")
        return EXIT_NOT_CONFIRMED

    logger.info(
        f"suite weight: {weight.test_lines} test lines / {weight.product_lines} product lines"
        f" = {weight.ratio:.6f} (ceiling {ceiling})"
    )
    if mode == "check":
        if weight.ratio > ceiling:
            logger.error(
                "suite weight ratchet FAILED: the tests grew faster than the product."
                " Trim tests, or raise the ceiling with --raise-ceiling and say why in the PR."
            )
            return EXIT_ABOVE
        return EXIT_PASS

    target = weight.ceiling_value()
    if mode == "update" and target > ceiling:
        logger.error(f"refused: --update only lowers the ceiling; today's {target} is above {ceiling}.")
        return EXIT_ABOVE
    if target == ceiling:
        logger.info("ceiling unchanged.")
        return EXIT_PASS
    try:
        toml_path.write_text(write_ceiling(toml_text, target), encoding="utf-8")
    except (OSError, ConfigError) as exc:
        logger.error(f"suite weight ratchet: config error: {exc}")
        return EXIT_CONFIG
    logger.info(f"ceiling {ceiling} -> {target} written to .fleet.toml.")
    return EXIT_PASS


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--update", action="store_true", help="lower the ceiling to today's ratio")
    group.add_argument("--raise-ceiling", action="store_true", help="set the ceiling to today's ratio even when higher")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    mode = "raise" if args.raise_ceiling else "update" if args.update else "check"
    return run(PROJECT_ROOT, mode)


if __name__ == "__main__":
    sys.exit(main())
