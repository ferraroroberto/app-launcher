"""Static gate: a comment or docstring never names a flat module a package split retired.

``src/`` is grouped into domain packages, so ``src/board/exchange.py`` was once
``board_exchange.py``. Code moves in the split; the prose pointing at it does
not, and a reader following ``board_exchange._read_tail`` lands on nothing. The
retired name is always ``<package>_<module>``, so the set is derived from the
tree and tracks every future split. A name still used as a file stem, an
identifier or a string key (an import alias, the ``desktop_browser`` setting) is
not retired.

Only that derivable subset is checked: a rename that is not ``<package>_<module>``
(``launch_flags`` -> ``src.launching.flags``) leaves no trace in the tree.
"""

import ast
import io
import re
import tokenize
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCANNED_ROOTS = ("src", "app")


def _py_files(*roots: str):
    for root in roots:
        yield from (PROJECT_ROOT / root).rglob("*.py")


def _retired_names() -> set:
    derived = {
        f"{path.parent.name}_{path.stem}"
        for path in _py_files("src")
        if path.stem != "__init__" and (path.parent / "__init__.py").is_file()
    }
    live = {path.stem for path in _py_files("src", "app", "scripts", "tests")}
    for path in _py_files("src", "app"):
        source = path.read_text(encoding="utf-8")
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.NAME:
                live.add(token.string)
            elif token.type == tokenize.STRING:
                live.add(token.string.strip("\"'"))
    return derived - live


def _prose(source: str):
    """Every comment and docstring in ``source`` as ``(line, text)``."""
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            yield token.start[0], token.string
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                yield getattr(node.body[0], "lineno", 1), doc


def test_no_comment_or_docstring_names_a_retired_flat_module():
    retired = _retired_names()
    assert retired, "no package modules found; the derivation is broken"
    pattern = re.compile(r"(?<![\w.])(" + "|".join(sorted(retired)) + r")(?![\w])")
    hits = [
        f"{path.relative_to(PROJECT_ROOT).as_posix()}:{line}: {match.group(1)}"
        for path in _py_files(*SCANNED_ROOTS)
        for line, text in _prose(path.read_text(encoding="utf-8"))
        for match in pattern.finditer(text)
    ]
    assert not hits, "name the module's current dotted path instead:\n" + "\n".join(hits)
