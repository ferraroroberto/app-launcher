"""Real titles for the links a session mentioned.

An artifact the session never published, a web page, a pull request and a
local server carry only a URL in the transcript, so the Links sheet would show
"Artifact F8PkhsyA" and a bare address. Each is looked up here, server-side:
a GitHub pull request or issue through ``gh`` (so a private repo answers too),
anything else by reading the page's ``<title>``.

Bounded and never fatal: a lookup gives up after :data:`FETCH_TIMEOUT`, the
whole request waits at most :data:`REQUEST_DEADLINE` (a lookup still running
lands in the cache for the next open), at most :data:`MAX_LOOKUPS` run per
request, and any failure leaves the link with the label it had. Only a URL the
fold already cleaned is ever requested, so nothing credential-shaped leaves.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import requests

from src.clients import github
from src.session_view.links import TITLE_TAG, clean_title

logger = logging.getLogger(__name__)

FETCH_TIMEOUT = 3.0
REQUEST_DEADLINE = 4.0
MAX_LOOKUPS = 40
READ_BYTES = 64 * 1024
CACHE_LIMIT = 512
# A title that was found stays for hours; a miss is retried within minutes.
HIT_TTL = 6 * 3600
MISS_TTL = 5 * 60

_GH_ITEM = re.compile(r"^/([\w.-]+)/([\w.-]+)/(?:pull|issues)/(\d+)")
# What a sign-in wall or the artifact hosting shell answers for a page the
# server cannot read: not a title.
_WALL_TITLES = frozenset({"claude", "claude artifact", "sign in", "log in", "login", "just a moment..."})

_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="link-title")
_cache: Dict[str, Tuple[Optional[str], float]] = {}
_lock = threading.Lock()

Fetcher = Callable[[str], Optional[str]]


def page_title(url: str) -> Optional[str]:
    """The ``<title>`` of an HTML page, or ``None`` when it has none or the
    server did not answer in time."""
    try:
        with requests.get(
            url, timeout=FETCH_TIMEOUT, stream=True, allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; link-titles)", "Accept": "text/html"},
        ) as resp:
            if resp.status_code >= 400 or "html" not in resp.headers.get("content-type", "").lower():
                return None
            head = next(resp.iter_content(READ_BYTES), b"")
            found = TITLE_TAG.search(head.decode(resp.encoding or "utf-8", errors="replace"))
    except (requests.RequestException, ValueError):
        return None
    title = clean_title(found.group(1)) if found else ""
    return title if title and title.lower() not in _WALL_TITLES else None


def github_title(url: str) -> Optional[str]:
    """The title of a GitHub pull request or issue URL, ``None`` when ``gh``
    did not answer (or the URL is neither)."""
    path = urlsplit(url).path
    found = _GH_ITEM.match(path)
    if not found:
        return None
    try:
        title = github.issue_title(found.group(1), found.group(2), int(found.group(3)), timeout=FETCH_TIMEOUT)
    except github.GhError:
        return None
    return clean_title(title) or None


def _compose(group: str, label: str, title: str) -> str:
    """Pull requests and servers keep the identifier the owner recognises in
    front of the page's own title; artifacts and pages show the title alone."""
    if group in ("github", "servers"):
        return "%s · %s" % (label, title)
    return title


def _cached(url: str, now: float) -> Tuple[bool, Optional[str]]:
    with _lock:
        hit = _cache.get(url)
    if hit is None or hit[1] < now:
        return False, None
    return True, hit[0]


def _remember(url: str, title: Optional[str]) -> None:
    ttl = HIT_TTL if title else MISS_TTL
    with _lock:
        if len(_cache) >= CACHE_LIMIT and url not in _cache:
            del _cache[next(iter(_cache))]
        _cache[url] = (title, time.monotonic() + ttl)


def _run(url: str, fetch: Fetcher) -> Optional[str]:
    try:
        title = fetch(url)
    except Exception as exc:  # a lookup never takes the request down
        logger.info("⚠️ link title lookup failed (%s): %s", type(exc).__name__, exc)
        title = None
    _remember(url, title)
    return title


def reset_title_cache() -> None:
    with _lock:
        _cache.clear()


def attach_titles(
    result: Dict[str, Any],
    *,
    web: Optional[Fetcher] = None,
    gh_item: Optional[Fetcher] = None,
    deadline: float = REQUEST_DEADLINE,
) -> Dict[str, Any]:
    """Fill ``title`` on every link of a fold that has none and whose page
    answers; ``result`` is returned (and changed in place). ``web`` and
    ``gh_item`` are the two lookups, injectable so a test never reaches a
    network."""
    web = web or page_title
    gh_item = gh_item or github_title
    now = time.monotonic()
    wanted: List[Tuple[Dict[str, Any], str]] = []
    for grp in result.get("groups", []):
        for item in grp["links"]:
            if item.get("title"):
                continue
            done, title = _cached(item["url"], now)
            if done:
                if title:
                    item["title"] = _compose(grp["key"], item["label"], title)
                continue
            wanted.append((item, grp["key"]))
    pending: Dict[str, Any] = {}
    for item, key in wanted[:MAX_LOOKUPS]:
        if item["url"] not in pending:
            is_gh = key == "github" and _GH_ITEM.match(urlsplit(item["url"]).path)
            pending[item["url"]] = _pool.submit(_run, item["url"], gh_item if is_gh else web)
    done_set, late = wait(list(pending.values()), timeout=deadline)
    for item, key in wanted:
        future = pending.get(item["url"])
        if future is not None and future in done_set and future.result():
            item["title"] = _compose(key, item["label"], future.result())
    logger.info("ℹ️ link titles: %d looked up, %d late, %d over the cap, %d total",
                len(done_set), len(late), max(0, len(wanted) - MAX_LOOKUPS), len(wanted))
    return result
