"""Every link a session mentioned, folded from its transcript.

The session's ⋮ → *Links* lists the artifacts, pull requests, preview
servers and other URLs the agent (or the owner) put in the conversation, so
one mentioned an hour ago is one tap away instead of a scroll through Chat.
Like Changed files it reads the transcript, so it still answers after the
session ends.

Only what was said counts: the owner's prompts and the agent's own reply
text, read through the same per-agent entry builders the Chat pane renders
(so Claude, Codex, Grok, Pi, Antigravity and Copilot alike). A URL inside a
tool call, a tool result, a file read, command output or thinking is noise
(a long session holds hundreds) and is never listed, and neither is a
sub-agent's traffic. The one exception is a published artifact: the URL of
a successful Claude Artifact publish is listed even when the reply never
repeats it, labelled with the title the call was given. The tool's other
actions (quickstart, list, read, open) and a refused publish name artifacts
the session never published, so they add no row.

Safety: a URL is never listed with anything credential-shaped. User-info
(``user:pass@``) drops the whole URL; a query holding a key-like parameter
name or a long opaque value is shown without its query string (and so
dedupes with the bare URL).

Bounded work per request: one window of at most :data:`SCAN_CEILING` bytes
(the newest, or the one ending at the ``before`` offset a "Read more" passes
back), lines with neither ``http`` nor an Artifact call skipped before they are
parsed, and at most :data:`GROUP_LIMIT` links listed per group.

An artifact's name comes from the publish call's ``title``, else the ``<title>``
in the first :data:`HEAD_BYTES` of the page file when it is still on disk, else
the file's name. A URL the session only quoted takes its name from the
session's own ``list`` result of the owner's artifacts when that named it,
else it keeps the short id.
"""

from __future__ import annotations

import html
import ipaddress
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from src.session_view.transcript import FLAVORS
from src.transcript_flavors._shared import Line

logger = logging.getLogger(__name__)

SCAN_CEILING = 64 * 1024 * 1024
GROUP_LIMIT = 100
LABEL_CHARS = 120
URL_CHARS = 500
# Only the head of a page file is read for its <title>, never the whole file.
HEAD_BYTES = 8 * 1024

# Lines that carry the whole system prompt and no link the session made.
_SKIP_TYPES = (b"prompt_snapshot", b"api-request-shape")
_URL = re.compile(r"https?://[^\s<>\"'`\\\]\[{}|^]+", re.I)
_TRAILING = ".,;:!?)*_~"
# A parameter name that carries a secret, or any value that looks like one.
_SECRET_NAME = re.compile(
    r"key|token|secret|sig|signature|pass|pwd|auth|credential|session|jwt|bearer|code|otp|hmac",
    re.I,
)
_OPAQUE_VALUE = re.compile(r"^[A-Za-z0-9_\-+/=.]{24,}$")
_PR_PATH = re.compile(r"^/([^/]+)/([^/]+)/(pull|issues)/(\d+)")
_TAILNET_SUFFIX = ".ts.net"
TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.I | re.S)
# A line of the Artifact tool's ``list`` result: "- (kind) Title — URL — updated 2026-10-08".
_LISTED = re.compile(r"^\s*-\s*(?:\([^)]*\)\s*)?(.+?)\s+—\s+(https://claude\.ai/\S+?)\s+—", re.M)
_STAMP = re.compile(rb'"timestamp"\s*:\s*"([^"]+)"')
# Lines inspected for the window's oldest timestamp (a line may carry none).
_STAMP_PROBE_LINES = 200

GROUPS: Tuple[Tuple[str, str], ...] = (
    ("artifacts", "Artifacts"),
    ("github", "GitHub"),
    ("servers", "Servers & previews"),
    ("other", "Other"),
)


def _host_is_local(host: str) -> bool:
    if host in ("localhost", "0.0.0.0") or host.endswith(".localhost") or host.endswith(_TAILNET_SUFFIX):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    # Loopback, LAN and the tailnet's 100.64.0.0/10.
    return addr.is_loopback or addr.is_private or addr in ipaddress.ip_network("100.64.0.0/10")


def _sanitize(raw: str) -> Optional[Tuple[str, str, str, int]]:
    """``(url, host, path, port)`` ready to list, or ``None`` for a string
    that is not a listable link. ``port`` is 0 when the URL names none."""
    url = raw.rstrip(_TRAILING)
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port or 0
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    # "http://foo" in a code sample is not a link; keep real hostnames and
    # the local forms that have no dot.
    if "." not in host and not _host_is_local(host) and ":" not in host:
        return None
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        if not pairs or any(
            _SECRET_NAME.search(name) or _OPAQUE_VALUE.match(value) for name, value in pairs
        ):
            query = ""
    path = parts.path.rstrip("/") or ""
    clean = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))
    if len(clean) > URL_CHARS:
        return None
    return clean, host, path, port


def _classify(host: str, path: str, port: int) -> Tuple[str, str]:
    """``(group key, label)`` for a sanitized URL."""
    if host == "claude.ai" and re.match(r"^/(code/)?artifact/", path):
        return "artifacts", "Artifact " + path.rsplit("/", 1)[-1][:8]
    if host == "github.com":
        found = _PR_PATH.match(path)
        if found:
            return "github", "%s/%s#%s" % (found.group(1), found.group(2), found.group(4))
        return "github", (host + path)[:LABEL_CHARS]
    if _host_is_local(host):
        shown = "[%s]" % host if ":" in host else host
        return "servers", "%s:%d" % (shown, port) if port else shown
    return "other", (host + path)[:LABEL_CHARS]


def clean_title(text: str) -> str:
    """A page title as one tidy line, cut to the label length."""
    return re.sub(r"\s+", " ", html.unescape(text)).strip()[:LABEL_CHARS]


class _Window:
    """One scan window of a transcript: the lines worth parsing, the byte
    offset it starts at, and the timestamp of its oldest line."""

    def __init__(self) -> None:
        self.lines: List[bytes] = []
        self.offset = 0
        self.read_from: Optional[str] = None


def _read_window(path: Path, before: Optional[int]) -> _Window:
    """The at most :data:`SCAN_CEILING` bytes that end at ``before`` (the end
    of the file when ``None``), starting on a line boundary."""
    size = path.stat().st_size
    end = size if before is None else max(0, min(before, size))
    start = max(0, end - SCAN_CEILING)
    window = _Window()
    with path.open("rb") as handle:
        if start:
            # One byte early, so a window that starts exactly on a line keeps it.
            handle.seek(start - 1)
            handle.readline()
            window.offset = handle.tell()
        probed = 0
        while handle.tell() < end:
            line = handle.readline()
            if not line:
                break
            if window.read_from is None and probed < _STAMP_PROBE_LINES:
                probed += 1
                found = _STAMP.search(line)
                if found:
                    window.read_from = found.group(1).decode("utf-8", errors="replace")
            if (b"http" in line or b"Artifact" in line) and not any(s in line for s in _SKIP_TYPES):
                window.lines.append(line)
    return window


def _strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)
    elif isinstance(node, dict):
        for item in node.values():
            yield from _strings(item)


def _blocks(row: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                yield block


def _page_file_title(file_path: str) -> str:
    """The ``<title>`` of a published page file still on disk ('' when it is
    gone, is not HTML or names none). Only the head is read."""
    page = Path(file_path)
    if page.suffix.lower() not in (".html", ".htm") or not page.is_absolute():
        return ""
    try:
        with page.open("rb") as handle:
            head = handle.read(HEAD_BYTES)
    except OSError:
        return ""
    found = TITLE_TAG.search(head.decode("utf-8", errors="replace"))
    return clean_title(found.group(1)) if found else ""


def _artifact_title(inputs: Dict[str, Any], page_titles: Dict[str, str]) -> str:
    """What the owner would call a published artifact: the title the call was
    given, else the page's own ``<title>``, else the page file's name.
    ``page_titles`` caches one fold's disk reads."""
    title = inputs.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()[:LABEL_CHARS]
    file_path = inputs.get("file_path")
    if isinstance(file_path, str) and file_path:
        if file_path not in page_titles:
            page_titles[file_path] = _page_file_title(file_path)
        return page_titles[file_path] or re.split(r"[\\/]", file_path)[-1][:LABEL_CHARS]
    return ""


# One piece of scannable text: (line index, timestamp, text, artifact title,
# artifact_only). ``artifact_only`` text is a tool result, of which only the
# Artifact tool's own URL counts.
_Piece = Tuple[int, Optional[str], str, str, bool]


def _spoken_pieces(lines: List[Line], flavor: str) -> List[_Piece]:
    """The owner's prompts and the agent's reply text, in file order."""
    build = FLAVORS.get(flavor, FLAVORS["claude"])[0]
    pieces: List[_Piece] = []
    for entry in build(lines, uncapped=True):
        if entry.get("kind") not in ("user", "assistant") or entry.get("sidechain"):
            continue
        text = entry.get("text")
        if isinstance(text, str) and "http" in text:
            stamp = entry.get("timestamp")
            pieces.append((int(entry["offset"]), stamp if isinstance(stamp, str) else None,
                           text, "", False))
    return pieces


# Artifact actions that publish or update the session's own page. quickstart,
# list, read and open return other artifacts' URLs (design systems, types,
# someone else's page) that the session never published.
_PUBLISH_ACTIONS = ("", "publish")


def _published_url(call_url: Optional[str], body: str) -> Optional[str]:
    """The one URL a successful publish stands for: the page it updated,
    else the first claude.ai artifact URL its result names."""
    if call_url:
        return call_url
    for match in _URL.finditer(body):
        clean = _sanitize(match.group(0))
        if clean and _classify(clean[1], clean[2], clean[3])[0] == "artifacts":
            return clean[0]
    return None


def _listed_titles(body: str) -> Dict[str, str]:
    """URL -> title for each line of an Artifact ``list`` result."""
    found: Dict[str, str] = {}
    for match in _LISTED.finditer(body):
        clean = _sanitize(match.group(2))
        title = clean_title(match.group(1))
        if clean and title:
            found.setdefault(clean[0], title)
    return found


def _artifact_pieces(lines: List[Line]) -> Tuple[List[_Piece], Dict[str, str], Dict[str, str]]:
    """The URL of each successful Artifact publish (paired with the title the
    call was given), the earliest name the session gave each artifact URL
    (for a URL a later reply or a title-less republish repeats) and the titles
    a ``list`` result gave. A refused publish and any other action
    (quickstart, list, read, open) contribute no row."""
    calls: Dict[str, Tuple[str, str, Optional[str]]] = {}
    names: Dict[str, str] = {}
    listed: Dict[str, str] = {}
    page_titles: Dict[str, str] = {}
    pieces: List[_Piece] = []
    for index, text in lines:
        if "Artifact" not in text and not (calls and "tool_use_id" in text):
            continue
        try:
            row = json.loads(text)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        stamp = row.get("timestamp") if isinstance(row.get("timestamp"), str) else None
        for block in _blocks(row):
            if block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                if str(block.get("name") or "").split("__")[-1] == "Artifact":
                    inputs = block["input"]
                    action = str(inputs.get("action") or "")
                    target = inputs.get("url")
                    clean = _sanitize(target) if isinstance(target, str) else None
                    title = _artifact_title(inputs, page_titles)
                    calls[str(block.get("id"))] = (action, title, clean[0] if clean else None)
                    if title and clean and action in _PUBLISH_ACTIONS:
                        names.setdefault(clean[0], title)
            elif block.get("type") == "tool_result" and str(block.get("tool_use_id")) in calls:
                action, title, call_url = calls[str(block.get("tool_use_id"))]
                if action == "list" and not block.get("is_error"):
                    for url, listed_title in _listed_titles(" ".join(_strings(block.get("content")))).items():
                        listed.setdefault(url, listed_title)
                if action not in _PUBLISH_ACTIONS:
                    logger.info("ℹ️ artifact link dropped: action %r publishes nothing", action)
                    continue
                if block.get("is_error"):
                    logger.info("ℹ️ artifact link dropped: the publish failed")
                    continue
                url = _published_url(call_url, " ".join(_strings(block.get("content"))))
                if url is None:
                    logger.info("ℹ️ artifact link dropped: the publish named no artifact URL")
                    continue
                pieces.append((index, stamp, url, title, True))
                if title:
                    names.setdefault(url, title)
    return pieces, names, listed


def fold_links(path: Path, flavor: str = "claude", before: Optional[int] = None) -> Dict[str, Any]:
    """``{groups: [{key, title, links: [{url, label, title, first_seen,
    last_seen, count}]}], total, partial, offset, read_from}``; links newest
    first within a group. ``offset`` is where the scanned window starts (pass
    it back as ``before`` for the next older window), ``read_from`` the
    timestamp of its oldest line, and ``partial`` is true while older
    transcript remains unread.
    A link's ``title`` is what the transcript itself names (a published
    artifact's title) or ``None`` for :mod:`src.session_view.link_titles` to
    look up.
    ``flavor`` picks the agent's transcript grammar (a key of
    :data:`FLAVORS`; unknown falls back to Claude's, like the Chat pane)."""
    window = _read_window(path, before)
    lines: List[Line] = [
        (index, raw.decode("utf-8", errors="replace")) for index, raw in enumerate(window.lines)
    ]
    artifact_pieces, names, listed = _artifact_pieces(lines)
    pieces = _spoken_pieces(lines, flavor) + artifact_pieces
    pieces.sort(key=lambda piece: piece[0])
    found: Dict[str, Dict[str, Any]] = {}
    seq = 0
    for _index, stamp, text, label_hint, artifact_only in pieces:
        for match in _URL.finditer(text):
            clean = _sanitize(match.group(0))
            if clean is None:
                continue
            url, host, url_path, port = clean
            group, label = _classify(host, url_path, port)
            if artifact_only and group != "artifacts":
                continue
            seq += 1
            entry = found.get(url)
            if entry is None:
                entry = found[url] = {
                    "url": url, "group": group, "label": label, "title": None,
                    "first_seen": stamp, "count": 0,
                }
            if label_hint and entry["group"] == "artifacts":
                entry["label"] = entry["title"] = label_hint
            entry["last_seen"] = stamp
            entry["seq"] = seq
            entry["count"] += 1
    sources = {"call title": 0, "transcript": 0, "list result": 0, "short id": 0}
    for entry in found.values():
        if entry["group"] != "artifacts":
            continue
        if entry["title"]:
            sources["call title"] += 1
        elif entry["url"] in names:
            entry["label"] = entry["title"] = names[entry["url"]]
            sources["transcript"] += 1
        elif entry["url"] in listed:
            entry["label"] = entry["title"] = listed[entry["url"]]
            sources["list result"] += 1
        else:
            sources["short id"] += 1
    if any(sources.values()):
        logger.info("ℹ️ artifact row names: %d from the call, %d from earlier in the transcript, "
                    "%d from a list result, %d by short id", sources["call title"], sources["transcript"],
                    sources["list result"], sources["short id"])
    groups: List[Dict[str, Any]] = []
    for key, title in GROUPS:
        members = sorted((e for e in found.values() if e["group"] == key), key=lambda e: -e["seq"])
        if not members:
            continue
        groups.append({
            "key": key, "title": title, "total": len(members),
            "links": [
                {k: e[k] for k in ("url", "label", "title", "first_seen", "last_seen", "count")}
                for e in members[:GROUP_LIMIT]
            ],
        })
    return {
        "groups": groups, "total": len(found),
        "partial": window.offset > 0, "offset": window.offset, "read_from": window.read_from,
    }
