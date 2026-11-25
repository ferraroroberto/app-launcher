"""Ratchet on the UX surface map: every `data-surface` id in the code has a map entry, and back.

A web app's `docs/ux-surfaces.yaml` lists every surface a person can reach,
one entry per stable id, and generates the readable `docs/ux-surface-map.md`;
the code marks each surface root with the same id as `data-surface="<id>"`.
This stage reads the app's HTML and JS and the map, counts each rule's hits per
file against a committed baseline, and fails only when a count rises. Existing
debt never blocks work, but it cannot grow.

The rules (docs/ux-surface-ratchet.md quotes the convention each one holds):

  * `unmarked-surface` -- an element that is a surface by its markup (a
    `<dialog>`, a `role` of dialog/alertdialog/menu/tabpanel, a `popover`
    attribute; in HTML, in a JS string or template, or from `createElement`
    with that role or `popover` set in the same block) with no `data-surface`;
  * `invalid-surface-id` -- a `data-surface` value, a `dataset.surface =` or
    `setAttribute('data-surface', ...)` value that is not a string literal
    matching the id pattern (or a module-level `const` bound to one), or a map
    entry whose id does not match it;
  * `unmapped-surface` -- an id the code declares that no map entry lists;
  * `stale-entry` -- a map entry no code declares, unless its `root:` is
    `none: <reason>`;
  * `duplicate-surface` -- an id declared on more than one element in the
    code, or listed by more than one map entry (every site is a hit);
  * `dangling-link` -- a map entry's `links` `to:` id that no entry lists;
  * `stale-generated` -- the committed page is not what `ux_surface_map.py`
    generates from the map (line endings aside), or it is missing.

A single hit keeps its exception with a `<rule>-exempt: <reason>` comment (`//`
in JS, `<!-- -->` in HTML, `#` in the map) on its own line or in the comment
block right above it; a marker with no reason exempts nothing.

Opt-in, per adopter, in its own `.fleet.toml`:

    [ux_surface_ratchet]
    enabled  = true
    baseline = ".ratchets/ux-surface.json"            # default
    map      = "docs/ux-surfaces.yaml"                         # default: the source
    page     = "docs/ux-surface-map.md"                        # default: generated from it
    rules    = ["unmarked-surface", "invalid-surface-id", "unmapped-surface", "stale-entry", "duplicate-surface",
                "dangling-link", "stale-generated"]             # default: all
    paths    = ["app/webapp/static/**", "app/webapp/templates/**"]  # default
    exclude  = ["app/webapp/static/vendor/**"]                 # globs; never scanned

Exit codes: 0 pass or not enabled; 1 a count rose, the map or the baseline is
missing or invalid, or a mode was refused; 2 bad configuration, baseline or arguments; 3
NOT CONFIRMED (`git` unusable, a file that cannot be read or decoded, a JS file
whose strings, comments or braces do not close) -- the gate fails on it. Modes:
`check`, `--init`, `--update-baseline` (shrink only),
`--update-baseline --allow-additions` (shows in review).

Vendor-verbatim (manifest key `ux_surface_ratchet`), stdlib-only; it reads JS
through the usage track ratchet's tokenizer, `usage_track_scan.py` (manifest key
`usage_track_scan`), and the map through `ux_surface_map.py` (manifest key
`ux_surface_map`). Procedure and limits: docs/ux-surface-ratchet.md.
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
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any


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
        print(f"ux surface ratchet: NOT CONFIRMED: {name}.py is missing beside this script -- vendor it with its "
              "siblings (manifest keys ux_surface_ratchet, usage_track_scan, ux_surface_map, ratchet_baseline)")
        sys.exit(3)


scan = _load_sibling("usage_track_scan")
smap = _load_sibling("ux_surface_map")
_baseline = _load_sibling("ratchet_baseline")  # where the baseline lives, and its move into .ratchets/
NotConfirmed: type[RuntimeError] = scan.NotConfirmed

REPO_ROOT = Path(__file__).resolve().parent.parent

BASELINE_VERSION = 1
DEFAULT_BASELINE = ".ratchets/ux-surface.json"
DEFAULT_MAP: str = smap.DEFAULT_MAP
DEFAULT_PAGE: str = smap.DEFAULT_PAGE
DEFAULT_PATHS = ("app/webapp/static/**", "app/webapp/templates/**")
#: Always excluded, whatever the adopter's own `exclude` adds; a minified file is third-party output.
BUILTIN_EXCLUDES = ("**/node_modules/**", "**/.venv/**", "**/venv/**", "**/*.min.js")
GIT_TIMEOUT_S = 120

RULES = ("unmarked-surface", "invalid-surface-id", "unmapped-surface", "stale-entry", "duplicate-surface",
         "dangling-link", "stale-generated")
ATTR = "data-surface"
#: The e2e capture hook's name form (tests/e2e/_surface.py), so one id names a surface in the map, the
#: markup and the `surface(page, ...)` capture.
SURFACE_ID: re.Pattern[str] = smap.SURFACE_ID
#: Roles that make an element a surface by its markup; screens and drill-ins are surfaces by declaration only.
SURFACE_ROLES = frozenset({"dialog", "alertdialog", "menu", "tabpanel"})
HTML_SUFFIXES: tuple[str, ...] = scan.HTML_SUFFIXES
JS_SUFFIXES: tuple[str, ...] = scan.JS_SUFFIXES

EXIT_PASS, EXIT_RISE, EXIT_CONFIG, EXIT_NOT_CONFIRMED = 0, 1, 2, 3

# Vendor-verbatim module: derives the flag locally (the shared `src/no_window.py`
# would not resolve in a consumer tree), the documented exception.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_CONFIG_KEYS = {"enabled", "baseline", "map", "page", "rules", "paths", "exclude"}


class ConfigError(RuntimeError):
    """The `[ux_surface_ratchet]` table or the baseline file is unusable."""


@dataclass(frozen=True)
class Config:
    baseline: str = DEFAULT_BASELINE
    rules: tuple[str, ...] = RULES
    map: str = DEFAULT_MAP
    paths: tuple[str, ...] = DEFAULT_PATHS
    exclude: tuple[str, ...] = ()
    page: str = DEFAULT_PAGE


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    rule: str
    message: str


@dataclass(frozen=True)
class Site:
    """One place an id is declared: a `data-surface` in the code, or an entry in the map."""

    id: str
    path: str
    line: int
    rootless: bool = False  # a map entry whose `root` is `none: <reason>`


# ----------------------------------------------------------------- config

def _str_list(table: dict[str, object], name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if name not in table:
        return default
    value = table[name]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"[ux_surface_ratchet] {name} must be a list of non-empty strings")
    return tuple(v.replace("\\", "/") for v in value)


def _str(table: dict[str, object], name: str, default: str) -> str:
    value = table.get(name, default)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"[ux_surface_ratchet] {name} must be a non-empty string")
    return value.replace("\\", "/")


def parse_config(toml_text: str) -> Config | None:
    """`[ux_surface_ratchet]` -> `Config`, or None when the stage is not enabled.

    An absent table, or `enabled = false`, is the one correct skip: the adopter
    declared it. Anything malformed raises `ConfigError` -- a typo'd key or rule
    must not quietly run with less than the adopter asked for.
    """
    table = tomllib.loads(toml_text).get("ux_surface_ratchet")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError("[ux_surface_ratchet] must be a table")
    unknown = sorted(set(table) - _CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"[ux_surface_ratchet]: unknown key(s) {', '.join(unknown)}")
    enabled = table.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("[ux_surface_ratchet] enabled must be true or false")
    if not enabled:
        return None
    rules = _str_list(table, "rules", RULES)
    bad = sorted(set(rules) - set(RULES))
    if not rules or bad:
        raise ConfigError(f"[ux_surface_ratchet] rules must name some of: {', '.join(RULES)}"
                          + (f" (unknown: {', '.join(bad)})" if bad else ""))
    paths = _str_list(table, "paths", DEFAULT_PATHS)
    if not paths:
        raise ConfigError("[ux_surface_ratchet] paths must name at least one glob")
    source, page = _str(table, "map", DEFAULT_MAP).strip("/"), _str(table, "page", DEFAULT_PAGE).strip("/")
    if source.lower().endswith(".md"):
        raise ConfigError(f"[ux_surface_ratchet] map = {source!r}: the map is YAML now; move its entries into "
                          f"{DEFAULT_MAP} (docs/ux-surface-map.md, \"Moving a Markdown map\") and drop `map`")
    if source == page:
        raise ConfigError("[ux_surface_ratchet] map and page must be different files")
    return Config(_baseline.configured(_str(table, "baseline", DEFAULT_BASELINE), DEFAULT_BASELINE), tuple(r for r in RULES if r in rules),
                  source, paths, _str_list(table, "exclude", ()), page)


def vendored_dests(toml_text: str) -> tuple[str, ...]:
    table = tomllib.loads(toml_text).get("vendored")
    found = []
    for entry in (table.values() if isinstance(table, dict) else ()):
        value = entry.get("dest") if isinstance(entry, dict) else None
        if isinstance(value, str) and value.strip("/"):
            found.append(value.replace("\\", "/").strip("/"))
    return tuple(sorted(found))


def load_config(root: Path) -> tuple[Config | None, tuple[str, ...]]:
    """(config or None when not enabled, `[vendored]` dests)."""
    path = root / ".fleet.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return parse_config(text), vendored_dests(text)
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
        elif glob[i] in "*?":
            out.append("[^/]*" if glob[i] == "*" else "[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def matches_any(text: str, globs: tuple[str, ...]) -> bool:
    return any(_glob_regex(g).match(text) for g in globs)


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _is_source(path: str) -> bool:
    """An HTML or JS file outside the test folders."""
    return (path.lower().endswith(HTML_SUFFIXES + JS_SUFFIXES)
            and not any(p in ("tests", "test") for p in PurePosixPath(path).parts[:-1]))


def in_scope(path: str, cfg: Config, vendored: tuple[str, ...]) -> bool:
    """Is `path` an HTML or JS file this stage holds to its rules? A vendored copy's surfaces are stamped by its callers."""
    if not _is_source(path):
        return False
    if any(_under(path, d) for d in vendored) or not matches_any(path, cfg.paths):
        return False
    return not matches_any(path, BUILTIN_EXCLUDES + cfg.exclude)


def in_vendored_scope(path: str, vendored: tuple[str, ...]) -> bool:
    """Is `path` a vendored HTML or JS file, read only for the surface ids it stamps itself?"""
    return (_is_source(path) and any(_under(path, d) for d in vendored)
            and not matches_any(path, BUILTIN_EXCLUDES))


# ------------------------------------------------------------- code reader

@dataclass
class _Found:
    """One file's raw findings: (line, end line, rule, message) hits and the ids it declares."""

    hits: list[tuple[int, int, str, str]] = field(default_factory=list)
    sites: list[tuple[str, int, int]] = field(default_factory=list)  # (id, line, end line)

    def hit(self, line: int, rule: str, message: str, end: int | None = None) -> None:
        self.hits.append((line, end if end is not None else line, rule, message))

    def declare(self, value: str, line: int, what: str, end: int | None = None) -> None:
        if is_surface_id(value):
            self.sites.append((value, line, end if end is not None else line))
        else:
            self.hit(line, "invalid-surface-id", f"{what} {value!r} is not a constant surface id", end)


def is_surface_id(value: str) -> bool:
    return SURFACE_ID.match(value) is not None


def is_surface_root(tag: str, attrs: dict[str, str]) -> bool:
    return tag == "dialog" or "popover" in attrs or attrs.get("role", "").strip().lower() in SURFACE_ROLES


class _Markup(HTMLParser):
    """`data-surface` ids and unmarked surface roots in one HTML document or fragment."""

    def __init__(self, found: _Found, line_offset: int, path: str | None) -> None:
        super().__init__(convert_charrefs=True)
        self.found, self.line_base, self.path = found, line_offset, path
        self.script: list[str] | None = None  # chunks of an open inline JS script (documents only)
        self.script_line = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, {k: v or "" for k, v in attrs})

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, {k: v or "" for k, v in attrs})

    def _start(self, tag: str, attrs: dict[str, str]) -> None:
        line = self.getpos()[0] + self.line_base
        end = line + (self.get_starttag_text() or "").count("\n")
        if ATTR in attrs:
            self.found.declare(attrs[ATTR], line, f"<{tag} {ATTR}>", end)
        elif is_surface_root(tag, attrs):
            self.found.hit(line, "unmarked-surface", f"<{tag}> is a surface root with no {ATTR}", end)
        if tag == "script" and self.path is not None and "src" not in attrs \
                and attrs.get("type", "").strip().lower() in ("", "module", "text/javascript", "application/javascript"):
            self.script, self.script_line = [], end

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.script is not None and self.path is not None:
            _scan_js("".join(self.script), self.path, self.script_line, self.found)
            self.script = None

    def handle_data(self, data: str) -> None:
        if self.script is not None:
            self.script.append(data)


def _scan_markup(text: str, found: _Found, line_offset: int, path: str | None) -> None:
    parser = _Markup(found, line_offset, path)
    parser.feed(text)
    parser.close()


class _Js:
    """The JS forms over one token list: markup in strings, `dataset.surface`, `setAttribute`, `createElement`."""

    def __init__(self, toks: list[Any], closes: dict[int, int], found: _Found) -> None:  # `scan.Tok`, loaded by path
        self.toks, self.closes, self.found = toks, closes, found
        self.consts = self._module_consts()

    def _at(self, i: int, kind: str, text: str | None = None) -> bool:
        if not 0 <= i < len(self.toks):
            return False
        return self.toks[i].kind == kind and (text is None or self.toks[i].text == text)

    def _literal(self, i: int) -> str | None:
        """The string value at `i` when it stands alone as the expression: a literal, or a module constant."""
        if not 0 <= i < len(self.toks):
            return None
        tok = self.toks[i]
        nxt = self.toks[i + 1] if i + 1 < len(self.toks) else None
        if nxt is not None and not (nxt.kind == "punct" and nxt.text in ";,)}]") and nxt.line == tok.line:
            return None  # `'a' + x`, `x.y`, a call: computed
        if tok.kind == "str" or (tok.kind == "tpl" and not tok.holes):
            return str(tok.text)
        if tok.kind == "name":
            return self.consts.get(tok.text)
        return None

    def _module_consts(self) -> dict[str, str]:
        """`const NAME = '<id>'` at module level."""
        out = {}
        for i, tok in enumerate(self.toks):
            if (tok.kind == "name" and tok.text == "const" and tok.block == -1 and self._at(i + 1, "name")
                    and self._at(i + 2, "punct", "=") and i + 3 < len(self.toks)):
                value = self.toks[i + 3]
                after = self.toks[i + 4] if i + 4 < len(self.toks) else None
                closed = after is None or after.line > value.line or (after.kind == "punct" and after.text in ";,")
                if closed and (value.kind == "str" or (value.kind == "tpl" and not value.holes)):
                    out[self.toks[i + 1].text] = value.text
        return out

    def _value(self, at: int, value_at: int, what: str) -> None:
        value = self._literal(value_at)
        line = self.toks[at].line
        if value is None:
            self.found.hit(line, "invalid-surface-id", f"{what} is not a constant surface id")
        else:
            self.found.declare(value, line, what)

    def scan(self) -> None:
        for i, tok in enumerate(self.toks):
            if tok.kind in ("str", "tpl") and "<" in tok.text and re.search(r"<[A-Za-z]", tok.text):
                _scan_markup(tok.text, self.found, tok.line - 1, None)
            elif tok.kind == "name" and (self._at(i - 1, "punct", ".") or self._at(i - 1, "op", "?.")):
                self._member(i, tok.text)

    def _member(self, i: int, name: str) -> None:
        if name == "dataset" and self._at(i + 1, "punct", ".") and self._at(i + 2, "name", "surface") \
                and self._at(i + 3, "punct", "="):
            self._value(i, i + 4, "`dataset.surface =` value")
        elif name == "setAttribute" and self._at(i + 1, "punct", "(") and self._at(i + 2, "str", ATTR) \
                and self._at(i + 3, "punct", ","):
            self._value(i, i + 4, f"`setAttribute('{ATTR}', ...)` value")
        elif name == "createElement" and self._at(i + 1, "punct", "(") and self._at(i + 2, "str") \
                and self._at(i + 3, "punct", ")"):
            self._created(i, self.toks[i + 2].text.lower())

    def _bound_name(self, dot: int) -> str | None:
        """`NAME = document.createElement(...)`: NAME, from the `.` before `createElement`."""
        start = dot - 1
        while self._at(start - 1, "punct", ".") and self._at(start - 2, "name"):
            start -= 2
        if self._at(start - 1, "punct", "=") and self._at(start - 2, "name"):
            return str(self.toks[start - 2].text)
        return None

    def _created(self, i: int, tag: str) -> None:
        name = self._bound_name(i - 1)
        attrs = self._attrs_set(name, i) if name else {}
        if ATTR not in attrs and is_surface_root(tag, attrs):
            self.found.hit(self.toks[i].line, "unmarked-surface",
                           f"createElement('{tag}') is a surface root and gets no {ATTR} in its block")

    def _attrs_set(self, name: str, start: int) -> dict[str, str]:
        """Attributes the code sets on `name` after `start`, in the same block (nested blocks included)."""
        block = self.toks[start].block
        end = self.closes.get(block, len(self.toks)) if block >= 0 else len(self.toks)
        attrs: dict[str, str] = {}
        for j in range(start + 1, end):
            if not (self._at(j, "name", name) and self._at(j + 1, "punct", ".") and self._at(j + 2, "name")):
                continue
            member = self.toks[j + 2].text
            if member == "dataset" and self._at(j + 4, "name", "surface") and self._at(j + 5, "punct", "="):
                attrs[ATTR] = ""
            elif member == "setAttribute" and self._at(j + 3, "punct", "(") and self._at(j + 4, "str"):
                attrs[self.toks[j + 4].text.lower()] = self._literal(j + 6) or ""
            elif member in ("role", "popover") and self._at(j + 3, "punct", "="):
                attrs[member] = self._literal(j + 4) or ""
        return attrs


def _scan_js(text: str, path: str, line: int, found: _Found) -> None:
    toks, closes = scan.tokenize_js(text, path, line)
    _Js(toks, closes, found).scan()


def scan_code(text: str, path: str, rules: tuple[str, ...]) -> tuple[list[Hit], list[Site]]:
    """(hits of the per-site code rules, not exempted; the ids the file declares) for one HTML or JS file.

    Raises `NotConfirmed` when a JS source (a file, or an inline script) does not tokenize.
    """
    found = _Found()
    if path.lower().endswith(JS_SUFFIXES):
        _scan_js(text, path, 1, found)
    else:
        _scan_markup(text, found, 0, path)
    lines = text.splitlines()
    hits = [Hit(path, line, rule, message) for line, end, rule, message in found.hits
            if rule in rules and not scan.is_exempt(rule, lines, line, end)]
    sites = [Site(value, path, line) for value, line, _ in found.sites]
    return sorted(hits, key=lambda h: (h.line, h.rule)), sites


# -------------------------------------------------------------- map reader

def read_map(smap_obj: Any, path: str) -> tuple[list[Site], list[Hit]]:
    """(the map's entries, as sites; its `invalid-surface-id` and `dangling-link` hits) from a loaded map."""
    sites = [Site(s.id, path, s.line, s.rootless) for s in smap_obj.surfaces]
    known = {s.id for s in smap_obj.surfaces}
    hits = [Hit(path, s.line, "invalid-surface-id", f"map entry id {s.id!r} is not a valid surface id")
            for s in smap_obj.surfaces if not is_surface_id(s.id)]
    hits += [Hit(path, link.line, "dangling-link", f"`{s.id}` links to `{link.to}`, which no entry lists")
             for s in smap_obj.surfaces for link in s.links if link.to not in known]
    return sites, hits


def generated_hit(smap_obj: Any, cfg: Config, page_text: str | None) -> Hit | None:
    """The `stale-generated` hit when the committed page is missing or not what the map generates."""
    if page_text is None:
        return Hit(cfg.page, 1, "stale-generated",
                   f"the page is missing -- run `python scripts/ux_surface_map.py` to generate it from {cfg.map}")
    line = smap.first_difference(smap.render(smap_obj, cfg.map), page_text)
    if not line:
        return None
    return Hit(cfg.page, line, "stale-generated", f"the page is not what {cfg.map} generates from here on -- "
               "edit the map, never the page, and run `python scripts/ux_surface_map.py`")


# ----------------------------------------------------------------- matching

def cross_check(code: list[Site], entries: list[Site], rules: tuple[str, ...],
                vendored: tuple[Site, ...] = ()) -> list[Hit]:
    """The rules that compare the code's ids with the map's: unmapped, stale, duplicate.

    `vendored` are the ids a vendored component stamps itself. They keep a map
    entry from being stale; they are not held to the other rules, since the
    adopting app cannot edit a vendored copy to fix or except a hit there.
    """
    hits: list[Hit] = []
    mapped = {s.id for s in entries}
    declared = {s.id for s in code} | {s.id for s in vendored}
    if "unmapped-surface" in rules:
        hits += [Hit(s.path, s.line, "unmapped-surface", f"`{s.id}` has no entry in the surface map")
                 for s in code if s.id not in mapped]
    if "stale-entry" in rules:
        hits += [Hit(s.path, s.line, "stale-entry",
                     f"`{s.id}` is in the map but no element carries {ATTR}=\"{s.id}\" "
                     "(a surface with no element of its own sets `root:` to `none: <reason>`, nested under it)")
                 for s in entries if s.id not in declared and not s.rootless]
    if "duplicate-surface" in rules:
        for sites, where in ((code, "declared on"), (entries, "listed by")):
            count = Counter(s.id for s in sites)
            hits += [Hit(s.path, s.line, "duplicate-surface", f"`{s.id}` is {where} {count[s.id]} sites")
                     for s in sites if count[s.id] > 1]
    return hits


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


def scan_tree(root: Path, cfg: Config, vendored: tuple[str, ...], map_text: str) -> dict[str, list[Hit]]:
    """Every rule's hits across the in-scope HTML/JS files, the map and its page, exempt markers applied, by file.

    Raises `smap.MapError` when the map is outside the YAML subset or the schema.
    """
    hits: list[Hit] = []
    code: list[Site] = []
    from_vendored: list[Site] = []
    for path in tracked_files(root):
        if in_scope(path, cfg, vendored):
            text = _read_text(root, path)
            if text is not None:
                file_hits, sites = scan_code(text, path, cfg.rules)
                hits += file_hits
                code += sites
        elif in_vendored_scope(path, vendored):
            text = _read_text(root, path)
            if text is not None:
                from_vendored += scan_code(text, path, ())[1]  # ids only: no rule applies to a vendored copy
    loaded = smap.load(map_text, cfg.map)
    entries, map_hits = read_map(loaded, cfg.map)
    stale_page = generated_hit(loaded, cfg, _read_text(root, cfg.page))
    map_lines = map_text.splitlines()
    texts: dict[str, list[str]] = {}
    for h in [*map_hits, *cross_check(code, entries, cfg.rules, tuple(from_vendored))]:
        if h.rule not in cfg.rules:
            continue
        if h.path == cfg.map:
            exempt = smap.is_exempt(h.rule, map_lines, h.line)
        else:
            lines = texts.setdefault(h.path, (_read_text(root, h.path) or "").splitlines())
            exempt = scan.is_exempt(h.rule, lines, h.line, h.line)
        if not exempt:
            hits.append(h)
    if stale_page is not None and "stale-generated" in cfg.rules:
        hits.append(stale_page)
    found: dict[str, list[Hit]] = defaultdict(list)
    for h in sorted(hits, key=lambda h: (h.path, h.line, h.rule)):
        found[h.path].append(h)
    return dict(found)


# ---------------------------------------------------------------- baseline

Counts = dict[str, Counter[str]]  # path -> Counter(rule)


def current_counts(found: dict[str, list[Hit]]) -> Counts:
    return {path: Counter(h.rule for h in hits) for path, hits in found.items()}


def read_baseline(path: Path) -> Counts:
    """Baseline file -> ``{path: Counter(rule)}``. Raises `ConfigError` on anything off."""
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
                or not all(r in RULES and isinstance(n, int) and not isinstance(n, bool) and n > 0
                           for r, n in counts.items())):
            raise ConfigError(f"baseline {path}: entry {item!r} needs `counts` of rules to positive integers")
        out[item["key"]] = Counter(counts)
    return out


def render_baseline(counts: Counts) -> str:
    """Sorted, one file per line, no line numbers, so a baseline diff reviews cleanly."""
    lines = []
    for key in sorted(counts):
        record = {"key": key, "counts": {r: counts[key][r] for r in RULES if counts[key][r]}}
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
    print(f"ux surface ratchet: {_total(added)} hit(s) beyond the baseline, in {len(added)} file(s):")
    for path, extra in added.items():
        for rule in RULES:
            if not extra[rule]:
                continue
            had = baseline.get(path, Counter())[rule]
            print(f"  {path}: [{rule}] {had + extra[rule]} hit(s), baseline {had}:")
            for h in found[path]:
                if h.rule == rule:
                    print(f"    {path}:{h.line}: [{rule}] {h.message}")


def _check(cfg: Config, found: dict[str, list[Hit]], current: Counts, baseline: Counts) -> int:
    added, gone = diff(current, baseline)
    if added:
        _report_added(added, found, baseline)
        print(f"  Give each surface root a constant {ATTR} id and one entry in {cfg.map}, then regenerate "
              f"{cfg.page} (docs/ux-surface-map.md); remove an entry with its surface. A hit that must stay carries "
              "`<rule>-exempt: <reason>`; or (reviewed) raise the baseline with `--update-baseline --allow-additions`.")
        return EXIT_RISE
    print(f"ux surface ratchet: PASS ({_total(current)} baselined hit(s) in {len(current)} file(s), none new; "
          f"rules: {', '.join(cfg.rules)})")
    if gone:
        print(f"  {gone} baselined hit(s) are gone -- run "
              "`python scripts/ux_surface_ratchet.py --update-baseline` to lower it")
    return EXIT_PASS


def _load_baseline(baseline_path: Path, cfg: Config, init: bool) -> Counts | int:
    """The committed baseline (empty for `--init`), or the exit code that refuses the run."""
    if init:
        if baseline_path.exists():
            print(f"ux surface ratchet: {cfg.baseline} already exists; --init never overwrites it")
            return EXIT_CONFIG
        return {}
    if not baseline_path.is_file():
        print(f"ux surface ratchet: FAIL: baseline {cfg.baseline} is missing -- "
              "run `python scripts/ux_surface_ratchet.py --init`")
        return EXIT_RISE
    try:
        return read_baseline(baseline_path)
    except ConfigError as exc:
        print(f"ux surface ratchet: BASELINE ERROR: {exc}")
        return EXIT_CONFIG


def _write(path: Path, counts: Counts) -> None:
    # runtime-write-exempt: the repo's own baseline file, named by the config, never a code folder
    path.write_text(render_baseline(counts), encoding="utf-8", newline="\n")


def run(root: Path, *, init: bool, update: bool, allow_additions: bool) -> int:
    try:
        cfg, vendored = load_config(root)
    except ConfigError as exc:
        print(f"ux surface ratchet: CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    if cfg is None:
        if init or update:
            print("ux surface ratchet: not enabled -- set [ux_surface_ratchet] enabled = true in .fleet.toml first")
            return EXIT_CONFIG
        print("ux surface ratchet: not enabled (no [ux_surface_ratchet] enabled = true in .fleet.toml)")
        return EXIT_PASS
    baseline_path = _baseline.resolve(root, cfg.baseline, DEFAULT_BASELINE, "ux surface ratchet")
    if baseline_path is None:
        return EXIT_CONFIG
    baseline = _load_baseline(baseline_path, cfg, init)
    if isinstance(baseline, int):
        return baseline
    try:
        map_text = _read_text(root, cfg.map)
        if map_text is None:
            print(f"ux surface ratchet: FAIL: the surface map {cfg.map} is missing -- copy "
                  "docs/templates/ux-surfaces.yaml from project-scaffolding and fill it (docs/ux-surface-map.md)")
            return EXIT_RISE
        found = scan_tree(root, cfg, vendored, map_text)
    except smap.MapError as exc:
        print(f"ux surface ratchet: FAIL: the surface map is invalid: {exc}")
        print("  (fix the map: docs/ux-surface-map.md lists the schema, scripts/ux_surface_map.py the YAML subset)")
        return EXIT_RISE
    except NotConfirmed as exc:
        print(f"ux surface ratchet: NOT CONFIRMED: {exc}")
        print("  (the tree could not be read through, so this is neither a pass nor a clean tree)")
        return EXIT_NOT_CONFIRMED
    current = current_counts(found)

    if init:
        _write(baseline_path, current)
        print(f"ux surface ratchet: wrote {cfg.baseline} with {_total(current)} hit(s) in {len(current)} file(s)")
        return EXIT_PASS
    if not update:
        return _check(cfg, found, current, baseline)
    added, gone = diff(current, baseline)
    if added and not allow_additions:
        _report_added(added, found, baseline)
        print("ux surface ratchet: nothing written -- fix the code or the map, or raise the baseline "
              "with --allow-additions")
        return EXIT_RISE
    _write(baseline_path, current)
    print(f"ux surface ratchet: {cfg.baseline} now has {_total(current)} hit(s) in {len(current)} file(s) "
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
    # Reports contain non-ASCII (e.g. "—"); a cp1252 console would otherwise crash after the verdict.
    for _stream in (sys.stdout, sys.stderr):
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # real console streams are TextIOWrapper
    sys.exit(main())
