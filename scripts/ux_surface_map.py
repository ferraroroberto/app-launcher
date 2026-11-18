"""The UX surface map: read `docs/ux-surfaces.yaml`, generate `docs/ux-surface-map.md`.

A web app keeps one structured file listing every surface a person can reach
(docs/ux-surface-map.md in project-scaffolding is the convention). This module
reads it and renders the readable page: entries grouped by area, one Mermaid
`flowchart` per area drawn from each surface's `links`, and a link index. The
output is deterministic (source order, no timestamps), so a second run is
byte-identical and the UX surface ratchet can fail a page that was edited by
hand or not regenerated.

The file is read by a strict YAML subset reader, not a YAML library: the
ratchet that reads it is vendored stdlib-only into every adopter, and the
reader keeps the line numbers and `#` comments the ratchet needs for its hits
and its `<rule>-exempt: <reason>` markers. Everything the subset accepts is
valid YAML that any YAML tool reads the same way; anything outside it is an
error naming the line, never a silent misread. The subset:

  * block mappings with identifier keys (`id:`, `walk:`), block sequences
    (`- item`, `- key: value`), indentation by spaces only;
  * scalars: plain, 'single-quoted', "double-quoted" (backslash escapes for
    backslash, quote, slash, n, t and u plus four hex digits), on one line;
    `|` and `>` block scalars (optionally `-`) for prose;
  * flow sequences of scalars on one line (`[390, 1280]`, `[]`);
  * `#` comments on their own line or after a value;
  * every scalar is text; a plain `null`, `~`, `true`, `false`, `yes`, `no`,
    `on` or `off` is refused (YAML would not read it as text), so quote it.

Not accepted: anchors, aliases, tags, flow mappings, multi-line plain or quoted
scalars, `?` keys, more than one document.

    python scripts/ux_surface_map.py            # write the page
    python scripts/ux_surface_map.py --check    # exit 1 when the page is not what the map generates

The paths come from `[ux_surface_ratchet]` `map` and `page` in `.fleet.toml`
when set, else the defaults below. Exit codes: 0 written or up to date; 1 the
map is invalid or missing, or (`--check`) the page differs; 2 bad configuration
or arguments.

Vendor-verbatim (manifest key `ux_surface_map`), stdlib-only; the UX surface
ratchet loads it as a sibling.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAP = "docs/ux-surfaces.yaml"
DEFAULT_PAGE = "docs/ux-surface-map.md"
#: The e2e capture hook's name form; ids outside it are the ratchet's `invalid-surface-id` hits, not errors here.
SURFACE_ID = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*\Z")
ROOT_ELEMENT = "data-surface"

EXIT_OK, EXIT_FAIL, EXIT_CONFIG = 0, 1, 2


class MapError(ValueError):
    """The map is not in the YAML subset, or does not follow the schema; `line` is 1-based."""

    def __init__(self, path: str, line: int, message: str) -> None:
        super().__init__(f"{path}:{line}: {message}")
        self.path, self.line, self.message = path, line, message


# ------------------------------------------------------------ YAML subset

class Mapping(dict[str, Any]):
    """A block mapping, with the line it opens on and the line of each key."""

    def __init__(self, line: int) -> None:
        super().__init__()
        self.line = line
        self.key_lines: dict[str, int] = {}


class Sequence(list[Any]):
    """A block or flow sequence, with the line it opens on and the line of each item."""

    def __init__(self, line: int) -> None:
        super().__init__()
        self.line = line
        self.item_lines: list[int] = []


_KEY = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_-]*)[ \t]*:(?=[ \t]|$)")
_NOT_TEXT = frozenset({"null", "~", "true", "false", "yes", "no", "on", "off"})
_PLAIN_FIRST = set("[]{},&*!|>'\"%@`#")
_ESCAPES = {"\\": "\\", '"': '"', "n": "\n", "t": "\t", "/": "/"}
_BLOCK_HEADER = re.compile(r"(?P<style>[|>])(?P<chomp>-?)[ \t]*(?:#.*)?\Z")


class _Reader:
    def __init__(self, text: str, path: str) -> None:
        self.path = path
        self.lines = text.removeprefix("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        self.i = 0

    def fail(self, index: int, message: str) -> MapError:
        return MapError(self.path, index + 1, message)

    def _indent(self, index: int) -> int:
        raw = self.lines[index]
        stripped = raw.lstrip(" ")
        if stripped.startswith("\t"):
            raise self.fail(index, "a tab in the indentation (indent with spaces)")
        return len(raw) - len(stripped)

    def peek(self) -> tuple[int, str] | None:
        """(indent, content) of the next line holding content, skipping blank and comment lines; None at the end."""
        while self.i < len(self.lines):
            content = self.lines[self.i].strip()
            if content and not content.startswith("#"):
                indent = self._indent(self.i)
                if indent == 0 and (content.startswith(("---", "...", "%"))):
                    raise self.fail(self.i, "document markers and directives are outside the subset")
                return indent, content
            self.i += 1
        return None

    def document(self) -> Mapping:
        first = self.peek()
        if first is None:
            raise self.fail(0, "the map is empty")
        if first[0] != 0 or _is_item(first[1]) or not _KEY.match(first[1]):
            raise self.fail(self.i, "the map must be a mapping starting at column 0")
        top = self.mapping(0)
        if self.peek() is not None:
            raise self.fail(self.i, "unexpected content after the top-level mapping")
        return top

    def block(self, indent: int) -> Mapping | Sequence:
        nxt = self.peek()
        assert nxt is not None
        return self.sequence(indent) if _is_item(nxt[1]) else self.mapping(indent)

    def mapping(self, indent: int) -> Mapping:
        out = Mapping(self.i + 1)
        while (nxt := self.peek()) is not None and nxt[0] == indent and not _is_item(nxt[1]):
            match = _KEY.match(nxt[1])
            if match is None:
                raise self.fail(self.i, "expected `key: value` (keys are plain identifiers)")
            key = match.group("key")
            if key in out:
                raise self.fail(self.i, f"duplicate key `{key}`")
            line = self.i
            out.key_lines[key] = line + 1
            self.i += 1
            out[key] = self.value(nxt[1][match.end():], indent, line, allow_same_indent_seq=True)
        self._no_deeper(indent)
        return out

    def sequence(self, indent: int) -> Sequence:
        out = Sequence(self.i + 1)
        while (nxt := self.peek()) is not None and nxt[0] == indent and _is_item(nxt[1]):
            rest = nxt[1][1:].lstrip(" ")
            line = self.i
            out.item_lines.append(line + 1)
            if rest and not rest.startswith("#") and _KEY.match(rest):
                item_indent = indent + len(nxt[1]) - len(rest)
                self.lines[line] = " " * item_indent + rest  # read the item's first key at its own column
                out.append(self.mapping(item_indent))
            else:
                self.i += 1
                out.append(self.value(rest, indent, line, allow_same_indent_seq=False))
        self._no_deeper(indent)
        return out

    def _no_deeper(self, indent: int) -> None:
        nxt = self.peek()
        if nxt is not None and nxt[0] > indent:
            raise self.fail(self.i, "unexpected indentation (a value spanning lines goes in a `>` block)")

    def value(self, rest: str, indent: int, line: int, *, allow_same_indent_seq: bool) -> Any:
        rest = rest.strip(" \t")
        if rest and rest[0] in "|>":
            return self.block_scalar(rest, indent, line)
        if rest.startswith("["):
            return self.flow(rest, line)
        if rest and not rest.startswith("#"):
            return self.scalar(rest, line)
        nxt = self.peek()
        if nxt is not None and nxt[0] > indent:
            return self.block(nxt[0])
        if nxt is not None and allow_same_indent_seq and nxt[0] == indent and _is_item(nxt[1]):
            return self.sequence(indent)
        return None

    def scalar(self, text: str, line: int, *, flow: bool = False) -> str:
        if text[0] in "'\"":
            value, end = self.quoted(text, line)
            tail = text[end:].strip(" \t")
            if tail and not (tail.startswith("#") and text[end:end + 1] in (" ", "\t")):
                raise self.fail(line, f"unexpected text after a quoted value: {tail!r}")
            return value
        return self.plain(text, line, flow=flow)

    def plain(self, text: str, line: int, *, flow: bool = False) -> str:
        cut = re.search(r"[ \t]#", text)
        value = (text[:cut.start()] if cut else text).strip(" \t")
        if not value:
            raise self.fail(line, "empty value")
        if value[0] in _PLAIN_FIRST or value.startswith(("- ", "? ", ": ")) or value in ("-", "?"):
            raise self.fail(line, f"a value starting with {value[0]!r} must be quoted")
        if ": " in value or value.endswith(":") or (flow and any(c in value for c in "[]{},")):
            raise self.fail(line, f"quote this value: {value!r}")
        if value.lower() in _NOT_TEXT:
            raise self.fail(line, f"YAML reads plain {value!r} as a non-text value; quote it")
        return value

    def quoted(self, text: str, line: int) -> tuple[str, int]:
        """(the value, the index just past its closing quote)."""
        quote = text[0]
        out: list[str] = []
        i = 1
        while i < len(text):
            char = text[i]
            if char == quote:
                if quote == "'" and text[i + 1:i + 2] == "'":
                    out.append("'")
                    i += 2
                    continue
                return "".join(out), i + 1
            if quote == '"' and char == "\\":
                code = text[i + 1:i + 2]
                if code == "u" and re.fullmatch(r"[0-9A-Fa-f]{4}", text[i + 2:i + 6]):
                    out.append(chr(int(text[i + 2:i + 6], 16)))
                    i += 6
                    continue
                if code not in _ESCAPES:
                    raise self.fail(line, f"unsupported escape \\{code}")
                out.append(_ESCAPES[code])
                i += 2
                continue
            out.append(char)
            i += 1
        raise self.fail(line, "a quoted value must close on its own line")

    def flow(self, text: str, line: int) -> Sequence:
        out = Sequence(line + 1)
        items = _split_flow(text[1:])
        if items is None:
            raise self.fail(line, "a `[...]` list must close on its own line")
        if isinstance(items, str):
            raise self.fail(line, items)
        parts, tail = items
        tail = tail.strip(" \t")
        if tail and not tail.startswith("#"):
            raise self.fail(line, f"unexpected text after a list: {tail!r}")
        if "".join(parts).strip(" \t"):
            for item in parts:
                if not item.strip(" \t"):
                    raise self.fail(line, "an empty item in a `[...]` list")
                out.append(self.scalar(item.strip(" \t"), line, flow=True))
                out.item_lines.append(line + 1)
        return out

    def block_scalar(self, header: str, indent: int, line: int) -> str:
        match = _BLOCK_HEADER.match(header)
        if match is None:
            raise self.fail(line, f"unsupported block scalar header {header!r} (use |, |-, > or >-)")
        raw: list[str] = []
        block_indent = -1
        while self.i < len(self.lines):
            text = self.lines[self.i]
            if text.strip():
                here = self._indent(self.i)
                if here <= indent:
                    break
                if block_indent < 0:
                    block_indent = here
                elif here < block_indent:
                    raise self.fail(self.i, "a block scalar line indented less than its first line")
                if match.group("style") == ">" and here > block_indent:
                    raise self.fail(self.i, "a more-indented line in a `>` block is outside the subset; use `|`")
                raw.append(text[block_indent:])
            else:
                raw.append("")
            self.i += 1
        while raw and not raw[-1]:
            raw.pop()
        if not raw:
            raise self.fail(line, "an empty block scalar")
        if match.group("style") == "|":
            value = "\n".join(raw)
        else:
            value = "\n".join(" ".join(p.split("\n")) for p in "\n".join(raw).split("\n\n"))
        return value if match.group("chomp") else value + "\n"


def _is_item(content: str) -> bool:
    return content == "-" or content.startswith(("- ", "-\t"))


def _split_flow(text: str) -> tuple[list[str], str] | str | None:
    """A flow list's text after its `[`: (items split on commas outside quotes, the text after `]`).

    An error message for a nested collection; None when the `]` never comes.
    """
    items: list[str] = []
    current: list[str] = []
    quote, escaped = "", False
    for i, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = ""  # a doubled '' closes and reopens: the item text keeps both
        elif char in "'\"":
            quote = char
        elif char == "]":
            items.append("".join(current))
            return items, text[i + 1:]
        elif char in "[{":
            return "nested flow collections are outside the subset"
        elif char == ",":
            items.append("".join(current))
            current = []
            continue
        current.append(char)
    return None


def read_yaml(text: str, path: str) -> Mapping:
    """The subset's document: a top-level mapping. Raises `MapError` naming the line of anything outside it."""
    return _Reader(text, path).document()


# ----------------------------------------------------------------- schema

@dataclass(frozen=True)
class Link:
    to: str
    via: str
    line: int


@dataclass(frozen=True)
class Surface:
    id: str
    area: str
    title: str
    line: int
    root_none: str | None  # None: an element carries the id; else the reason it has no element ("" = no reason)
    reach: tuple[str, ...] = ()
    holds: tuple[str, ...] = ()
    does: tuple[str, ...] = ()
    states: tuple[str, ...] = ()
    why: str = ""
    phone: str = ""
    desktop: str = ""
    links: tuple[Link, ...] = ()

    @property
    def rootless(self) -> bool:
        """A surface with no element of its own, and a reason saying so; a bare `none` does not count."""
        return bool(self.root_none)


@dataclass(frozen=True)
class Area:
    id: str
    title: str
    notes: str
    line: int


@dataclass(frozen=True)
class Walk:
    date: str
    build: str
    widths: tuple[str, ...]
    themes: tuple[str, ...]


@dataclass(frozen=True)
class SurfaceMap:
    app: str
    notes: str
    walk: Walk
    areas: tuple[Area, ...]
    surfaces: tuple[Surface, ...]


_TOP_KEYS = {"app", "notes", "walk", "areas", "surfaces"}
_WALK_KEYS = {"date", "build", "widths", "themes"}
_AREA_KEYS = {"id", "title", "notes"}
_SURFACE_KEYS = {"id", "area", "title", "root", "links", "reach", "holds", "does", "states", "why", "phone",
                 "desktop"}
_LINK_KEYS = {"to", "via"}


class _Schema:
    def __init__(self, path: str) -> None:
        self.path = path

    def fail(self, line: int, message: str) -> MapError:
        return MapError(self.path, line, message)

    def mapping(self, node: Any, line: int, what: str, keys: set[str], required: tuple[str, ...]) -> Mapping:
        if not isinstance(node, Mapping):
            raise self.fail(line, f"{what} must be a mapping")
        unknown = sorted(set(node) - keys)
        if unknown:
            raise self.fail(node.key_lines[unknown[0]], f"{what}: unknown key(s) {', '.join(unknown)}")
        for key in required:
            if node.get(key) is None:
                raise self.fail(node.line, f"{what} needs `{key}`")
        return node

    def text(self, node: Mapping, key: str, what: str) -> str:
        value = node.get(key)
        if value is None:
            return ""
        if not isinstance(value, str):
            raise self.fail(node.key_lines[key], f"{what} `{key}` must be text")
        return value.strip()

    def texts(self, node: Mapping, key: str, what: str) -> tuple[str, ...]:
        value = node.get(key)
        if value is None:
            return ()
        if not isinstance(value, Sequence) or not all(isinstance(v, str) for v in value):
            raise self.fail(node.key_lines[key], f"{what} `{key}` must be a list of text items")
        return tuple(v.strip() for v in value)

    def items(self, node: Mapping, key: str) -> Sequence:
        value = node.get(key)
        if value is None:
            return Sequence(node.key_lines.get(key, node.line))
        if not isinstance(value, Sequence):
            raise self.fail(node.key_lines[key], f"`{key}` must be a list")
        return value

    def root(self, node: Mapping, what: str) -> str | None:
        value, line = node["root"], node.key_lines["root"]
        if value == ROOT_ELEMENT:
            return None
        if isinstance(value, Mapping) and set(value) == {"none"}:
            reason = value["none"]
            if reason is None:
                return ""
            if isinstance(reason, str):
                return reason.strip()
        raise self.fail(line, f"{what} `root` is `{ROOT_ELEMENT}` or `none: <reason>`")

    def surface(self, node: Any, line: int, areas: dict[str, Area]) -> Surface:
        node = self.mapping(node, line, "a surface", _SURFACE_KEYS, ("id", "area", "title", "root"))
        ident = self.text(node, "id", "a surface")
        what = f"surface `{ident}`"
        area = self.text(node, "area", what)
        if area not in areas:
            raise self.fail(node.key_lines["area"], f"{what}: area `{area}` is not one of `areas`")
        links = []
        items = self.items(node, "links")
        for item, item_line in zip(items, items.item_lines, strict=True):
            link = self.mapping(item, item_line, f"a link of {what}", _LINK_KEYS, ("to",))
            links.append(Link(self.text(link, "to", "a link"), self.text(link, "via", "a link"), item_line))
        return Surface(ident, area, self.text(node, "title", what), line, self.root(node, what),
                       reach=self.texts(node, "reach", what), holds=self.texts(node, "holds", what),
                       does=self.texts(node, "does", what), states=self.texts(node, "states", what),
                       why=self.text(node, "why", what), phone=self.text(node, "phone", what),
                       desktop=self.text(node, "desktop", what), links=tuple(links))

    def load(self, top: Mapping) -> SurfaceMap:
        self.mapping(top, 1, "the map", _TOP_KEYS, ("app", "walk", "areas"))
        walk = self.mapping(top["walk"], top.key_lines["walk"], "`walk`", _WALK_KEYS, tuple(sorted(_WALK_KEYS)))
        areas: dict[str, Area] = {}
        area_items = self.items(top, "areas")
        if not area_items:
            raise self.fail(top.key_lines["areas"], "`areas` needs at least one area")
        for item, line in zip(area_items, area_items.item_lines, strict=True):
            node = self.mapping(item, line, "an area", _AREA_KEYS, ("id", "title"))
            area = Area(self.text(node, "id", "an area"), self.text(node, "title", "an area"),
                        self.text(node, "notes", "an area"), line)
            if area.id in areas:
                raise self.fail(line, f"area `{area.id}` is listed twice")
            areas[area.id] = area
        surface_items = self.items(top, "surfaces")
        surfaces = tuple(self.surface(item, line, areas)
                         for item, line in zip(surface_items, surface_items.item_lines, strict=True))
        return SurfaceMap(self.text(top, "app", "the map"), self.text(top, "notes", "the map"),
                          Walk(self.text(walk, "date", "`walk`"), self.text(walk, "build", "`walk`"),
                               self.texts(walk, "widths", "`walk`"), self.texts(walk, "themes", "`walk`")),
                          tuple(areas.values()), surfaces)


def load(text: str, path: str) -> SurfaceMap:
    """The map in `text`. Raises `MapError` (with its line) when it is outside the subset or the schema."""
    return _Schema(path).load(read_yaml(text, path))


def is_exempt(rule: str, lines: list[str], line: int) -> bool:
    """Does a reasoned `<rule>-exempt:` marker sit on `line` or in the `#` comment block right above it?"""
    above = line - 1
    while above > 0 and lines[above - 1].strip().startswith("#"):
        above -= 1
    marker = f"{rule}-exempt"
    return any(text.split(marker, 1)[1].strip(" :#-\t") for text in lines[above:line] if marker in text)


# ----------------------------------------------------------------- render

def _slug(text: str) -> str:
    """GitHub's heading anchor: lowercased, punctuation dropped, spaces to hyphens."""
    return re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")


class _Anchors:
    """Heading anchors in document order, numbered the way GitHub numbers repeats."""

    def __init__(self) -> None:
        self.seen: dict[str, int] = {}

    def add(self, heading: str) -> str:
        base = _slug(heading)
        count = self.seen.get(base, 0)
        self.seen[base] = count + 1
        return base if count == 0 else f"{base}-{count}"


def _label(text: str) -> str:
    return text.replace('"', "#quot;")


def _cell(text: str) -> str:
    """Text safe inside a Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def _surface_heading(s: Surface) -> str:
    return f"`{s.id}` {s.title}"


def _diagram(area: Area, smap: SurfaceMap, node_ids: dict[str, str], area_titles: dict[str, str]) -> list[str]:
    by_id = {s.id: s for s in reversed(smap.surfaces)}  # first entry wins for a duplicated id
    own = [s for s in smap.surfaces if s.area == area.id]
    out = ["```mermaid", "flowchart LR"]
    out += [f'  {node_ids[s.id]}["{_label(s.title)}"]' for s in own]
    stubs: dict[str, str] = {}
    edges: list[str] = []
    for source in smap.surfaces:
        for link in source.links:
            target = by_id.get(link.to)
            inside = (source.area == area.id, target is not None and target.area == area.id)
            if not any(inside):
                continue
            ends = []
            for surface_id, is_inside in ((source.id, inside[0]), (link.to, inside[1])):
                node = node_ids.get(surface_id) or f"missing_{len(stubs)}"
                if not is_inside and surface_id not in stubs:
                    other = by_id.get(surface_id)
                    label = (f"{other.title} · {area_titles[other.area]}" if other is not None
                             else f"{surface_id} (no entry)")
                    stubs[surface_id] = node
                    out.append(f'  {node}(["{_label(label)}"])')
                ends.append(stubs.get(surface_id, node))
            arrow = f'-->|"{_label(link.via)}"|' if link.via else "-->"
            edges.append(f"  {ends[0]} {arrow} {ends[1]}")
    return [*out, *edges, "```"]


def _field(name: str, items: tuple[str, ...]) -> list[str]:
    if not items:
        return []
    if len(items) == 1:
        return [f"- **{name}:** {items[0]}"]
    return [f"- **{name}:**", *(f"  - {item}" for item in items)]


def render(smap: SurfaceMap, source: str) -> str:
    """The page for `smap`, generated from the file at `source` (repo-relative)."""
    anchors = _Anchors()
    anchors.add(f"UX surface map: {smap.app}")
    anchors.add("Areas")
    area_anchor: dict[str, str] = {}
    surface_anchor: dict[str, str] = {}
    for area in smap.areas:
        area_anchor[area.id] = anchors.add(area.title)
        for s in smap.surfaces:
            if s.area == area.id:
                surface_anchor.setdefault(s.id, anchors.add(_surface_heading(s)))
    titles: dict[str, str] = {}
    for s in smap.surfaces:
        titles.setdefault(s.id, s.title)
    node_ids = {s.id: f"s{n}" for n, s in reversed(list(enumerate(smap.surfaces)))}
    area_titles = {a.id: a.title for a in smap.areas}

    def ref(surface_id: str) -> str:
        if surface_id in surface_anchor:
            return f"[{titles[surface_id]}](#{surface_anchor[surface_id]})"
        return f"`{surface_id}` (no entry)"

    walk = smap.walk
    out = [
        f"<!-- Generated from {source} by scripts/ux_surface_map.py. Do not edit: change the map and re-run. -->",
        "", f"# UX surface map: {smap.app}", "",
        "Every surface a person can reach in this app, each under a stable id that its root element carries as "
        "`data-surface`. Each area has a diagram of how its surfaces connect; a rounded node is a surface in "
        "another area.", "",
    ]
    if smap.notes:
        out += [smap.notes, ""]
    out += [f"Walked {walk.date} against build `{walk.build}`, at widths {', '.join(walk.widths)}, "
            f"themes {', '.join(walk.themes)}.", "", "## Areas", "", "| Area | Surfaces |", "|---|---|"]
    out += [f"| [{a.title}](#{area_anchor[a.id]}) | {sum(s.area == a.id for s in smap.surfaces)} |"
            for a in smap.areas]
    for area in smap.areas:
        own = [s for s in smap.surfaces if s.area == area.id]
        out += ["", f"## {area.title}", ""]
        if area.notes:
            out += [area.notes, ""]
        if not own:
            out.append("No surfaces yet.")
            continue
        out += _diagram(area, smap, node_ids, area_titles)
        for s in own:
            root = f"none — {s.root_none}" if s.root_none else ("none" if s.root_none == "" else "`data-surface`")
            out += ["", f"### {_surface_heading(s)}", "", f"- **Root:** {root}"]
            for name, items in (("Reach", s.reach), ("Holds", s.holds), ("Does", s.does), ("States", s.states),
                                ("Phone", (s.phone,) if s.phone else ()),
                                ("Desktop", (s.desktop,) if s.desktop else ()), ("Why", (s.why,) if s.why else ())):
                out += _field(name, items)
            out += _field("Leads to", tuple(f"{ref(k.to)}{f' ({k.via})' if k.via else ''}" for k in s.links))
    out += ["", "## Link index", ""]
    links = [(s, k) for s in smap.surfaces for k in s.links]
    if links:
        out += ["| From | Via | To |", "|---|---|---|"]
        out += [f"| {ref(s.id)} | {_cell(k.via)} | {ref(k.to)} |" for s, k in links]
    else:
        out.append("No links yet.")
    return "\n".join(out) + "\n"


def first_difference(expected: str, actual: str) -> int:
    """The 1-based line where `actual` first departs from `expected` (line endings ignored); 0 when equal."""
    want, have = expected.splitlines(), actual.replace("\r\n", "\n").splitlines()
    if want == have:
        return 0
    for number, (a, b) in enumerate(zip(want, have, strict=False), start=1):
        if a != b:
            return number
    return min(len(want), len(have)) + 1


# -------------------------------------------------------------------- CLI

def _paths(root: Path, map_arg: str | None, page_arg: str | None) -> tuple[str, str]:
    table: dict[str, Any] = {}
    toml = root / ".fleet.toml"
    if toml.is_file():
        found = tomllib.loads(toml.read_text(encoding="utf-8")).get("ux_surface_ratchet")
        table = found if isinstance(found, dict) else {}
    paths = []
    for arg, key, default in ((map_arg, "map", DEFAULT_MAP), (page_arg, "page", DEFAULT_PAGE)):
        value = arg if arg is not None else table.get(key, default)
        if not isinstance(value, str) or not value.strip("/"):
            raise ValueError(f"[ux_surface_ratchet] {key} must be a non-empty string")
        paths.append(value.replace("\\", "/").strip("/"))
    return paths[0], paths[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repo root (default: this repo)")
    parser.add_argument("--map", help=f"the map, repo-relative (default: [ux_surface_ratchet] map, else {DEFAULT_MAP})")
    parser.add_argument("--page", help=f"the page, repo-relative (default: [ux_surface_ratchet] page, else {DEFAULT_PAGE})")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 when the page is not current")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        map_path, page_path = _paths(root, args.map, args.page)
    except (ValueError, tomllib.TOMLDecodeError, OSError) as exc:
        print(f"ux surface map: CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    try:
        text = (root / map_path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ux surface map: FAIL: cannot read {map_path}: {exc}")
        return EXIT_FAIL
    try:
        page = render(load(text, map_path), map_path)
    except MapError as exc:
        print(f"ux surface map: FAIL: {exc}")
        return EXIT_FAIL
    target = root / page_path
    current = target.read_text(encoding="utf-8") if target.is_file() else ""
    line = first_difference(page, current)
    if args.check:
        if line:
            print(f"ux surface map: FAIL: {page_path} is not what {map_path} generates (line {line}) -- "
                  "run `python scripts/ux_surface_map.py`")
            return EXIT_FAIL
        print(f"ux surface map: {page_path} is current")
        return EXIT_OK
    if line:
        # runtime-write-exempt: the folder of the repo's own generated page, named by the config
        target.parent.mkdir(parents=True, exist_ok=True)
        # runtime-write-exempt: the repo's own generated page, named by the config, never a code folder
        target.write_text(page, encoding="utf-8", newline="\n")
        print(f"ux surface map: wrote {page_path} from {map_path}")
    else:
        print(f"ux surface map: {page_path} is current")
    return EXIT_OK


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # real console streams are TextIOWrapper
    sys.exit(main())
