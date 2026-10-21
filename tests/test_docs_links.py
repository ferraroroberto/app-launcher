"""Every relative link and ``#anchor`` in the repo's prose docs resolves (#1752).

README.md is an index into ``docs/``, so a section moved to a topic page, or a
heading renamed, silently breaks the links that point at it. A broken link
reads fine in the editor and only fails when someone taps it, so the build
fails instead. Read-only: it parses the markdown, it never fetches a URL.

Modelled on fleet-config's ``docs_links`` check (fleet-config#1515, step 1).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterator, List, Set, Tuple

_REPO_ROOT = Path(__file__).resolve().parent.parent

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
# [text](target) or [text](target "title"); the target may not contain spaces
# or parentheses, which is how every link in these docs is written.
_LINK_RE = re.compile(r"\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_EXTERNAL_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//)", re.IGNORECASE)


def doc_files() -> List[Path]:
    return [
        _REPO_ROOT / "README.md",
        _REPO_ROOT / "CLAUDE.md",
        *sorted((_REPO_ROOT / "docs").glob("*.md")),
    ]


def slugify(heading: str) -> str:
    """GitHub's heading-to-anchor rule: lowercase, drop punctuation, spaces
    become hyphens (so ``Terminal ⇄ Chat`` is ``terminal--chat``)."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # [t](u) -> t
    text = text.replace("`", "").replace("*", "").replace("~", "")
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _prose_lines(text: str) -> Iterator[Tuple[int, str]]:
    """Yield ``(lineno, line)`` for every line outside a fenced code block."""
    fence = False
    for lineno, line in enumerate(text.splitlines(), 1):
        if _FENCE_RE.match(line):
            fence = not fence
            continue
        if not fence:
            yield lineno, line


def heading_slugs(text: str) -> Set[str]:
    seen: Dict[str, int] = {}
    slugs: Set[str] = set()
    for _, line in _prose_lines(text):
        match = _HEADING_RE.match(line)
        if not match:
            continue
        base = slugify(match.group(2))
        count = seen.get(base, 0)
        seen[base] = count + 1
        slugs.add(base if count == 0 else f"{base}-{count}")
    return slugs


def broken_links(files: List[Path]) -> List[str]:
    """One ``file:line -> target (why)`` string per link that does not resolve."""
    slug_cache: Dict[Path, Set[str]] = {}

    def slugs_of(path: Path) -> Set[str]:
        if path not in slug_cache:
            slug_cache[path] = heading_slugs(path.read_text(encoding="utf-8"))
        return slug_cache[path]

    problems: List[str] = []
    for doc in files:
        text = doc.read_text(encoding="utf-8")
        rel_doc = doc.relative_to(_REPO_ROOT).as_posix()
        for lineno, line in _prose_lines(text):
            for match in _LINK_RE.finditer(_INLINE_CODE_RE.sub("", line)):
                target = match.group(1)
                if _EXTERNAL_RE.match(target):
                    continue
                path_part, _, anchor = target.partition("#")
                resolved = doc if not path_part else (doc.parent / path_part).resolve()
                where = f"{rel_doc}:{lineno} -> {target}"
                if not resolved.exists():
                    problems.append(f"{where} (no such file)")
                elif anchor and resolved.suffix == ".md":
                    if anchor.lower() not in slugs_of(resolved):
                        problems.append(f"{where} (no such heading)")
    return problems


def test_relative_links_and_anchors_resolve():
    problems = broken_links(doc_files())
    assert not problems, "broken doc links:\n" + "\n".join(problems)


def test_the_checker_catches_a_broken_link_and_a_broken_anchor(tmp_path):
    """The check is only worth having if it can fail: prove each leg does."""
    global _REPO_ROOT
    page = tmp_path / "page.md"
    other = tmp_path / "other.md"
    other.write_text("# Other\n\n## A heading\n", encoding="utf-8")
    page.write_text(
        "[ok](other.md#a-heading) [ok2](#local)\n\n# Local\n\n"
        "[no file](missing.md) [no anchor](other.md#nope) [no local](#nope)\n"
        "`[in code](missing.md)` and a fence:\n```\n[x](missing.md)\n```\n",
        encoding="utf-8",
    )
    saved = _REPO_ROOT
    _REPO_ROOT = tmp_path
    try:
        problems = broken_links([page])
    finally:
        _REPO_ROOT = saved
    assert len(problems) == 3, problems
    assert any("missing.md (no such file)" in p for p in problems)
    assert any("other.md#nope (no such heading)" in p for p in problems)
    assert any("#nope (no such heading)" in p and "other.md" not in p for p in problems)


def test_slugify_matches_github_for_the_headings_the_docs_link_to():
    assert slugify("HTTPS certificate (Tailscale)") == "https-certificate-tailscale"
    assert slugify("Terminal ⇄ Chat") == "terminal--chat"
    assert slugify("Installing the `Codex` CLI") == "installing-the-codex-cli"
    assert slugify("Screen time to task-os (#1678)") == "screen-time-to-task-os-1678"
