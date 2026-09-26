"""Diff-proportionate e2e routing for the pre-ship gate (project-scaffolding#180).

`scripts/verify-before-ship.ps1` used to run the whole `tests/e2e` suite
unconditionally, regardless of what the diff touched. Proven first in
`app-launcher` (issue #568, PR #574): map the changed-file set of the current
branch to a coverage tier so the gate's browser phase runs a slice
proportionate to the diff -- never weaker for real UI/behaviour changes, and
fail-safe to the full suite whenever the diff is ambiguous, mixed, or touches
anything not confidently narrow.

Unlike app-launcher's classifier, the path -> tier rules here are NOT
hardcoded in this module -- every consumer's layout differs. They are
declared per-project in that repo's own `.fleet.toml` under an `[e2e]` table
(see the `[e2e]` block in this repo's own `.fleet.toml` for a worked example,
and `docs/e2e-routing.md` for the schema reference). This module only
supplies the mechanism: load the declared rules, classify the diff against
them in order, first match wins.

Tiers:

  * ``skip``    every changed path matched a declared "none" rule (no
                browser impact) -> no browser suite runs.
  * ``static``  the worst-matching path is a declared "static" rule (e.g. an
                image asset or an inert vendored HTML fragment) -> a narrow
                smoke target, declared as ``static_pytest_target``.
  * ``full``    any path matched a declared "full" rule, OR any path matched
                *no* rule at all, OR the project has no `[e2e]` table
                declared, OR the diff is empty -- runs
                ``full_pytest_target`` (the whole e2e suite by default). This
                is the fail-safe default: uncertainty always escalates to
                full coverage, never narrows it.
  * ``surface`` a narrowing of ``full`` (project-scaffolding#258): the diff
                would route ``full``, but every ``full`` path belongs to ONE
                declared ``[[e2e.surface]]`` -> only that surface's
                ``pytest_targets`` run (plus ``static_pytest_target`` when a
                static path rides along, which must sit in the same
                surface). Any unclassified path, a ``full``/``static``
                path outside every surface, a path in two surfaces, a diff
                spanning two surfaces, or an unusable surface declaration
                keeps the whole-suite ``full``. Without ``[[e2e.surface]]``
                routing is exactly the three tiers above.

Two narrowings feed ``surface`` (project-scaffolding#289):

  * a declared **shared stylesheet** (``[e2e] shared_stylesheets``) is owned,
    for this diff only, by the one surface whose ``selectors`` prefixes claim
    every CSS rule the diff changes. A token or ``:root`` change, any at-rule
    (``@media``, ``@import``, ``@font-face``...), a nested rule, a selector no
    surface or two surfaces claim, a line the scanner can't attribute, or a
    sheet whose old/new text is unavailable keeps the whole suite;
  * an edited **e2e test module** (``test_*.py`` / ``*_test.py`` in the suite)
    that no surface owns and no other suite file imports runs only itself.
    Shared helpers (``conftest.py``, ``_*.py``) still run the whole suite.

CLI: prints ``E2E_*=`` key/value lines (parsed by the PowerShell gate) plus a
human summary on stderr. Run standalone to see how the current branch would
route:

    python scripts/classify_e2e.py            # classify the live diff
    python scripts/classify_e2e.py a.js b.svg  # classify an explicit file list
"""

from __future__ import annotations

import difflib
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FLEET_TOML = REPO_ROOT / ".fleet.toml"


class Category(IntEnum):
    """Per-path coverage requirement. Higher wins across the diff."""

    NONE = 0    # declared "none" rule -- no browser impact
    STATIC = 1  # declared "static" rule -- a narrow smoke target is enough
    FULL = 2    # declared "full" rule, or no rule matched at all


_TIER_NAMES = {"none": Category.NONE, "static": Category.STATIC, "full": Category.FULL}


@dataclass
class Rule:
    """One declared `[[e2e.rule]]` entry. First matching rule wins."""

    tier: Category
    prefix: str | None = None
    path: str | None = None
    extensions: tuple[str, ...] | None = None
    label: str = "rule"

    def matches(self, path: str, ext: str) -> bool:
        if self.path is not None:
            return path == self.path
        if self.prefix is not None and not path.startswith(self.prefix):
            return False
        if self.extensions is not None and ext not in self.extensions:
            return False
        # A rule needs at least one real matcher -- a bare {tier=...} row
        # with no prefix/path/extensions would match everything by accident.
        return self.prefix is not None or self.extensions is not None


@dataclass
class Surface:
    """One declared `[[e2e.surface]]`: the paths one slice of the suite covers."""

    name: str
    pytest_targets: tuple[str, ...]
    prefixes: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    # CSS selector prefixes this surface owns in a shared stylesheet (#289).
    selectors: tuple[str, ...] = ()

    def matches(self, path: str) -> bool:
        return path in self.paths or any(path.startswith(p) for p in self.prefixes)

    def owns_selector(self, selector: str) -> bool:
        return any(selector.startswith(p) for p in self.selectors)


@dataclass
class E2EConfig:
    rules: list[Rule]
    static_pytest_target: str = "tests/e2e"
    static_browsers: tuple[str, ...] = ("chromium",)
    full_pytest_target: str = "tests/e2e"
    # "declared" = usable [e2e] table found; anything else is a fail-safe
    # reason string surfaced in the routing output.
    source: str = "missing"
    surfaces: list[Surface] = field(default_factory=list)
    # Why declared surfaces were disabled ("" when none declared or all usable).
    surfaces_note: str = ""
    # Stylesheets shared across surfaces, narrowed by the rules a diff changes (#289).
    shared_stylesheets: tuple[str, ...] = ()
    # Where the suite lives on disk; a self-only e2e module must exist there (#289).
    repo_root: Path = REPO_ROOT


def _str_list(raw: object) -> tuple[str, ...] | None:
    """A list of non-empty, whitespace-free strings, or None for anything else."""
    if not isinstance(raw, list):
        return None
    if not all(isinstance(x, str) and x and not any(c.isspace() for c in x) for x in raw):
        return None
    return tuple(raw)


_SURFACE_NAME = re.compile(r"[A-Za-z0-9_.-]+")
# The files that define routing itself; a diff touching either never narrows.
_ROUTING_SOURCES = frozenset({".fleet.toml", "scripts/classify_e2e.py"})


def _target_problem(target: str, repo_root: Path, suite_dir: str) -> str | None:
    """Why *target* can't be a surface target, or None if it can.

    A target must be a relative path inside the e2e suite that exists on disk:
    an absolute path, a `..` hop, or `tests` (swapping the browser suite for
    the unit suite) would each run something other than a slice of the suite.
    """
    if Path(target).is_absolute() or ".." in Path(target).parts:
        return "is not a relative path inside the suite"
    suite_root = (repo_root / suite_dir).resolve()
    resolved = (repo_root / target).resolve()
    if resolved != suite_root and not resolved.is_relative_to(suite_root):
        return f"is outside {suite_dir}"
    if not resolved.exists():
        return "does not exist"
    return None


def load_surfaces(
    raw: object, repo_root: Path, suite_dir: str = "tests/e2e"
) -> tuple[list[Surface], str]:
    """Parse `[[e2e.surface]]` into `(surfaces, note)`.

    All-or-nothing: one malformed entry, a duplicate name, or an unusable
    target disables every surface (the note says why), so a stale or
    half-edited map can only ever widen routing back to the whole suite.
    Names are restricted to `[A-Za-z0-9_.-]` because they are echoed into the
    `E2E_*` lines the gate parses -- a newline in one could forge a later
    `E2E_TIER=` line. Prefixes must end in `/`, so `app/board` can never claim
    `app/boardroom/`.
    """
    if raw is None:
        return [], ""
    if not isinstance(raw, list) or not raw:
        return [], "[[e2e.surface]] is not a non-empty list -- surfaces disabled"
    surfaces: list[Surface] = []
    for i, entry in enumerate(raw, start=1):
        bad = f"[[e2e.surface]] entry {i} is malformed -- surfaces disabled"
        if not isinstance(entry, dict):
            return [], bad
        name = entry.get("name")
        targets = _str_list(entry.get("pytest_targets"))
        prefixes = _str_list(entry.get("prefixes", []))
        paths = _str_list(entry.get("paths", []))
        selectors = _str_list(entry.get("selectors", []))
        if (not isinstance(name, str) or not _SURFACE_NAME.fullmatch(name) or not targets
                or prefixes is None or paths is None or not (prefixes or paths)
                or not all(p.endswith("/") for p in prefixes)
                or selectors is None or any(sel.startswith(":root") for sel in selectors)):
            return [], bad
        if any(s.name == name for s in surfaces):
            return [], f"[[e2e.surface]] name {name!r} declared twice -- surfaces disabled"
        for target in targets:
            problem = _target_problem(target, repo_root, suite_dir)
            if problem:
                return [], f"surface {name!r} target {target} {problem} -- surfaces disabled"
        surfaces.append(Surface(name=name, pytest_targets=targets, prefixes=prefixes,
                                paths=paths, selectors=selectors))
    return surfaces, ""


def load_config(fleet_toml: Path = FLEET_TOML) -> E2EConfig:
    """Read the `[e2e]` table from *fleet_toml*.

    Missing file, unparsable TOML, absent `[e2e]` table, or an `[e2e]` table
    with zero usable rules all return a config with ``source != "declared"``
    -- `classify()` then fail-safes every path to FULL rather than guessing.
    """
    if not fleet_toml.is_file():
        return E2EConfig(rules=[], source="missing")
    try:
        data = tomllib.loads(fleet_toml.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return E2EConfig(rules=[], source="invalid")

    e2e = data.get("e2e")
    if not isinstance(e2e, dict):
        return E2EConfig(rules=[], source="missing")

    rules: list[Rule] = []
    for raw in e2e.get("rule", []):
        if not isinstance(raw, dict) or "tier" not in raw:
            continue
        tier = _TIER_NAMES.get(str(raw["tier"]).lower())
        if tier is None:
            continue
        exts = raw.get("extensions")
        rules.append(
            Rule(
                tier=tier,
                prefix=raw.get("prefix"),
                path=raw.get("path"),
                extensions=tuple(str(e).lower() for e in exts) if exts else None,
                label=str(raw.get("label") or raw.get("prefix") or raw.get("path") or "rule"),
            )
        )

    if not rules:
        return E2EConfig(rules=[], source="empty")

    full_pytest_target = str(e2e.get("full_pytest_target", "tests/e2e"))
    surfaces, surfaces_note = load_surfaces(
        e2e.get("surface"), fleet_toml.parent, full_pytest_target
    )
    # Malformed -> no shared stylesheet, so a sheet edit keeps the whole suite.
    sheets = _str_list(e2e.get("shared_stylesheets", [])) or ()
    return E2EConfig(
        rules=rules,
        static_pytest_target=str(e2e.get("static_pytest_target", "tests/e2e")),
        static_browsers=tuple(e2e.get("static_browsers", ["chromium"])),
        full_pytest_target=full_pytest_target,
        source="declared",
        surfaces=surfaces,
        surfaces_note=surfaces_note,
        shared_stylesheets=sheets,
        repo_root=fleet_toml.parent,
    )


def _classify_one(path: str, rules: list[Rule]) -> tuple[Category, str]:
    """Map one repo-relative (posix) path to its coverage category + label.

    Rules are tried in declaration order, first match wins. A path matching
    no rule at all is the fail-safe: treated as FULL, labelled "unclassified".
    """
    name = path.rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    for rule in rules:
        if rule.matches(path, ext):
            return rule.tier, rule.label
    return Category.FULL, "unclassified"


# ------------------------------------------------------ shared stylesheets (#289)
# A changed line is attributed to the CSS rule whose selector or declarations
# it holds. Anything that can reach past one rule's selectors -- a token
# (`--x:`), `:root`, an at-rule and everything inside one, a nested rule, text
# outside every rule -- is an "unsafe" attribution and keeps the whole suite.

_UNSAFE = "unsafe"


def _split_selectors(prelude: str) -> list[str]:
    """A selector list split on its top-level commas, whitespace-normalised."""
    parts, depth, cur = [], 0, ""
    for ch in prelude:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return [" ".join(p.split()) for p in parts if p.strip()]


def css_line_attributions(text: str) -> dict[int, set[str]] | None:
    """`{line: {selector | "unsafe"}}` for every line holding CSS, or None.

    None means the scanner could not follow the sheet (an unclosed block,
    comment or string, or a stray `}`); the caller treats that as unsafe.
    Lines holding only whitespace or comments are absent: they can't render.
    """
    out: dict[int, set[str]] = {}
    stack: list[set[str]] = []      # per open block: what its lines attribute to
    buf, buf_lines = "", set()
    line, i, n = 1, 0, len(text)

    def mark(lines: set[int], attrs: set[str]) -> None:
        for ln in lines:
            out.setdefault(ln, set()).update(attrs)

    while i < n:
        ch = text[i]
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                return None
            line += text.count("\n", i, end)
            i = end + 2
            continue
        if ch in "\"'":
            end = i + 1
            while end < n and text[end] != ch and text[end] != "\n":
                end += 2 if text[end] == "\\" else 1
            if end >= n or text[end] != ch:
                return None
            buf += text[i:end + 1]
            buf_lines.add(line)
            i = end + 1
            continue
        if ch == "\n":
            line += 1
            buf += ch
        elif ch == "{":
            prelude = buf.strip()
            if stack or prelude.startswith("@") or not prelude:
                attrs = {_UNSAFE}  # an at-rule, anything inside one, a nested rule
            else:
                sels = _split_selectors(prelude)
                attrs = {_UNSAFE} if any(s.startswith(":root") for s in sels) else set(sels)
            mark(buf_lines | {line}, attrs)
            stack.append(attrs)
            buf, buf_lines = "", set()
        elif ch in ";}":
            if buf.strip():
                attrs = stack[-1] if stack else {_UNSAFE}
                if buf.split(":", 1)[0].strip().startswith("--"):
                    attrs = {_UNSAFE}  # a custom property: a token other rules read
                mark(buf_lines | ({line} if ch == ";" else set()), attrs)
            buf, buf_lines = "", set()
            if ch == "}":
                if not stack:
                    return None
                mark({line}, stack.pop())
        else:
            buf += ch
            if not ch.isspace():
                buf_lines.add(line)
        i += 1
    if stack or buf.strip():
        return None
    return out


def changed_selectors(old: str | None, new: str | None) -> set[str] | None:
    """The selectors of every CSS rule changed between two texts of one sheet.

    None when the change can reach past those rules (see `_UNSAFE`), when
    either text is unavailable, or when the scanner can't follow one of them.
    """
    if old is None or new is None:
        return None
    old_attr, new_attr = css_line_attributions(old), css_line_attributions(new)
    if old_attr is None or new_attr is None:
        return None
    sels: set[str] = set()
    matcher = difflib.SequenceMatcher(None, old.splitlines(), new.splitlines(), autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            continue
        for ln in range(i1 + 1, i2 + 1):
            sels |= old_attr.get(ln, set())
        for ln in range(j1 + 1, j2 + 1):
            sels |= new_attr.get(ln, set())
    return None if _UNSAFE in sels else sels


def _sheet_owner(
    path: str, sheet_changes: dict[str, set[str] | None], surfaces: list[Surface],
) -> Surface | None:
    """The one surface owning every rule the diff changes in *path*, or None."""
    sels = sheet_changes.get(path)
    if not sels:
        return None  # unknown, unsafe, or nothing but comments changed
    owner: Surface | None = None
    for sel in sels:
        owners = [s for s in surfaces if s.owns_selector(sel)]
        if len(owners) != 1 or (owner is not None and owners[0] is not owner):
            return None
        owner = owners[0]
    return owner


def _self_only_module(path: str, config: E2EConfig) -> bool:
    """An e2e test module that only its own run can break (#289).

    `test_*.py` / `*_test.py` inside the suite, present on disk (a deleted
    module has nothing to run), and imported by no other suite file.
    """
    suite = config.full_pytest_target.rstrip("/") + "/"
    name = path.rsplit("/", 1)[-1]
    if not path.startswith(suite) or not name.endswith(".py"):
        return False
    if not (name.startswith("test_") or name.endswith("_test.py")):
        return False
    module = config.repo_root / path
    if not module.is_file():
        return False
    imports = re.compile(rf"^\s*(?:from|import)\s.*\b{re.escape(name[:-3])}\b", re.MULTILINE)
    for other in (config.repo_root / suite).rglob("*.py"):
        if other != module and imports.search(other.read_text(encoding="utf-8", errors="replace")):
            return False
    return True


@dataclass
class Routing:
    tier: str                       # "skip" | "static" | "full" | "surface"
    browsers: list[str]
    pytest_target: str              # "" when skip; space-separated when surface
    reasons: list[str] = field(default_factory=list)
    surface: str = ""               # the surface name when tier == "surface"


def _narrow_to_surface(
    classified: list[tuple[str, Category, str]], config: E2EConfig,
    sheet_changes: dict[str, set[str] | None],
) -> Routing | None:
    """The `surface` routing for a would-be-full diff, or None to keep whole `full`.

    Narrows only a confidently-classified single-surface diff; every `return
    None` below is a fail-safe back to the whole suite. Static paths must sit
    in the winning surface too: "inert" markup can still be the page another
    harness drives (this repo's component gallery), so a static path no
    surface owns keeps the whole suite rather than riding along on a smoke run.
    A shared stylesheet takes the one surface owning every rule it changes, and
    a self-only e2e module adds just itself (#289).
    """
    if any(label == "unclassified" for _, _, label in classified):
        return None
    # A diff that edits the surface map (or the mechanism reading it) must not
    # be judged by its own unreviewed map; a `..` hop defeats prefix matching.
    if any(path in _ROUTING_SOURCES or ".." in path.split("/") for path, _, _ in classified):
        return None
    hit: Surface | None = None
    example = ""
    modules: list[str] = []
    for path, cat, _label in classified:
        if cat == Category.NONE:
            continue
        if path in config.shared_stylesheets:
            owner = _sheet_owner(path, sheet_changes, config.surfaces)
            owners = [owner] if owner else []
            if not owners:
                return None
        else:
            owners = [s for s in config.surfaces if s.matches(path)]
        if not owners and cat == Category.FULL and _self_only_module(path, config):
            modules.append(path)
            continue
        if len(owners) != 1:
            return None  # outside every surface (incl. no surfaces declared), or inside two
        if hit is not None and owners[0].name != hit.name:
            return None  # the diff spans two surfaces
        if hit is None:
            hit, example = owners[0], path
    if hit is None and not modules:
        return None
    targets = list(hit.pytest_targets) if hit else []
    has_static = any(cat == Category.STATIC for _, cat, _ in classified)
    if has_static and config.static_pytest_target and config.static_pytest_target not in targets:
        targets.append(config.static_pytest_target)
    targets += [m for m in modules if m not in targets]
    reasons = [f"surface {hit.name}: {example}"] if hit else []
    reasons += [f"e2e module runs itself: {m}" for m in modules]
    return Routing("surface", [], " ".join(targets), reasons, hit.name if hit else "self")


def classify(
    paths: list[str], config: E2EConfig,
    sheet_changes: dict[str, set[str] | None] | None = None,
) -> Routing:
    """Route a set of changed paths to an e2e tier per *config*.

    Fail-safe to FULL whenever: the project has no usable `[e2e]`
    declaration, the diff is empty, or any changed path matches no declared
    rule. Uncertainty always escalates to full coverage, never narrows it.
    *sheet_changes* maps a changed shared stylesheet to `changed_selectors()`;
    a sheet missing from it keeps the whole suite.
    """
    if config.source != "declared":
        reason = {
            "missing": "no [e2e] table declared in .fleet.toml",
            "invalid": ".fleet.toml could not be parsed",
            "empty": "[e2e] table declared but has no usable rule entries",
        }.get(config.source, config.source)
        return Routing("full", [], config.full_pytest_target or "tests/e2e",
                        [f"{reason} -- fail-safe full suite"])

    examples: dict[str, str] = {}
    classified: list[tuple[str, Category, str]] = []
    top = Category.NONE
    for raw in paths:
        path = raw.strip().replace("\\", "/")
        if not path:
            continue
        cat, label = _classify_one(path, config.rules)
        top = max(top, cat)
        examples.setdefault(f"{cat.name}:{label}", path)
        classified.append((path, cat, label))

    def reasons_for(cat: Category) -> list[str]:
        return [
            f"{key.split(':', 1)[1]}: {ex}"
            for key, ex in sorted(examples.items())
            if key.startswith(f"{cat.name}:")
        ]

    if not examples:
        # Empty diff (e.g. run on a clean tree): can't prove narrow -> full.
        return Routing("full", [], config.full_pytest_target, ["empty-diff: no changed files"])

    if top == Category.FULL:
        narrowed = _narrow_to_surface(classified, config, sheet_changes or {})
        if narrowed is not None:
            return narrowed
        reasons = reasons_for(Category.FULL)
        if config.surfaces_note:
            reasons.append(config.surfaces_note)
        return Routing("full", [], config.full_pytest_target, reasons)
    if top == Category.STATIC:
        return Routing(
            "static", list(config.static_browsers), config.static_pytest_target,
            reasons_for(Category.STATIC),
        )
    return Routing("skip", [], "", reasons_for(Category.NONE))


# --------------------------------------------------------------------- git diff
# This module is copied byte-identical into adopter repos (fleet-config's
# `e2e_route.py bootstrap`, then hash-verified against this file), so it must
# stay self-contained -- it deliberately does NOT import this repo's shared
# `src/no_window.py`, which would not resolve in a consumer's tree. Deriving
# the flag locally here is the documented vendor-verbatim exception, not drift.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _run_git(args: list[str]) -> list[str]:
    # The gate runs this 4-5 times per invocation (symbolic-ref, rev-parse, two
    # diffs, ls-files). A console-less parent -- a tray, a scheduled task, an
    # agent shell -- would get a console window flashed for each one without
    # CREATE_NO_WINDOW; the closed stdin stops git ever waiting on a prompt.
    try:
        out = subprocess.run(
            ["git", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
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
        return head[0].replace("refs/remotes/", "", 1)
    if _run_git(["rev-parse", "--verify", "--quiet", "origin/main"]):
        return "origin/main"
    return "main"


def changed_files() -> list[str]:
    """Changed files on the current branch vs main, incl. the working tree.

    Union of: committed since the merge-base with main (``main...HEAD``),
    tracked working-tree edits (staged + unstaged, ``git diff HEAD``), and
    untracked new files. So a *pre-commit* gate run classifies correctly.
    """
    ref = _main_ref()
    files: set[str] = set()
    files.update(_run_git(["diff", "--name-only", f"{ref}...HEAD"]))
    files.update(_run_git(["diff", "--name-only", "HEAD"]))
    files.update(_run_git(["ls-files", "--others", "--exclude-standard"]))
    return sorted(files)


def _git_text(args: list[str]) -> str | None:
    """Stdout of a git command verbatim, or None when it fails."""
    try:
        out = subprocess.run(
            ["git", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
        )
    except OSError:
        return None
    return out.stdout if out.returncode == 0 else None


def sheet_changes_from_git(
    paths: list[str], config: E2EConfig,
) -> dict[str, set[str] | None]:
    """`changed_selectors()` per changed shared stylesheet: merge-base vs working tree.

    A sheet new since the merge-base diffs against the empty text; a deleted
    or unreadable one is None, which keeps the whole suite.
    """
    out: dict[str, set[str] | None] = {}
    wanted = [p for p in paths if p in config.shared_stylesheets]
    base = _run_git(["merge-base", _main_ref(), "HEAD"]) if wanted else []
    for path in wanted:
        old = None
        if base:
            old = _git_text(["show", f"{base[0]}:{path}"])
            if old is None and _git_text(["cat-file", "-e", f"{base[0]}:{path}"]) is None:
                old = ""  # absent at the merge-base: a new sheet
        try:
            new = (config.repo_root / path).read_text(encoding="utf-8")
        except OSError:
            new = None
        out[path] = changed_selectors(old, new)
    return out


def main(argv: list[str]) -> int:
    explicit = len(argv) > 1
    paths = [p.replace("\\", "/") for p in (argv[1:] if explicit else changed_files())]
    config = load_config()
    # An explicit file list carries no diff text, so its sheets keep the whole suite.
    routing = classify(paths, config, {} if explicit else sheet_changes_from_git(paths, config))

    # Machine-readable block the PowerShell gate parses (^E2E_ lines only).
    print(f"E2E_TIER={routing.tier}")
    print(f"E2E_BROWSERS={','.join(routing.browsers)}")
    print(f"E2E_PYTEST_TARGET={routing.pytest_target}")
    print(f"E2E_REASON={' | '.join(routing.reasons) if routing.reasons else '(none)'}")
    if routing.tier == "surface":
        print(f"E2E_SURFACE={routing.surface}")

    # Human summary (ignored by the gate parser).
    print("", file=sys.stderr)
    print(f"e2e routing: tier={routing.tier} "
          f"browsers={routing.browsers or 'suite-default'}", file=sys.stderr)
    for reason in routing.reasons:
        print(f"  - {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
