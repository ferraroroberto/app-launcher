"""The comment stripper that shrinks the JS the phone downloads."""

from __future__ import annotations

import gzip
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.webapp.static_trim import trim_js
from app.webapp.routers._helpers import STATIC_DIR


@pytest.mark.parametrize(
    "source, expected",
    [
        ("a = 1; // note\nb = 2;\n", "a = 1;\nb = 2;\n"),
        ("a = 1;\n  // whole line\nb = 2;\n", "a = 1;\nb = 2;\n"),
        ("a = 1;\n/* block\n   over lines */\nb = 2;\n", "a = 1;\nb = 2;\n"),
        ("a = 1; /* tail */\nb = 2;\n", "a = 1;\nb = 2;\n"),
        ("f(a, /* why */ b);\n", "f(a, b);\n"),
        ("return/**/x;\n", "return x;\n"),
    ],
)
def test_comments_go_and_empty_lines_with_them(source: str, expected: str) -> None:
    assert trim_js(source) == expected


def test_a_comment_over_a_line_break_leaves_one() -> None:
    # `a` and `b` are two statements only because of that line break.
    assert trim_js("a = 1/* x\n y */b = 2\n") == "a = 1\nb = 2\n"


@pytest.mark.parametrize(
    "source",
    [
        "const u = 'https://example.test/a'; // go\n",
        'const u = "a // b /* c */";\n',
        "const t = `line\n// not a comment\n/* nor this */ ${x}`;\n",
        "const r = /\\/\\//.test(s);\n",
        "const r = /[/*]/.test(s);\n",
        "const r = x ? /'/g : /\"/g;\n",
        "const half = total / 2; const rest = total / 3;\n",
        "const html = `<a href=\"//cdn.test/x\">${label}</a>`;\n",
        "/*! keep this licence */\nrun();\n",
    ],
)
def test_code_that_only_looks_like_a_comment_is_kept(source: str) -> None:
    kept = trim_js(source)
    # Only the trailing `// go` of the first case may go.
    assert kept == source.replace(" // go", "")


def test_regex_after_return_and_division_after_a_call() -> None:
    source = "function f(s) { return /\\/\\//.test(s) / g(2); } // done\n"
    assert trim_js(source) == "function f(s) { return /\\/\\//.test(s) / g(2); }\n"


def test_comment_inside_a_template_expression_goes() -> None:
    assert trim_js("const t = `a ${ b /* c */ + 1 } d`;\n") == "const t = `a ${ b + 1 } d`;\n"


def test_nested_template_keeps_its_text() -> None:
    source = "const t = `a ${ `b // ${c}` } d`; // x\n"
    assert trim_js(source) == "const t = `a ${ `b // ${c}` } d`;\n"


@pytest.mark.parametrize(
    "source",
    ["a = 1; /* never closed\nb = 2;\n", "const t = `never closed ${x}\n", "const t = `${ x \n"],
)
def test_a_source_that_does_not_end_cleanly_comes_back_unchanged(source: str) -> None:
    assert trim_js(source) == source


def test_a_string_never_runs_past_its_line() -> None:
    # An unclosed quote (here a regex the scanner read as division) must not
    # swallow the next line's comment into "string".
    source = "a = b / 'c;\n// gone\nd = 1;\n"
    assert trim_js(source) == "a = b / 'c;\nd = 1;\n"


def test_nothing_to_strip_is_returned_as_is() -> None:
    assert trim_js("const a = 1;\n") == "const a = 1;\n"


def _served_modules() -> list[Path]:
    return sorted(
        p for p in STATIC_DIR.rglob("*.js") if p.relative_to(STATIC_DIR).parts[0] != "vendor"
    )


def test_trimming_is_idempotent_on_every_shipped_module() -> None:
    for path in _served_modules():
        once = trim_js(path.read_text(encoding="utf-8"))
        assert trim_js(once) == once, path


def test_trimming_cuts_the_gzipped_module_graph_by_a_third_or_more() -> None:
    # The cold launch's budget leans on this; a scanner that quietly gave up
    # on files (returning them unchanged) would show here first.
    before = after = 0
    for path in _served_modules():
        source = path.read_text(encoding="utf-8")
        before += len(gzip.compress(source.encode("utf-8")))
        after += len(gzip.compress(trim_js(source).encode("utf-8")))
    assert after < before * 0.67, (before, after)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_every_trimmed_shipped_module_still_parses(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node
    paths = []
    for index, path in enumerate(_served_modules()):
        target = tmp_path / f"{index}.mjs"
        target.write_text(trim_js(path.read_text(encoding="utf-8")), encoding="utf-8")
        paths.append((path, target))

    def check(pair: tuple[Path, Path]) -> tuple[Path, int, str]:
        done = subprocess.run(
            [node, "--check", str(pair[1])],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return pair[0], done.returncode, done.stderr

    with ThreadPoolExecutor(max_workers=8) as pool:
        failures = [(p, err) for p, code, err in pool.map(check, paths) if code != 0]
    assert not failures, failures[:3]
