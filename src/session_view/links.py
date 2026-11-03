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
sub-agent's traffic. The one exception is a published artifact: a URL the
Claude Artifact tool returned is listed even when the reply never repeats
it, labelled with the title the call was given.

Safety: a URL is never listed with anything credential-shaped. User-info
(``user:pass@``) drops the whole URL; a query holding a key-like parameter
name or a long opaque value is shown without its query string (and so
dedupes with the bare URL).

Bounded work per request: at most the newest :data:`SCAN_CEILING` bytes of
the file, lines with neither ``http`` nor an Artifact call skipped before they are parsed, and at
most :data:`GROUP_LIMIT` links listed per group.
"""

from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from src.session_view.transcript import FLAVORS
from src.transcript_flavors._shared import Line

SCAN_CEILING = 64 * 1024 * 1024
GROUP_LIMIT = 100
LABEL_CHARS = 120
URL_CHARS = 500

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


def _lines(path: Path) -> Iterator[bytes]:
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > SCAN_CEILING:
            handle.seek(size - SCAN_CEILING)
            handle.readline()
        for line in handle:
            if (b"http" in line or b"Artifact" in line) and not any(s in line for s in _SKIP_TYPES):
                yield line


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


def _artifact_title(inputs: Dict[str, Any]) -> str:
    """What the owner would call a published artifact: its title, else the
    page file's name."""
    title = inputs.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()[:LABEL_CHARS]
    file_path = inputs.get("file_path")
    if isinstance(file_path, str) and file_path:
        return re.split(r"[\\/]", file_path)[-1][:LABEL_CHARS]
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


def _artifact_pieces(lines: List[Line]) -> List[_Piece]:
    """What the Artifact tool returned: its result text, paired with the
    title the call was given."""
    titles: Dict[str, str] = {}
    pieces: List[_Piece] = []
    for index, text in lines:
        if "Artifact" not in text and not (titles and "tool_use_id" in text):
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
                    titles[str(block.get("id"))] = _artifact_title(block["input"])
            elif block.get("type") == "tool_result" and str(block.get("tool_use_id")) in titles:
                body = " ".join(_strings(block.get("content")))
                pieces.append((index, stamp, body, titles[str(block.get("tool_use_id"))], True))
    return pieces


def fold_links(path: Path, flavor: str = "claude") -> Dict[str, Any]:
    """``{groups: [{key, title, links: [{url, label, title, first_seen,
    last_seen, count}]}], total, partial}``; links newest first within a group.
    A link's ``title`` is what the transcript itself names (a published
    artifact's title) or ``None`` for :mod:`src.session_view.link_titles` to
    look up.
    ``flavor`` picks the agent's transcript grammar (a key of
    :data:`FLAVORS`; unknown falls back to Claude's, like the Chat pane)."""
    size = path.stat().st_size
    lines: List[Line] = [
        (index, raw.decode("utf-8", errors="replace")) for index, raw in enumerate(_lines(path))
    ]
    pieces = _spoken_pieces(lines, flavor) + _artifact_pieces(lines)
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
        "partial": size > SCAN_CEILING,
    }
