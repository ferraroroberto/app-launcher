"""Prove a web app still looks the same: compare fresh screenshots with a committed baseline.

An agent that ships a move-only refactor (a stylesheet split, a markup move, a
script broken into modules) needs to show the app still renders the same
pixels, at phone and desktop width, light and dark, without a person checking
on a device. The e2e suite captures each screen through the vendored
`tests/e2e/_shot.py` helper into a committed baseline folder; this stage
compares what a run rewrote against what git holds, then puts the tree back.

Three modes (run from anywhere; `--root` defaults to this repo):

    python scripts/shot_baseline.py --check-tree --since <epoch-seconds>
    python scripts/shot_baseline.py --baseline
    python scripts/shot_baseline.py [--keep <dir>]

`--check-tree --since T` is the comparison alone, for a caller that has just
run the capture itself (the pre-ship gate records T before its e2e stage). A
committed shot whose file was written at or after T was recaptured by that run
and is compared; one written earlier was not recaptured (a routed partial e2e
slice) and is counted, never compared and never passed as identical.
`--baseline` runs the configured capture command, then does the same check.
With no mode flag the capture runs twice and the two runs are compared with
each other: that proves the capture is reproducible, which is what makes the
baseline comparison mean anything. Every mode restores the shots it rewrote
from the git index, so a verification run never leaves rewritten PNGs behind.
The index is the baseline, which equals HEAD in a clean tree and lets a staged
re-baseline be what is compared against.

A shot passes when it is byte-identical, or differs only as the rasteriser
differs from itself, within two narrow bands that are reported on every run:

  * `raster_max_delta` / `raster_max_pixels` -- many pixels off by a level or
    two along antialiased edges;
  * `glyph_max_delta` / `glyph_max_pixels` -- a few dozen pixels off by more,
    one icon stroke landing on the other side of a sub-pixel.

Real UI movement shifts whole glyphs and edges, hundreds of levels at a time,
so it fits under neither band. A shot whose content genuinely cannot be pinned
goes in `allowed_to_differ` by file name, with its reason, and is reported.

Opt-in, per adopter, in its own `.fleet.toml` -- re-vendoring never breaks a
repo that has not opted in:

    [shot_baseline]
    enabled           = true
    shots_dir         = "tests/e2e/shots"          # default
    capture           = ["-m", "pytest", "tests/e2e", "-q"]   # default; argv after the interpreter
    raster_max_delta  = 2                          # defaults: the measured noise floor
    raster_max_pixels = 500
    glyph_max_delta   = 24
    glyph_max_pixels  = 64

    [shot_baseline.allowed_to_differ]
    "clock-phone-light.png" = "renders the server's uptime, which no fixture pins"

Exit codes (a state is never folded into a pass):

  0  no recaptured shot changed in a way a reader could see, or the stage is
     not enabled (declared, not inferred)
  1  a shot drifted: the UI changed and the baseline owes a deliberate
     re-baseline, or something on screen is still unpinned
  2  bad configuration or arguments -- fail loud
  3  NOT CONFIRMED: the comparison could not be made -- git unusable, Pillow
     missing, no committed baseline, no shot recaptured, an unreadable image,
     or the capture command failed. The gate fails on it.

Vendor-verbatim: copied byte-for-byte into adopters (manifest key
`shot_baseline`); stdlib plus Pillow. Contract and re-baseline steps:
docs/playwright-ui-testing.md ("Screenshot baseline").
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SHOTS_DIR = "tests/e2e/shots"
DEFAULT_CAPTURE = ("-m", "pytest", "tests/e2e", "-q")
# Measured on a 211-shot suite captured with the `_shot.py` pins: real runs sit
# under 200 px at delta 1-2, one antialiased glyph edge at device scale 3 is at
# most 64 px at delta 14, and the smallest genuine change measured (one digit of
# a timestamp) was 35 px at delta 210 -- an order of magnitude above both bands.
DEFAULT_RASTER_MAX_DELTA = 2
DEFAULT_RASTER_MAX_PIXELS = 500
DEFAULT_GLYPH_MAX_DELTA = 24
DEFAULT_GLYPH_MAX_PIXELS = 64
GIT_TIMEOUT_S = 120
# The capture is a whole browser e2e run; a hung one must end as NOT CONFIRMED.
CAPTURE_TIMEOUT_S = 1800

EXIT_PASS, EXIT_DRIFT, EXIT_CONFIG, EXIT_NOT_CONFIRMED = 0, 1, 2, 3

# Vendor-verbatim module: derives the flag locally (the shared `src/no_window.py`
# would not resolve in a consumer tree), the documented exception.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_CONFIG_KEYS = {
    "enabled", "shots_dir", "capture", "allowed_to_differ",
    "raster_max_delta", "raster_max_pixels", "glyph_max_delta", "glyph_max_pixels",
}


class ConfigError(RuntimeError):
    """The `[shot_baseline]` table is unusable."""


class NotConfirmed(RuntimeError):
    """The comparison could not be made, so sameness is unknown."""


@dataclass(frozen=True)
class Config:
    shots_dir: str
    capture: tuple[str, ...]
    allowed_to_differ: dict[str, str]
    raster_max_delta: int
    raster_max_pixels: int
    glyph_max_delta: int
    glyph_max_pixels: int

    def band_text(self) -> str:
        return (f"<={self.raster_max_delta}/255 on <={self.raster_max_pixels} px, "
                f"or <={self.glyph_max_delta}/255 on <={self.glyph_max_pixels} px")


@dataclass
class Report:
    """One comparison's verdicts, by file name."""

    identical: list[str] = field(default_factory=list)
    raster: list[str] = field(default_factory=list)
    allowed: list[str] = field(default_factory=list)
    drifted: list[tuple[str, str]] = field(default_factory=list)
    unreadable: list[tuple[str, str]] = field(default_factory=list)

    @property
    def compared(self) -> int:
        return (len(self.identical) + len(self.raster) + len(self.allowed)
                + len(self.drifted) + len(self.unreadable))


# ----------------------------------------------------------------- config

def _int(table: dict[str, object], name: str, default: int, upper: int | None) -> int:
    value = table.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or (upper is not None and value > upper):
        bound = f" between 0 and {upper}" if upper is not None else " >= 0"
        raise ConfigError(f"[shot_baseline] {name} must be an integer{bound}")
    return value


def parse_config(toml_text: str) -> Config | None:
    """`[shot_baseline]` -> `Config`, or None when the stage is not enabled.

    An absent table, or `enabled = false`, is the one correct skip: the adopter
    declared it. Anything malformed raises `ConfigError` -- a typo'd key must not
    quietly run with defaults.
    """
    table = tomllib.loads(toml_text).get("shot_baseline")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError("[shot_baseline] must be a table")
    unknown = sorted(set(table) - _CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"[shot_baseline]: unknown key(s) {', '.join(unknown)}")
    enabled = table.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("[shot_baseline] enabled must be true or false")
    if not enabled:
        return None
    shots_dir = table.get("shots_dir", DEFAULT_SHOTS_DIR)
    if not isinstance(shots_dir, str) or not shots_dir.strip("/\\"):
        raise ConfigError("[shot_baseline] shots_dir must be a non-empty relative path")
    shots_dir = shots_dir.replace("\\", "/").strip("/")
    if shots_dir.startswith("..") or ":" in shots_dir:
        raise ConfigError("[shot_baseline] shots_dir must stay inside the repo")
    capture = table.get("capture", list(DEFAULT_CAPTURE))
    if not isinstance(capture, list) or not capture or not all(isinstance(a, str) and a for a in capture):
        raise ConfigError("[shot_baseline] capture must be a non-empty list of non-empty strings")
    allowed = table.get("allowed_to_differ", {})
    if not isinstance(allowed, dict) or not all(
        isinstance(k, str) and k.endswith(".png") and isinstance(v, str) and v.strip() for k, v in allowed.items()
    ):
        raise ConfigError('[shot_baseline.allowed_to_differ] maps "<name>.png" to a non-empty reason')
    return Config(
        shots_dir,
        tuple(capture),
        dict(allowed),
        _int(table, "raster_max_delta", DEFAULT_RASTER_MAX_DELTA, 255),
        _int(table, "raster_max_pixels", DEFAULT_RASTER_MAX_PIXELS, None),
        _int(table, "glyph_max_delta", DEFAULT_GLYPH_MAX_DELTA, 255),
        _int(table, "glyph_max_pixels", DEFAULT_GLYPH_MAX_PIXELS, None),
    )


def load_config(root: Path) -> Config | None:
    path = root / ".fleet.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return parse_config(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


# ------------------------------------------------------------- comparison

def visible_difference(a: Path, b: Path, cfg: Config) -> str | None:
    """None when the two images differ only as the rasteriser does, else why.

    Raises `NotConfirmed` when Pillow is missing or either file is not an image.
    """
    try:
        from PIL import Image, ImageChops
    except ImportError as exc:
        raise NotConfirmed("Pillow is not installed (pip install Pillow)") from exc
    try:
        with Image.open(a) as raw_a, Image.open(b) as raw_b:
            ia, ib = raw_a.convert("RGB"), raw_b.convert("RGB")
    except (OSError, ValueError) as exc:
        raise NotConfirmed(f"cannot read {a.name} as an image: {exc}") from exc
    if ia.size != ib.size:
        return f"size {ia.size} -> {ib.size}"
    diff = ImageChops.difference(ia, ib)
    bbox = diff.getbbox()
    if bbox is None:
        return None
    # One greyscale plane holding each pixel's worst channel delta, so the
    # histogram answers both questions at once: how far off, and how many.
    red, green, blue = diff.split()
    worst_channel = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    histogram = worst_channel.histogram()
    moved = sum(histogram[1:])
    worst = max(level for level, count in enumerate(histogram) if count)
    if worst <= cfg.raster_max_delta and moved <= cfg.raster_max_pixels:
        return None
    if worst <= cfg.glyph_max_delta and moved <= cfg.glyph_max_pixels:
        return None
    return f"{moved} px, worst channel delta {worst}, bbox {bbox}"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _judge(name: str, reference: Path, current: Path, cfg: Config, report: Report) -> None:
    """File one pair of images under exactly one verdict."""
    if _digest(reference) == _digest(current):
        report.identical.append(name)
        return
    if name in cfg.allowed_to_differ:
        report.allowed.append(name)
        return
    try:
        why = visible_difference(reference, current, cfg)
    except NotConfirmed as exc:
        report.unreadable.append((name, str(exc)))
        return
    if why is None:
        report.raster.append(name)
    else:
        report.drifted.append((name, why))


# -------------------------------------------------------------------- git

def _git(root: Path, *args: str) -> bytes:
    """One `git` call, bytes out (a PNG is not text); NotConfirmed when git is unusable."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, stdin=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_S, check=False, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NotConfirmed(f"git {' '.join(args)} could not run: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise NotConfirmed(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _git_paths(root: Path, command: str, *args: str) -> list[str]:
    """The `.png` paths a `git <command> -z ...` listing names, repo-relative."""
    out = _git(root, command, "-z", *args).decode("utf-8", "replace")
    return [p for p in out.split("\0") if p.endswith(".png")]


def _restore(root: Path, rels: list[str]) -> None:
    """Put the named tracked shots back to the index's copy; untracked files are never touched."""
    if rels:
        _git(root, "checkout", "--", *rels)


# ------------------------------------------------------------ check-tree

def _summary_line(total: int, recaptured: int, report: Report, new: int) -> str:
    return (f"{total} committed shot(s) · {recaptured} recaptured by this run · "
            f"{len(report.identical)} byte-identical · {len(report.raster)} raster-noise only · "
            f"{len(report.allowed)} on the stated allowlist · {len(report.drifted)} drifted · "
            f"{new} new (not committed) · {total - recaptured} not recaptured")


def _print_details(report: Report, cfg: Config) -> None:
    for name in report.raster:
        print(f"  raster:  {name} ({cfg.band_text()})")
    for name in report.allowed:
        print(f"  allowed: {name} -- {cfg.allowed_to_differ[name]}")
    for name, why in report.unreadable:
        print(f"  NOT CONFIRMED: {name} -- {why}")
    for name, why in report.drifted:
        print(f"  DRIFTED: {name} -- {why}")


def check_tree(root: Path, cfg: Config, since: float) -> int:
    """Compare every shot recaptured at or after *since* with the git index, then restore."""
    tracked = _git_paths(root, "ls-files", "--", cfg.shots_dir)
    untracked = _git_paths(root, "ls-files", "--others", "--exclude-standard", "--", cfg.shots_dir)
    if not tracked:
        raise NotConfirmed(f"git holds no baseline under {cfg.shots_dir}/ -- capture the screens, review "
                           f"them and commit the PNGs first")
    report = Report()
    recaptured: list[str] = []
    rewritten: list[str] = []
    with tempfile.TemporaryDirectory(prefix="shot-baseline-") as tmp:
        for rel in tracked:
            name = Path(rel).name
            current = root / rel
            if not current.is_file():
                report.drifted.append((name, "git holds it, the tree has no such file"))
                continue
            if current.stat().st_mtime < since:
                continue
            recaptured.append(rel)
            reference = Path(tmp) / name
            reference.write_bytes(_git(root, "show", f":{rel}"))
            if _digest(reference) != _digest(current):
                rewritten.append(rel)
            _judge(name, reference, current, cfg, report)
    _restore(root, rewritten)

    print(_summary_line(len(tracked), len(recaptured), report, len(untracked)))
    _print_details(report, cfg)
    for rel in untracked:
        print(f"  new:     {Path(rel).name} -- no committed twin; review it and commit it to baseline it")
    if rewritten:
        print(f"restored {len(rewritten)} rewritten shot(s) from the git index -- the tree is as you left it")

    if report.drifted:
        print(f"shot baseline: FAIL -- {len(report.drifted)} shot(s) drifted from the committed baseline.")
        print("  Either the UI changed and the baseline owes a deliberate re-baseline (run the capture, review "
              f"what moved, `git add {cfg.shots_dir}`), or something on screen is unpinned "
              "(a clock, a path, an animation): pin it in the capture, never widen the bands.")
        return EXIT_DRIFT
    if report.unreadable:
        print("shot baseline: NOT CONFIRMED -- an image could not be read, so sameness is unknown")
        return EXIT_NOT_CONFIRMED
    if not recaptured:
        print(f"shot baseline: NOT CONFIRMED -- this run recaptured none of the {len(tracked)} committed shot(s), "
              "so nothing was compared (did the capture run?)")
        return EXIT_NOT_CONFIRMED
    print(f"shot baseline: PASS -- {len(recaptured)} of {len(tracked)} committed shot(s) recaptured, 0 drifted")
    return EXIT_PASS


# ---------------------------------------------------------------- capture

def run_capture(root: Path, cfg: Config, label: str) -> float:
    """Run the capture command once; return the instant it started. NotConfirmed on failure."""
    print(f"=== {label}: {' '.join(cfg.capture)} ===", flush=True)
    started = time.time()
    try:
        result = subprocess.run(
            [sys.executable, *cfg.capture], cwd=str(root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=CAPTURE_TIMEOUT_S, check=False,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NotConfirmed(f"{label}: the capture command could not finish: {exc}") from exc
    # Captured rather than inherited: a CREATE_NO_WINDOW child's own console
    # reaches nobody, so a failed capture would otherwise lose its reason.
    output = ((result.stdout or "") + (result.stderr or "")).rstrip()
    if result.returncode != 0:
        print(output, flush=True)
        raise NotConfirmed(f"{label}: the capture command failed (exit {result.returncode}) -- fix the suite first")
    print("\n".join(output.splitlines()[-3:]), flush=True)
    return started


def _snapshot(root: Path, cfg: Config, since: float, into: Path) -> list[str]:
    """Copy every shot written at or after *since* into *into*; return their names."""
    into.mkdir(parents=True, exist_ok=True)
    names = []
    for png in sorted((root / cfg.shots_dir).glob("*.png")):
        if png.stat().st_mtime >= since:
            shutil.copy2(png, into / png.name)
            names.append(png.name)
    return names


def determinism(root: Path, cfg: Config, keep: Path | None) -> int:
    """Capture twice and compare the runs with each other, then restore the tree."""
    work = keep or Path(tempfile.mkdtemp(prefix="shot-determinism-"))
    first, second = work / "run-1", work / "run-2"
    started = time.time()
    try:
        names_1 = set(_snapshot(root, cfg, run_capture(root, cfg, "run 1"), first))
        names_2 = set(_snapshot(root, cfg, run_capture(root, cfg, "run 2"), second))
        report = Report()
        for name in sorted(names_1 | names_2):
            if name in names_1 and name in names_2:
                _judge(name, first / name, second / name, cfg, report)
            else:
                report.drifted.append((name, "written by only one of the two runs"))
    finally:
        # Only what these runs rewrote: an unstaged re-baseline from before them stays.
        _restore(root, [rel for rel in _git_paths(root, "diff", "--name-only", "--", cfg.shots_dir)
                        if (root / rel).is_file() and (root / rel).stat().st_mtime >= started])
        if keep is None:
            shutil.rmtree(work, ignore_errors=True)
    print(f"{report.compared} shot(s) · {len(report.identical)} byte-identical · {len(report.raster)} raster-noise "
          f"only · {len(report.allowed)} on the stated allowlist · {len(report.drifted)} moved")
    _print_details(report, cfg)
    if keep is not None:
        print(f"snapshots kept under {work}")
    if report.drifted:
        print("shot baseline: FAIL -- the capture is not reproducible; whatever moved is still in flight or "
              "unpinned when the shutter opens (see tests/e2e/_shot.py's settle())")
        return EXIT_DRIFT
    if report.unreadable or not report.compared:
        print("shot baseline: NOT CONFIRMED -- the two runs produced nothing comparable")
        return EXIT_NOT_CONFIRMED
    print(f"shot baseline: PASS -- {report.compared} shot(s), no visible change across two runs of the same commit")
    return EXIT_PASS


# ------------------------------------------------------------------- main

def run(root: Path, *, mode: str, since: float | None = None, keep: Path | None = None) -> int:
    try:
        cfg = load_config(root)
    except ConfigError as exc:
        print(f"shot baseline: CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    if cfg is None:
        print("shot baseline: not enabled (no [shot_baseline] enabled = true in .fleet.toml)")
        return EXIT_PASS
    try:
        if mode == "check-tree":
            assert since is not None
            return check_tree(root, cfg, since)
        if mode == "baseline":
            return check_tree(root, cfg, run_capture(root, cfg, "baseline run"))
        return determinism(root, cfg, keep)
    except NotConfirmed as exc:
        print(f"shot baseline: NOT CONFIRMED: {exc}")
        print("  (sameness could not be established, so this is neither a pass nor a drift)")
        return EXIT_NOT_CONFIRMED


def main(argv: list[str] | None = None) -> int:
    # The report uses non-ASCII separators; piped on Windows, stdout falls back
    # to cp1252 and `print` would raise after the verdict was already decided.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repo root (default: this repo)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-tree", action="store_true",
                      help="compare the shots a run just recaptured with the committed baseline (needs --since)")
    mode.add_argument("--baseline", action="store_true",
                      help="run the capture once, then compare it with the committed baseline")
    parser.add_argument("--since", type=float, default=None,
                        help="with --check-tree: epoch seconds the capture started; older files were not recaptured")
    parser.add_argument("--keep", type=Path, default=None,
                        help="determinism mode: keep both runs' snapshots in this directory")
    args = parser.parse_args(argv)
    if args.check_tree and args.since is None:
        parser.error("--check-tree needs --since <epoch seconds the capture started>")
    if args.since is not None and not args.check_tree:
        parser.error("--since only applies with --check-tree")
    if args.keep is not None and (args.check_tree or args.baseline):
        parser.error("--keep only applies to the two-run determinism mode")
    chosen = "check-tree" if args.check_tree else "baseline" if args.baseline else "determinism"
    return run(args.root.resolve(), mode=chosen, since=args.since, keep=args.keep)


if __name__ == "__main__":
    sys.exit(main())
