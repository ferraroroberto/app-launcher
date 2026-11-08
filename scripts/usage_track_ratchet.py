"""Ratchet on usage tracking: every UX control and sheet reaches the usage log under a constant id.

The fleet's usage log only sees what the markup names: a tap on a control that
carries no `data-track` id is invisible, a sheet that records no view bills its
minutes to the tab under it, and an id built from content breaks the privacy
contract. This stage reads a web app's HTML and JS, counts each rule's hits per
file against a committed baseline, and fails only when a count rises. Existing
debt never blocks work, but it cannot grow.

The rules (docs/usage-track-ratchet.md quotes the convention each one holds):

  * `untracked-control` -- a control (`<button>`, `<summary>`, `<select>`, an
    `<input>` of type button/submit/reset/checkbox/radio/image, an element whose
    `role` is a control role, an `<a>` whose `href` is `#...` or `javascript:`)
    with no constant `data-track` of its own and no tracked control or grouping
    parent (`role` group/radiogroup/tablist/toolbar/menu/menubar/listbox). A nav
    tab (`data-tab` inside a `<nav>`) is a view, not a tap. In JS: a control in a
    string or template literal, a `createElement` control whose variable gets no
    `dataset.track` / `setAttribute('data-track', ...)` later in the same block,
    and a call to a declared builder (`builders`) with no `track:` key;
  * `untracked-sheet` -- a `<dialog>` (in HTML, in a JS string, or from
    `createElement('dialog')`) with no `data-track-view`;
  * `dynamic-id` -- a `data-track` / `data-track-view` value, a `dataset.track(View)`
    or `setAttribute('data-track...')` value, a `.track(...)` / `.trackView(...)`
    argument or a builder's `track:` value that is not a string literal matching
    the id pattern, or a name bound to a module-level `const` string literal;
  * `usage-log-wired` -- repo level and never baselined: `[vendored]` declares
    `usage-log` and `usage_log`, a file in scope calls `initUsageLog(`, and a
    tracked Python file calls `usage_router(` (the vendored `usage_log_starlette`
    adapter counts when `[vendored]` declares it).

A single hit keeps its exception with a `<rule>-exempt: <reason>` comment (`//`
in JS, `<!-- -->` in HTML) on its own line or in the comment block right above
it; a marker with no reason exempts nothing.

Opt-in, per adopter, in its own `.fleet.toml`:

    [usage_track_ratchet]
    enabled  = true
    baseline = ".usage-track-ratchet-baseline.json"           # default
    rules    = ["untracked-control", "untracked-sheet", "dynamic-id", "usage-log-wired"]
    paths    = ["app/webapp/static/**", "app/webapp/templates/**"]  # default
    exclude  = ["app/webapp/static/vendor/**"]                 # globs; never scanned
    builders = ["app/webapp/static/row.js:row"]                # control builders whose callers pass `track:`

Exit codes: 0 pass or not enabled; 1 a count rose, the wiring is missing, or the
baseline is missing / a mode was refused; 2 bad configuration, baseline or
arguments; 3 NOT CONFIRMED (`git` unusable, a file that cannot be read or
decoded, a JS file whose strings, comments or braces do not close) -- the gate
fails on it. Modes: `check`, `--init`, `--update-baseline` (shrink only),
`--update-baseline --allow-additions` (shows in review).

Vendor-verbatim (manifest key `usage_track_ratchet`) with its sibling reader
`usage_track_scan.py` (manifest key `usage_track_scan`), stdlib-only. Procedure
and limits: docs/usage-track-ratchet.md.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import ModuleType


def _load_sibling(name: str) -> ModuleType:
    """A sibling module, loaded by path so `python -P` (scripts/ off `sys.path`) finds it."""
    try:
        if __package__:  # imported as a package member (the tests)
            return importlib.import_module(f"{__package__}.{name}")
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
        assert spec is not None and spec.loader is not None
        module = sys.modules[spec.name] = importlib.util.module_from_spec(spec)  # a dataclass resolves its module by name
        spec.loader.exec_module(module)
        return module
    except (ImportError, OSError, SyntaxError):
        print(f"usage track ratchet: NOT CONFIRMED: {name}.py is missing beside this script -- vendor both "
              "(manifest keys usage_track_ratchet and usage_track_scan)")
        sys.exit(3)


scan = _load_sibling("usage_track_scan")
ConfigError: type[RuntimeError] = scan.ConfigError
NotConfirmed: type[RuntimeError] = scan.NotConfirmed
HTML_SUFFIXES: tuple[str, ...] = scan.HTML_SUFFIXES
JS_SUFFIXES: tuple[str, ...] = scan.JS_SUFFIXES

REPO_ROOT = Path(__file__).resolve().parent.parent

BASELINE_VERSION = 1
DEFAULT_BASELINE = ".usage-track-ratchet-baseline.json"
DEFAULT_PATHS = ("app/webapp/static/**", "app/webapp/templates/**")
#: Always excluded, whatever the adopter's own `exclude` adds; a minified file is third-party output.
BUILTIN_EXCLUDES = ("**/node_modules/**", "**/.venv/**", "**/venv/**", "**/*.min.js")
GIT_TIMEOUT_S = 120

RULES = ("untracked-control", "untracked-sheet", "dynamic-id", "usage-log-wired")
#: Counted per file against the baseline; `usage-log-wired` is a fact about the repo and never baselined.
FILE_RULES = ("untracked-control", "untracked-sheet", "dynamic-id")
WIRED = "usage-log-wired"

_CLIENT_KEY, _SERVER_KEY, _ADAPTER_KEY = "usage-log", "usage_log", "usage_log_starlette"
_ROUTER_CALL = re.compile(r"(?<!def )\busage_router\(")

EXIT_PASS, EXIT_RISE, EXIT_CONFIG, EXIT_NOT_CONFIRMED = 0, 1, 2, 3

# Vendor-verbatim module: derives the flag locally (the shared `src/no_window.py`
# would not resolve in a consumer tree), the documented exception.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_CONFIG_KEYS = {"enabled", "baseline", "rules", "paths", "exclude", "builders"}


@dataclass(frozen=True)
class Config:
    baseline: str
    rules: tuple[str, ...]
    paths: tuple[str, ...] = DEFAULT_PATHS
    exclude: tuple[str, ...] = ()
    builders: tuple[tuple[str, str], ...] = ()  # (file, function name)


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    rule: str
    message: str


# ----------------------------------------------------------------- config

def _str_list(table: dict[str, object], name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if name not in table:
        return default
    value = table[name]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"[usage_track_ratchet] {name} must be a list of non-empty strings")
    return tuple(v.replace("\\", "/") for v in value)


def _builders(table: dict[str, object]) -> tuple[tuple[str, str], ...]:
    out = []
    for entry in _str_list(table, "builders", ()):
        path, sep, name = entry.rpartition(":")
        if not sep or not path or not re.fullmatch(r"[A-Za-z_$][\w$]*", name):
            raise ConfigError(f"[usage_track_ratchet] builders entry {entry!r} must be `<file>:<function name>`")
        out.append((path, name))
    return tuple(out)


def parse_config(toml_text: str) -> Config | None:
    """`[usage_track_ratchet]` -> `Config`, or None when the stage is not enabled.

    An absent table, or `enabled = false`, is the one correct skip: the adopter
    declared it. Anything malformed raises `ConfigError` -- a typo'd key or rule
    must not quietly run with less than the adopter asked for.
    """
    table = tomllib.loads(toml_text).get("usage_track_ratchet")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError("[usage_track_ratchet] must be a table")
    unknown = sorted(set(table) - _CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"[usage_track_ratchet]: unknown key(s) {', '.join(unknown)}")
    enabled = table.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("[usage_track_ratchet] enabled must be true or false")
    if not enabled:
        return None
    baseline = table.get("baseline", DEFAULT_BASELINE)
    if not isinstance(baseline, str) or not baseline:
        raise ConfigError("[usage_track_ratchet] baseline must be a non-empty string")
    rules = _str_list(table, "rules", ())
    if not rules:
        raise ConfigError(f"[usage_track_ratchet] rules must name at least one of: {', '.join(RULES)}")
    bad = sorted(set(rules) - set(RULES))
    if bad:
        raise ConfigError(f"[usage_track_ratchet] unknown rule(s) {', '.join(bad)}; known: {', '.join(RULES)}")
    paths = _str_list(table, "paths", DEFAULT_PATHS)
    if not paths:
        raise ConfigError("[usage_track_ratchet] paths must name at least one glob")
    return Config(baseline, tuple(r for r in RULES if r in rules), paths, _str_list(table, "exclude", ()),
                  _builders(table))


def _vendored(toml_text: str) -> dict[str, object]:
    table = tomllib.loads(toml_text).get("vendored")
    return table if isinstance(table, dict) else {}


def vendored_dests(toml_text: str) -> tuple[str, ...]:
    found = []
    for entry in _vendored(toml_text).values():
        value = entry.get("dest") if isinstance(entry, dict) else None
        if isinstance(value, str) and value.strip("/"):
            found.append(value.replace("\\", "/").strip("/"))
    return tuple(sorted(found))


def adapter_dest(toml_text: str) -> str | None:
    """Where `[vendored]` puts the Starlette adapter, which mounts `usage_router(` for the adopter; None if not declared."""
    entry = _vendored(toml_text).get(_ADAPTER_KEY)
    value = entry.get("dest") if isinstance(entry, dict) else None
    return value.replace("\\", "/").strip("/") if isinstance(value, str) and value.strip("/") else None


def load_config(root: Path) -> tuple[Config | None, tuple[str, ...], frozenset[str], str | None]:
    """(config or None when not enabled, `[vendored]` dests, `[vendored]` manifest keys, adapter dest)."""
    path = root / ".fleet.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return parse_config(text), vendored_dests(text), frozenset(_vendored(text)), adapter_dest(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


# ------------------------------------------------------------------- globs

def _glob_regex(glob: str) -> re.Pattern[str]:
    """Repo-root-anchored glob -> regex; `**/` is zero or more directories."""
    out: list[str] = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def matches_any(text: str, globs: tuple[str, ...]) -> bool:
    return any(_glob_regex(g).match(text) for g in globs)


def _under(path: str, prefix: str) -> bool:
    prefix = prefix.replace("\\", "/").strip("/")
    return path == prefix or path.startswith(prefix + "/")


def is_test_path(path: str) -> bool:
    return any(p in ("tests", "test") for p in PurePosixPath(path).parts[:-1])


def in_scope(path: str, cfg: Config, vendored: tuple[str, ...]) -> bool:
    """Is `path` an HTML or JS file this stage scans? A vendored copy's debt belongs to the repo it came from."""
    if not path.lower().endswith(HTML_SUFFIXES + JS_SUFFIXES) or is_test_path(path):
        return False
    if any(_under(path, d) for d in vendored) or not matches_any(path, cfg.paths):
        return False
    return not matches_any(path, BUILTIN_EXCLUDES + cfg.exclude)



def scan_source(text: str, path: str, cfg: Config) -> tuple[list[Hit], bool]:
    """(hits of the enabled per-file rules in one file, does it call `initUsageLog(`)."""
    raw, inits = scan.scan_text(text, path, cfg.rules, cfg.builders)
    return [Hit(path, line, rule, message) for line, rule, message in raw], inits


# --------------------------------------------------------------------- git

def _git(root: Path, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")  # a read-only sweep must not leave index locks
    try:
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, env=env, stdin=subprocess.DEVNULL,
            creationflags=_NO_WINDOW, timeout=GIT_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise NotConfirmed(f"git {args[0]} timed out after {GIT_TIMEOUT_S}s") from exc
    except OSError as exc:
        raise NotConfirmed(f"git could not start: {exc}") from exc


def tracked_files(root: Path) -> list[str]:
    """Repo-relative, forward-slash paths of every tracked or untracked-but-not-ignored file."""
    args = ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    proc = _git(root, args)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise NotConfirmed(f"git {' '.join(args)} exited {proc.returncode}: {detail[-1] if detail else '(no output)'}")
    return sorted(p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p)


def _read_text(root: Path, path: str) -> str | None:
    """A file's text, None when it is tracked but deleted in the working tree."""
    try:
        return (root / path).read_bytes().decode("utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise NotConfirmed(f"{path}: cannot be read: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise NotConfirmed(f"{path}: is not UTF-8 (byte {exc.start})") from exc


@dataclass
class Tree:
    """One scan of the repo: hits per in-scope file (only files with one), and what the wiring rule needs."""

    found: dict[str, list[Hit]]
    inits: bool
    router: bool


def scan_tree(root: Path, cfg: Config, vendored: tuple[str, ...], adapter: str | None = None) -> Tree:
    """Every in-scope HTML/JS file, plus the tracked Python files the wiring rule reads for `usage_router(`.

    The vendored Starlette adapter (`adapter`, its `[vendored]` dest) is the one vendored
    file read for that call: it mounts the router on behalf of an app that has no FastAPI.

    Only HTML, JS and Python source is read: a baseline, this stage's or any
    other ratchet's, is JSON and never counts as a hit or as evidence against one.
    """
    paths = tracked_files(root)
    known = set(paths)
    for file, name in cfg.builders:
        if file not in known:
            raise ConfigError(f"[usage_track_ratchet] builder `{name}` names {file}, which git does not list")
    tree = Tree({}, False, False)
    for path in paths:
        if in_scope(path, cfg, vendored):
            text = _read_text(root, path)
            if text is None:
                continue
            hits, inits = scan_source(text, path, cfg)
            tree.inits |= inits
            if hits:
                tree.found[path] = hits
        elif (WIRED in cfg.rules and path.endswith(".py") and not tree.router and not is_test_path(path)
              and (path == adapter or not any(_under(path, d) for d in vendored))):
            text = _read_text(root, path)
            tree.router = text is not None and _ROUTER_CALL.search(text) is not None
    return tree


def wiring_problems(tree: Tree, vendored_keys: frozenset[str]) -> list[str]:
    """Why the app does not reach its usage log; empty when it does."""
    problems = [f"`[vendored]` does not declare `{key}` ({role})"
                for key, role in ((_CLIENT_KEY, "the client"), (_SERVER_KEY, "the server"))
                if key not in vendored_keys]
    if not tree.inits:
        problems.append("no file in `paths` calls `initUsageLog(`")
    if not tree.router:
        problems.append("no tracked Python file calls `usage_router(`, so `/api/usage/*` is not mounted")
    return problems


# ---------------------------------------------------------------- baseline

Counts = dict[str, Counter[str]]  # path -> Counter(rule)


def current_counts(found: dict[str, list[Hit]]) -> Counts:
    return {path: Counter(h.rule for h in hits) for path, hits in found.items()}


def read_baseline(path: Path) -> Counts:
    """Baseline file -> ``{source path: Counter(rule)}``. Raises `ConfigError` on anything off."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read baseline {path}: {exc}") from exc
    except ValueError as exc:
        raise ConfigError(f"baseline {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != BASELINE_VERSION:
        raise ConfigError(f"baseline {path}: expected an object with version {BASELINE_VERSION}")
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        raise ConfigError(f"baseline {path}: `entries` must be a list")
    out: Counts = {}
    for item in raw_entries:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            raise ConfigError(f"baseline {path}: malformed entry {item!r}")
        counts = item.get("counts")
        if (not isinstance(counts, dict) or not counts
                or not all(r in FILE_RULES and isinstance(n, int) and not isinstance(n, bool) and n > 0
                           for r, n in counts.items())):
            raise ConfigError(f"baseline {path}: entry {item!r} needs `counts` of per-file rules to positive integers")
        out[item["key"]] = Counter(counts)
    return out


def render_baseline(counts: Counts) -> str:
    """Sorted, one file per line, no line numbers, so a baseline diff reviews cleanly."""
    lines = []
    for key in sorted(counts):
        record = {"key": key, "counts": {r: counts[key][r] for r in FILE_RULES if counts[key][r]}}
        lines.append("    " + json.dumps(record, ensure_ascii=False))
    body = "[\n" + ",\n".join(lines) + "\n  ]" if lines else "[]"
    return f'{{\n  "version": {BASELINE_VERSION},\n  "entries": {body}\n}}\n'


def diff(current: Counts, baseline: Counts) -> tuple[Counts, int]:
    """(per file, the rule counts beyond the baseline; how many baselined hits are gone)."""
    added: Counts = {}
    gone = 0
    for path in sorted(set(current) | set(baseline)):
        cur, base = current.get(path, Counter()), baseline.get(path, Counter())
        extra = cur - base
        if extra:
            added[path] = extra
        gone += sum((base - cur).values())
    return added, gone


# -------------------------------------------------------------------- CLI

def _total(counts: Counts) -> int:
    return sum(sum(c.values()) for c in counts.values())


def _report_added(added: Counts, found: dict[str, list[Hit]], baseline: Counts) -> None:
    print(f"usage track ratchet: {_total(added)} hit(s) beyond the baseline, in {len(added)} file(s):")
    for path, extra in added.items():
        for rule in FILE_RULES:
            if not extra[rule]:
                continue
            had = baseline.get(path, Counter())[rule]
            print(f"  {path}: [{rule}] {had + extra[rule]} hit(s), baseline {had}:")
            for h in found[path]:
                if h.rule == rule:
                    print(f"    {path}:{h.line}: [{rule}] {h.message}")


def _check(cfg: Config, tree: Tree, current: Counts, baseline: Counts, vendored_keys: frozenset[str]) -> int:
    """The gate's mode: the wiring (never baselined), then every file's counts against the baseline."""
    added, gone = diff(current, baseline)
    problems = wiring_problems(tree, vendored_keys) if WIRED in cfg.rules else []
    if problems:
        print(f"usage track ratchet: [{WIRED}] the app does not reach its usage log:")
        for message in problems:
            print(f"  {message}")
    if added:
        _report_added(added, tree.found, baseline)
        print("  Give each control a constant `data-track` id and each <dialog> a `data-track-view` "
              "(docs/usage-track-ratchet.md). A hit that must stay carries `<rule>-exempt: <reason>`; "
              "or (reviewed) raise the baseline with `--update-baseline --allow-additions`.")
    if added or problems:
        return EXIT_RISE
    print(f"usage track ratchet: PASS ({_total(current)} baselined hit(s) in {len(current)} file(s), none new; "
          f"rules: {', '.join(cfg.rules)})")
    if gone:
        print(f"  {gone} baselined hit(s) are gone -- run "
              "`python scripts/usage_track_ratchet.py --update-baseline` to lower it")
    return EXIT_PASS


def _load_baseline(baseline_path: Path, cfg: Config, init: bool) -> Counts | int:
    """The committed baseline (empty for `--init`), or the exit code that refuses the run."""
    if init:
        if baseline_path.exists():
            print(f"usage track ratchet: {cfg.baseline} already exists; --init never overwrites it")
            return EXIT_CONFIG
        return {}
    if not baseline_path.is_file():
        print(f"usage track ratchet: FAIL: baseline {cfg.baseline} is missing -- "
              "run `python scripts/usage_track_ratchet.py --init`")
        return EXIT_RISE
    try:
        return read_baseline(baseline_path)
    except ConfigError as exc:
        print(f"usage track ratchet: BASELINE ERROR: {exc}")
        return EXIT_CONFIG


def run(root: Path, *, init: bool, update: bool, allow_additions: bool) -> int:
    try:
        cfg, vendored, vendored_keys, adapter = load_config(root)
    except ConfigError as exc:
        print(f"usage track ratchet: CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    if cfg is None:
        if init or update:
            print("usage track ratchet: not enabled -- set [usage_track_ratchet] enabled = true in .fleet.toml first")
            return EXIT_CONFIG
        print("usage track ratchet: not enabled (no [usage_track_ratchet] enabled = true in .fleet.toml)")
        return EXIT_PASS
    baseline_path = root / cfg.baseline
    baseline = _load_baseline(baseline_path, cfg, init)
    if isinstance(baseline, int):
        return baseline

    try:
        tree = scan_tree(root, cfg, vendored, adapter)
    except ConfigError as exc:
        print(f"usage track ratchet: CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    except NotConfirmed as exc:
        print(f"usage track ratchet: NOT CONFIRMED: {exc}")
        print("  (the tree could not be read through, so this is neither a pass nor a clean tree)")
        return EXIT_NOT_CONFIRMED
    current = current_counts(tree.found)

    if init:
        # runtime-write-exempt: the repo's own baseline file, named by the config, never a code folder
        baseline_path.write_text(render_baseline(current), encoding="utf-8", newline="\n")
        print(f"usage track ratchet: wrote {cfg.baseline} with {_total(current)} hit(s) in {len(current)} file(s)")
        return EXIT_PASS
    if not update:
        return _check(cfg, tree, current, baseline, vendored_keys)
    added, gone = diff(current, baseline)
    if added and not allow_additions:
        _report_added(added, tree.found, baseline)
        print("usage track ratchet: nothing written -- fix the code, or raise the baseline with --allow-additions")
        return EXIT_RISE
    # runtime-write-exempt: the repo's own baseline file, named by the config, never a code folder
    baseline_path.write_text(render_baseline(current), encoding="utf-8", newline="\n")
    print(f"usage track ratchet: {cfg.baseline} now has {_total(current)} hit(s) in {len(current)} file(s) "
          f"({gone} dropped, {_total(added)} added)")
    return EXIT_PASS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repo root (default: this repo)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--init", action="store_true", help="write the first baseline; never overwrites")
    mode.add_argument("--update-baseline", action="store_true", help="drop hits now gone (shrink only)")
    parser.add_argument("--allow-additions", action="store_true",
                        help="with --update-baseline: also add hits (shows in review)")
    args = parser.parse_args(argv)
    if args.allow_additions and not args.update_baseline:
        parser.error("--allow-additions only applies with --update-baseline")
    return run(args.root.resolve(), init=args.init, update=args.update_baseline,
               allow_additions=args.allow_additions)


if __name__ == "__main__":
    # Reports contain non-ASCII (e.g. "→"); a cp1252 console would otherwise crash after the verdict.
    for _stream in (sys.stdout, sys.stderr):
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # real console streams are TextIOWrapper
    sys.exit(main())
