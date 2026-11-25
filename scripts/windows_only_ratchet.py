"""Ratchet on unmarked Windows-only tests: their count may only go down.

The Mac nightly's unit leg runs `pytest -m "not windows_only"`. A test that
needs Windows but carries no marker passes in the Windows gate where it was
written and fails on the Mac a night later. This stage reads the test modules,
finds tests that touch Windows-only APIs without the marker, counts them per
file against a committed baseline, and fails only when a count rises.

The rules (docs/windows-only-ratchet.md):

  * `win-module` -- an import of a Windows-only module (`winreg`, `msvcrt`,
    `winsound`, `pywintypes`, `pythoncom`, `wmi`, `comtypes`, any `win32*`);
  * `win-api` -- a Windows-only attribute: `ctypes.windll` / `WinDLL` /
    `oledll` / `OleDLL`, `os.startfile`, the Windows `subprocess` flags
    (`CREATE_NO_WINDOW`, `STARTUPINFO`, `DETACHED_PROCESS`, `CREATE_NEW_CONSOLE`,
    `CREATE_NEW_PROCESS_GROUP`) and `signal.CTRL_C_EVENT` / `CTRL_BREAK_EVENT`;
  * `win-env-read` -- `os.environ["<var>"]` for a Windows-only variable
    (`ProgramFiles`, `LOCALAPPDATA`, `APPDATA`, `USERPROFILE`, `SystemRoot`, ...)
    that nothing in the file sets, so it raises `KeyError` off Windows;
  * `win-exe-lookup` -- one test both points a Windows-only variable at a folder
    and builds an on-disk `.exe` path (`tmp_path / "chrome.exe"`): the code
    under test resolves a planted Windows install, which no other OS finds;
  * `bare-exempt` -- a `windows-only-exempt` marker with no reason.

Only test functions (`test*`, including methods of a class) and module-level
code are read; a hit is not counted when its test is covered. Covered means a
`@pytest.mark.windows_only` on the test or its class, the marker in the
module's `pytestmark`, a platform `skipif` (directly or through a module-level
alias), a platform-guarded `pytest.skip` at the top of the test or module, or
a module-level `pytest.importorskip` of a Windows-only module. Code under an
`if` or conditional expression on `sys.platform` / `os.name` /
`platform.system()` / `hasattr(...)`, or in a `try` that catches the
`ImportError` or `AttributeError`, is guarded and never a hit. A test that fakes
the platform (`monkeypatch.setattr(sys, "platform", ...)`, `mock.patch(
"sys.platform", ...)`) is clean of the two environment rules; a mocked
`win32gui` placed in `sys.modules` is not an import and is never a hit.

A single hit keeps its exception with `windows-only-exempt: <reason>` in a
comment on its line, in the comment lines right above it, or on (or right
above) its test's `def` or first decorator. Exemptions are printed every run.

Opt-in, per adopter, in its own `.fleet.toml` (adopt it alongside `[ci.nightly]`):

    [windows_only_ratchet]
    enabled    = true
    baseline   = ".ratchets/windows-only.json"  # default
    rules      = [...]                                 # default: all
    marker     = "windows_only"                        # default
    test_paths = ["tests/**"]                          # default
    exclude    = ["tests/fixtures/**"]                 # globs; never scanned

Exit codes: 0 pass or not enabled; 1 a count rose, or the baseline is missing /
a mode was refused; 2 bad configuration, baseline or arguments; 3 NOT CONFIRMED
(`git` unusable, a file that cannot be read, decoded or parsed) -- the gate
fails on it. Modes: `check`, `--init`, `--update-baseline` (shrink only),
`--update-baseline --allow-additions` (shows in review).

Vendor-verbatim (manifest key `windows_only_ratchet`), stdlib-only.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tokenize
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import ModuleType


def _load_sibling(name: str) -> ModuleType:
    """A sibling module, loaded by path so `python -P` (scripts/ off `sys.path`) finds it."""
    try:
        if __package__:  # imported as a package member (the tests)
            return importlib.import_module(f"{__package__}.{name}")
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
        assert spec is not None and spec.loader is not None
        module = sys.modules[spec.name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except (ImportError, OSError, SyntaxError):
        print(f"windows only ratchet: NOT CONFIRMED: {name}.py is missing beside this script -- vendor both "
              "(manifest keys windows_only_ratchet and ratchet_baseline)")
        sys.exit(3)


_baseline = _load_sibling("ratchet_baseline")  # where the baseline lives, and its move into .ratchets/

REPO_ROOT = Path(__file__).resolve().parent.parent

RULES: tuple[str, ...] = ("win-module", "win-api", "win-env-read", "win-exe-lookup", "bare-exempt")
BASELINE_VERSION = 1
DEFAULT_BASELINE = ".ratchets/windows-only.json"
DEFAULT_MARKER = "windows_only"
DEFAULT_TEST_PATHS = ("tests/**",)
BUILTIN_EXCLUDES = ("**/node_modules/**", "**/.venv/**", "**/venv/**")
GIT_TIMEOUT_S = 120
EXIT_PASS, EXIT_RISE, EXIT_CONFIG, EXIT_NOT_CONFIRMED = 0, 1, 2, 3

# Vendor-verbatim module: derives the flag locally (the shared `src/no_window.py`
# would not resolve in a consumer tree), the documented exception.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_CONFIG_KEYS = {"enabled", "baseline", "rules", "marker", "test_paths", "exclude"}
#: Modules that exist only on Windows; `win32` is a prefix (win32api, win32gui, win32con, win32com...).
WIN_MODULES = frozenset({"winreg", "_winreg", "msvcrt", "winsound", "pywintypes", "pythoncom", "wmi", "comtypes"})
WIN_MODULE_PREFIX = "win32"
#: (module, attribute) pairs that raise AttributeError off Windows.
WIN_ATTRS: dict[str, frozenset[str]] = {
    "ctypes": frozenset({"windll", "WinDLL", "oledll", "OleDLL"}),
    "os": frozenset({"startfile"}),
    "subprocess": frozenset({"CREATE_NO_WINDOW", "STARTUPINFO", "DETACHED_PROCESS", "CREATE_NEW_CONSOLE",
                             "CREATE_NEW_PROCESS_GROUP"}),
    "signal": frozenset({"CTRL_C_EVENT", "CTRL_BREAK_EVENT"}),
}
#: Environment variables only Windows defines, compared case-insensitively as Windows does.
WIN_ENV = frozenset({"programfiles", "programfiles(x86)", "programw6432", "programdata", "localappdata", "appdata",
                     "userprofile", "systemroot", "windir", "comspec"})
_PLATFORM_ATTRS = {("sys", "platform"), ("os", "name"), ("platform", "system"), ("sys", "getwindowsversion")}
_GUARD_EXCEPTIONS = frozenset({"ImportError", "ModuleNotFoundError", "AttributeError", "OSError", "Exception"})
_EXEMPT = re.compile(r"windows-only-exempt\b:?(.*)")


class ConfigError(RuntimeError):
    """The adopter's configuration or baseline is unusable (exit 2)."""


class NotConfirmed(RuntimeError):
    """The tree could not be read through (exit 3)."""


@dataclass(frozen=True)
class Config:
    baseline: str
    rules: tuple[str, ...]
    marker: str = DEFAULT_MARKER
    test_paths: tuple[str, ...] = DEFAULT_TEST_PATHS
    exclude: tuple[str, ...] = ()


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
        raise ConfigError(f"[windows_only_ratchet] {name} must be a list of non-empty strings")
    return tuple(v.replace("\\", "/") for v in value)


def parse_config(toml_text: str) -> Config | None:
    """`[windows_only_ratchet]` -> `Config`, or None when the stage is not enabled.

    An absent table, or `enabled = false`, is the one correct skip. Anything
    malformed raises `ConfigError`: a typo'd key or rule must not quietly run
    with less than the adopter asked for.
    """
    table = tomllib.loads(toml_text).get("windows_only_ratchet")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError("[windows_only_ratchet] must be a table")
    unknown = sorted(set(table) - _CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"[windows_only_ratchet]: unknown key(s) {', '.join(unknown)}")
    enabled = table.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("[windows_only_ratchet] enabled must be true or false")
    if not enabled:
        return None
    baseline = _baseline.configured(table.get("baseline", DEFAULT_BASELINE), DEFAULT_BASELINE)
    if not isinstance(baseline, str) or not baseline:
        raise ConfigError("[windows_only_ratchet] baseline must be a non-empty string")
    marker = table.get("marker", DEFAULT_MARKER)
    if not isinstance(marker, str) or not marker.isidentifier():
        raise ConfigError("[windows_only_ratchet] marker must be a pytest marker name")
    rules = _str_list(table, "rules", RULES)
    bad = sorted(set(rules) - set(RULES))
    if not rules or bad:
        raise ConfigError(f"[windows_only_ratchet] rules must name rules from: {', '.join(RULES)}"
                          + (f" (unknown: {', '.join(bad)})" if bad else ""))
    return Config(baseline, tuple(r for r in RULES if r in rules), marker,
                  _str_list(table, "test_paths", DEFAULT_TEST_PATHS), _str_list(table, "exclude", ()))


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


# ------------------------------------------------------------------- files

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
    prefix = prefix.strip("/")
    return path == prefix or path.startswith(prefix + "/")


def in_scope(path: str, cfg: Config, vendored: tuple[str, ...]) -> bool:
    """A test module (`test_*.py` / `*_test.py`) under `test_paths`, not vendored or excluded."""
    name = PurePosixPath(path).name
    if not (name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))):
        return False
    if any(_under(path, d) for d in vendored) or matches_any(path, BUILTIN_EXCLUDES + cfg.exclude):
        return False
    return matches_any(path, cfg.test_paths)


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


# --------------------------------------------------------- syntax helpers

def _dotted(node: ast.AST) -> str:
    """`a.b.c` for a Name/Attribute chain, '' for anything else."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _is_win_module(name: str) -> bool:
    top = name.split(".")[0]
    return top in WIN_MODULES or top.startswith(WIN_MODULE_PREFIX)


def _str(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_platform_test(node: ast.AST) -> bool:
    """True when an expression reads the platform (`sys.platform`, `os.name`, `platform.system()`) or `hasattr`."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute):
            tail = _dotted(sub).split(".")[-2:]
            if tuple(tail) in _PLATFORM_ATTRS:
                return True
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "hasattr":
            return True
    return False


def _catches_guard(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(_dotted(n).split(".")[-1] in _GUARD_EXCEPTIONS for n in names)


def _guarded_nodes(scope: ast.AST) -> set[int]:
    """ids of every node under a platform `if`/conditional or an ImportError/AttributeError `try`."""
    guarded: set[int] = set()
    for node in ast.walk(scope):
        branches: list[ast.AST] = []
        if isinstance(node, ast.If) and _is_platform_test(node.test):
            branches = [*node.body, *node.orelse]
        elif isinstance(node, ast.IfExp) and _is_platform_test(node.test):
            branches = [node.body, node.orelse]
        elif isinstance(node, ast.Try) and any(_catches_guard(h) for h in node.handlers):
            branches = list(node.body)
        for branch in branches:
            guarded.update(id(sub) for sub in ast.walk(branch))
    return guarded


def _is_skip_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _dotted(node.func).split(".")[-1] == "skip"


def _platform_skip(body: list[ast.stmt]) -> bool:
    """A top-level `if <platform test>: pytest.skip(...)` -- the scope skips itself off Windows."""
    return any(isinstance(s, ast.If) and _is_platform_test(s.test)
               and any(_is_skip_call(sub) for b in s.body for sub in ast.walk(b)) for s in body)


# ---------------------------------------------------------------- coverage

def _module_aliases(tree: ast.Module, marker: str) -> set[str]:
    """Module-level names bound to the marker or a platform skipif (`windows_only = pytest.mark.skipif(...)`)."""
    names: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            if _is_cover_mark(stmt.value, marker, set()):
                names.add(stmt.targets[0].id)
    return names


def _is_cover_mark(node: ast.AST, marker: str, aliases: set[str]) -> bool:
    """`pytest.mark.<marker>`, a platform `pytest.mark.skipif(...)`, or an alias of either."""
    if isinstance(node, ast.Name) and node.id in aliases:
        return True
    dotted = _dotted(node.func if isinstance(node, ast.Call) else node)
    if dotted.endswith(f"mark.{marker}"):
        return True
    if dotted.endswith("mark.skipif") and isinstance(node, ast.Call):
        return any(_is_platform_test(a) for a in [*node.args, *(k.value for k in node.keywords)])
    return False


def _module_covered(tree: ast.Module, marker: str, aliases: set[str]) -> bool:
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets):
            values = stmt.value.elts if isinstance(stmt.value, (ast.List, ast.Tuple)) else [stmt.value]
            if any(_is_cover_mark(v, marker, aliases) for v in values):
                return True
        call = stmt.value if isinstance(stmt, (ast.Expr, ast.Assign)) else None
        if isinstance(call, ast.Call) and _dotted(call.func).endswith("importorskip"):
            name = _str(call.args[0]) if call.args else None
            if name and _is_win_module(name):
                return True
    return _platform_skip(tree.body)


# ------------------------------------------------------------------- rules

@dataclass
class _Imports:
    """Local names bound to the modules the attribute rules watch (`import subprocess as sp`)."""

    modules: dict[str, str] = field(default_factory=dict)  # local name -> module
    environ: set[str] = field(default_factory=set)  # local names bound to os.environ


def _imports(tree: ast.Module) -> _Imports:
    found = _Imports()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in WIN_ATTRS:
                    found.modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            found.environ.update(a.asname or a.name for a in node.names if a.name == "environ")
    return found


def _attr_module(node: ast.Attribute, imports: _Imports) -> str | None:
    """Which watched module `node.value` is: a name imported as it, or any `x.<module>` attribute."""
    if isinstance(node.value, ast.Name):
        return imports.modules.get(node.value.id)
    if isinstance(node.value, ast.Attribute) and node.value.attr in WIN_ATTRS:
        return node.value.attr
    return None


def _is_environ(node: ast.AST, imports: _Imports) -> bool:
    if _str(node) == "os.environ":  # patch.dict("os.environ", {...})
        return True
    if isinstance(node, ast.Name):
        return node.id in imports.environ
    return isinstance(node, ast.Attribute) and node.attr == "environ" and _dotted(node.value).split(".")[-1] == "os"


def _env_sets(scope: ast.AST, imports: _Imports) -> set[str]:
    """Windows-only variables a scope sets: `setenv`, `os.environ[...] =`, `setitem` or `patch.dict` on environ."""
    found: set[str] = set()
    for node in ast.walk(scope):
        keys: list[str | None] = []
        if isinstance(node, ast.Call):
            tail = _dotted(node.func).split(".")[-1]
            if tail == "setenv" and node.args:
                keys = [_str(node.args[0])]
            elif tail == "setitem" and len(node.args) >= 2 and _is_environ(node.args[0], imports):
                keys = [_str(node.args[1])]
            elif tail in ("dict", "update") and node.args:
                target = node.func.value if tail == "update" and isinstance(node.func, ast.Attribute) else node.args[0]
                mapping = node.args[0] if tail == "update" else (node.args[1] if len(node.args) > 1 else None)
                if _is_environ(target, imports) and isinstance(mapping, ast.Dict):
                    keys = [_str(k) for k in mapping.keys]
        elif isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            keys = [_str(t.slice) for t in targets if isinstance(t, ast.Subscript) and _is_environ(t.value, imports)]
        found.update(k.lower() for k in keys if k and k.lower() in WIN_ENV)
    return found


def _fakes_platform(scope: ast.AST) -> bool:
    """`monkeypatch.setattr(sys, "platform", ...)`, `patch("sys.platform", ...)`, `patch.object(os, "name", ...)`."""
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call) or _dotted(node.func).split(".")[-1] not in ("setattr", "patch", "object"):
            continue
        args = node.args
        first = _str(args[0]) if args else None
        if first and tuple(first.split(".")[-2:]) in _PLATFORM_ATTRS:
            return True
        if len(args) >= 2 and _str(args[1]) and (_dotted(args[0]).split(".")[-1], _str(args[1])) in _PLATFORM_ATTRS:
            return True
    return False


def _exe_paths(scope: ast.AST, guarded: set[int]) -> list[int]:
    """Lines building an on-disk `.exe` path: `x / "a.exe"`, `Path(..., "a.exe")`, `joinpath` / `os.path.join`."""
    lines: list[int] = []
    for node in ast.walk(scope):
        if id(node) in guarded:
            continue
        parts: list[ast.AST] = []
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            parts = [node.right]
        elif isinstance(node, ast.Call) and _dotted(node.func).split(".")[-1] in ("Path", "joinpath", "join"):
            parts = list(node.args)
        if any((_str(p) or "").lower().endswith(".exe") for p in parts):
            lines.append(node.lineno)  # type: ignore[attr-defined]  # BinOp and Call both carry lineno
    return sorted(set(lines))


def _scope_findings(scope: ast.AST, nodes: list[ast.AST], imports: _Imports, file_sets: set[str],
                    guarded: set[int], label: str) -> list[tuple[int, str, str]]:
    """The four API rules over `nodes` (one test's body, or the module's own statements)."""
    out: list[tuple[int, str, str]] = []
    fakes = _fakes_platform(scope)
    for top in nodes:
        for node in ast.walk(top):
            if id(node) in guarded:
                continue
            if isinstance(node, ast.Import):
                out += [(node.lineno, "win-module", f"{label} imports {a.name}") for a in node.names
                        if _is_win_module(a.name)]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                if _is_win_module(node.module):
                    out.append((node.lineno, "win-module", f"{label} imports from {node.module}"))
                for a in node.names:
                    if a.name in WIN_ATTRS.get(node.module, ()):
                        out.append((node.lineno, "win-api", f"{label} imports {node.module}.{a.name}"))
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                module = _attr_module(node, imports)
                if module and node.attr in WIN_ATTRS[module]:
                    out.append((node.lineno, "win-api", f"{label} uses {module}.{node.attr}"))
            elif (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
                  and _is_environ(node.value, imports)):
                key = (_str(node.slice) or "").lower()
                if key in WIN_ENV and key not in file_sets and not fakes:
                    out.append((node.lineno, "win-env-read", f"{label} reads os.environ[{_str(node.slice)!r}], "
                                                             "which only Windows defines"))
    if not isinstance(scope, ast.Module) and not fakes:
        env = sorted(_env_sets(scope, imports))
        exe = _exe_paths(scope, guarded)
        if env and exe:
            out.append((exe[0], "win-exe-lookup", f"{label} points {env[0]} at a folder and plants a .exe there: "
                                                  "a Windows install lookup no other OS resolves"))
    return out


# -------------------------------------------------------------- exemptions

def _comments(text: str) -> dict[int, str]:
    """line -> comment text (without `#`); a file that does not tokenize was already refused by `ast`."""
    found: dict[int, str] = {}
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == tokenize.COMMENT:
            found[tok.start[0]] = tok.string.lstrip("#").strip()
    return found


def _marker_above(comments: dict[int, str], line: int, lines: list[str]) -> tuple[int, str] | None:
    """The exempt marker on `line` or in the unbroken comment block right above it."""
    probe = line
    while probe >= 1:
        if probe in comments and _EXEMPT.search(comments[probe]):
            return probe, comments[probe]
        if probe != line and not lines[probe - 1].lstrip().startswith("#"):
            return None
        probe -= 1
    return None


def _reason(comment: str) -> str:
    match = _EXEMPT.search(comment)
    return match.group(1).strip() if match else ""


# -------------------------------------------------------------------- scan

@dataclass
class FileScan:
    findings: list[tuple[int, str, str]] = field(default_factory=list)
    exempted: list[tuple[int, str, str]] = field(default_factory=list)  # (line, rule, reason)


def _tests(tree: ast.Module) -> list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, list[ast.expr]]]:
    """Every `test*` function with the decorators that cover it (its own plus its class's)."""
    found: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, list[ast.expr]]] = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name.startswith("test"):
            found.append((stmt, list(stmt.decorator_list)))
        elif isinstance(stmt, ast.ClassDef) and stmt.name.startswith("Test"):
            found += [(m, [*stmt.decorator_list, *m.decorator_list]) for m in stmt.body
                      if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name.startswith("test")]
    return found


def scan_source(text: str, path: str, marker: str = DEFAULT_MARKER) -> FileScan:
    """Hits in one test module, after coverage and exemptions. Raises `NotConfirmed` when it does not parse."""
    try:
        tree = ast.parse(text, filename=path)
    except (SyntaxError, ValueError) as exc:
        raise NotConfirmed(f"{path}: does not parse: {exc}") from exc
    result = FileScan()
    comments = _comments(text)
    lines = text.splitlines()
    for line, comment in comments.items():
        if _EXEMPT.search(comment) and not _reason(comment):
            result.findings.append((line, "bare-exempt", "a windows-only-exempt marker needs a reason"))
    aliases = _module_aliases(tree, marker)
    if _module_covered(tree, marker, aliases):
        return result
    imports = _imports(tree)
    file_sets = _env_sets(tree, imports)
    guarded = _guarded_nodes(tree)
    tests = _tests(tree)
    in_functions = {id(n) for s in tree.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    for n in ast.walk(s)}
    module_nodes: list[ast.AST] = [s for s in tree.body if id(s) not in in_functions]
    # Each finding carries the lines whose marker covers its whole test: the first decorator and the `def`.
    raw: list[tuple[tuple[int, str, str], tuple[int, ...]]] = [
        (f, ()) for f in _scope_findings(tree, module_nodes, imports, file_sets, guarded, "the module")]
    for func, decorators in tests:
        if any(_is_cover_mark(d, marker, aliases) for d in decorators) or _platform_skip(func.body):
            continue
        heads: tuple[int, ...] = (min([func.lineno, *(d.lineno for d in decorators)]), func.lineno)
        raw += [(f, heads) for f in _scope_findings(func, list(func.body), imports, file_sets, guarded,
                                                     f"{func.name}()")]
    for (line, rule, message), covers in raw:
        mark = next((m for m in (_marker_above(comments, n, lines) for n in (line, *covers)) if m), None)
        if mark is not None and _reason(mark[1]):
            result.exempted.append((line, rule, _reason(mark[1])))
        else:
            result.findings.append((line, rule, message))
    return result


@dataclass
class Tree:
    found: dict[str, list[Hit]] = field(default_factory=dict)
    exempted: list[tuple[str, int, str, str]] = field(default_factory=list)  # (path, line, rule, reason)
    scanned: int = 0


def scan_tree(root: Path, cfg: Config, vendored: tuple[str, ...]) -> Tree:
    tree = Tree()
    for path in tracked_files(root):
        if not in_scope(path, cfg, vendored):
            continue
        text = _read_text(root, path)
        if text is None:
            continue
        tree.scanned += 1
        scanned = scan_source(text, path, cfg.marker)
        tree.exempted += [(path, line, rule, reason) for line, rule, reason in scanned.exempted]
        hits = [Hit(path, line, rule, msg) for line, rule, msg in scanned.findings if rule in cfg.rules]
        if hits:
            tree.found[path] = sorted(hits, key=lambda h: (h.line, h.rule))
    return tree


# ---------------------------------------------------------------- baseline

Counts = dict[str, Counter[str]]  # path -> Counter(rule)


def current_counts(found: dict[str, list[Hit]]) -> Counts:
    return {path: Counter(h.rule for h in hits) for path, hits in found.items()}


def read_baseline(path: Path) -> Counts:
    """Baseline file -> ``{test path: Counter(rule)}``. Raises `ConfigError` on anything off."""
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
    print(f"windows only ratchet: {_total(added)} hit(s) beyond the baseline, in {len(added)} file(s):")
    for path, extra in added.items():
        for rule in RULES:
            if not extra[rule]:
                continue
            had = baseline.get(path, Counter())[rule]
            print(f"  {path}: [{rule}] {had + extra[rule]} hit(s), baseline {had}:")
            for h in found[path]:
                if h.rule == rule:
                    print(f"    {path}:{h.line}: [{rule}] {h.message}")


def _report_context(tree: Tree) -> None:
    print(f"  {tree.scanned} test module(s) read; {len(tree.exempted)} exemption(s)" + (":" if tree.exempted else ""))
    for path, line, rule, reason in sorted(tree.exempted):
        print(f"    {path}:{line}: [{rule}] {reason}")


def _check(cfg: Config, tree: Tree, current: Counts, baseline: Counts) -> int:
    added, gone = diff(current, baseline)
    if added:
        _report_added(added, tree.found, baseline)
        print(f"  Mark the test @pytest.mark.{cfg.marker} (or the module's pytestmark) so the Mac nightly leaves it "
              "out, or fake the platform instead of using it (docs/windows-only-ratchet.md). A hit that must stay "
              "carries `windows-only-exempt: <reason>`; or (reviewed) raise the baseline with "
              "`--update-baseline --allow-additions`.")
        _report_context(tree)
        return EXIT_RISE
    print(f"windows only ratchet: PASS ({_total(current)} baselined hit(s) in {len(current)} file(s), none new; "
          f"marker: {cfg.marker})")
    _report_context(tree)
    if gone:
        print(f"  {gone} baselined hit(s) are gone -- run "
              "`python scripts/windows_only_ratchet.py --update-baseline` to lower it")
    return EXIT_PASS


def _load_baseline(baseline_path: Path, cfg: Config, init: bool) -> Counts | int:
    """The committed baseline (empty for `--init`), or the exit code that refuses the run."""
    if init:
        if baseline_path.exists():
            print(f"windows only ratchet: {cfg.baseline} already exists; --init never overwrites it")
            return EXIT_CONFIG
        return {}
    if not baseline_path.is_file():
        print(f"windows only ratchet: FAIL: baseline {cfg.baseline} is missing -- "
              "run `python scripts/windows_only_ratchet.py --init`")
        return EXIT_RISE
    try:
        return read_baseline(baseline_path)
    except ConfigError as exc:
        print(f"windows only ratchet: BASELINE ERROR: {exc}")
        return EXIT_CONFIG


def run(root: Path, *, init: bool, update: bool, allow_additions: bool) -> int:
    try:
        cfg, vendored = load_config(root)
    except ConfigError as exc:
        print(f"windows only ratchet: CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    if cfg is None:
        if init or update:
            print("windows only ratchet: not enabled -- set [windows_only_ratchet] enabled = true in .fleet.toml first")
            return EXIT_CONFIG
        print("windows only ratchet: not enabled (no [windows_only_ratchet] enabled = true in .fleet.toml)")
        return EXIT_PASS
    baseline_path = _baseline.resolve(root, cfg.baseline, DEFAULT_BASELINE, "windows only ratchet")
    if baseline_path is None:
        return EXIT_CONFIG
    baseline = _load_baseline(baseline_path, cfg, init)
    if isinstance(baseline, int):
        return baseline
    try:
        tree = scan_tree(root, cfg, vendored)
    except NotConfirmed as exc:
        print(f"windows only ratchet: NOT CONFIRMED: {exc}")
        print("  (the tree could not be read through, so this is neither a pass nor a clean tree)")
        return EXIT_NOT_CONFIRMED
    current = current_counts(tree.found)

    if init:
        # runtime-write-exempt: the repo's own baseline file, named by the config, never a code folder
        baseline_path.write_text(render_baseline(current), encoding="utf-8", newline="\n")
        print(f"windows only ratchet: wrote {cfg.baseline} with {_total(current)} hit(s) in {len(current)} file(s)")
        return EXIT_PASS
    if not update:
        return _check(cfg, tree, current, baseline)
    added, gone = diff(current, baseline)
    if added and not allow_additions:
        _report_added(added, tree.found, baseline)
        print("windows only ratchet: nothing written -- fix the tests, or raise the baseline with --allow-additions")
        return EXIT_RISE
    # runtime-write-exempt: the repo's own baseline file, named by the config, never a code folder
    baseline_path.write_text(render_baseline(current), encoding="utf-8", newline="\n")
    print(f"windows only ratchet: {cfg.baseline} now has {_total(current)} hit(s) in {len(current)} file(s) "
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
    # Reports contain non-ASCII; a cp1252 console would otherwise crash after the verdict.
    for _stream in (sys.stdout, sys.stderr):
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # real console streams are TextIOWrapper
    sys.exit(main())
