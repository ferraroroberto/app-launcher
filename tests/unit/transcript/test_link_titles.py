"""Titles for a session's links: found server-side, cached, bounded, and never
fatal. Every URL and title is synthetic; no test reaches a network or ``gh``.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List

import pytest

from src.session_view import link_titles as lt


@pytest.fixture(autouse=True)
def _fresh_cache():
    lt.reset_title_cache()
    yield
    lt.reset_title_cache()


def _row(url: str, label: str, title: Any = None) -> Dict[str, Any]:
    return {"url": url, "label": label, "title": title, "first_seen": None, "last_seen": None, "count": 1}


def _fold() -> Dict[str, Any]:
    return {"total": 5, "partial": False, "groups": [
        {"key": "artifacts", "title": "Artifacts", "total": 2, "links": [
            _row("https://claude.ai/artifact/AbCdEfGh123456", "Artifact AbCdEfGh", "Kept Title"),
            _row("https://claude.ai/artifact/ZzZzZzZz987654", "Artifact ZzZzZzZz")]},
        {"key": "github", "title": "GitHub", "total": 1, "links": [
            _row("https://github.com/acme/widgets/pull/12", "acme/widgets#12")]},
        {"key": "servers", "title": "Servers & previews", "total": 1, "links": [
            _row("http://localhost:3000/a", "localhost:3000")]},
        {"key": "other", "title": "Other", "total": 1, "links": [
            _row("https://example.org/guide", "example.org/guide")]},
    ]}


def _titles(result: Dict[str, Any]) -> Dict[str, Any]:
    return {x["label"]: x["title"] for g in result["groups"] for x in g["links"]}


def test_titles_are_found_and_composed_per_group():
    asked: List[str] = []
    pages = {"https://claude.ai/artifact/ZzZzZzZz987654": "Roadmap",
             "http://localhost:3000/a": "Dev Preview",
             "https://example.org/guide": "The Guide"}

    def web(url: str):
        asked.append(url)
        return pages.get(url)

    result = lt.attach_titles(_fold(), web=web, gh_item=lambda url: "Fix the thing")
    assert _titles(result) == {
        "Artifact AbCdEfGh": "Kept Title",
        "Artifact ZzZzZzZz": "Roadmap",
        "acme/widgets#12": "acme/widgets#12 · Fix the thing",
        "localhost:3000": "localhost:3000 · Dev Preview",
        "example.org/guide": "The Guide",
    }
    assert "https://claude.ai/artifact/AbCdEfGh123456" not in asked, "a title the session gave was looked up"


def test_a_failing_lookup_falls_back_to_the_label():
    def boom(url: str):
        raise RuntimeError("down")

    result = lt.attach_titles(_fold(), web=boom, gh_item=lambda url: None)
    assert _titles(result) == {
        "Artifact AbCdEfGh": "Kept Title", "Artifact ZzZzZzZz": None, "acme/widgets#12": None,
        "localhost:3000": None, "example.org/guide": None}


def test_a_lookup_past_the_deadline_is_not_waited_for_and_lands_in_the_cache():
    release = threading.Event()

    def slow(url: str):
        release.wait(5)
        return "Late"

    first = lt.attach_titles(_fold(), web=slow, gh_item=lambda url: None, deadline=0.05)
    assert _titles(first)["example.org/guide"] is None
    release.set()
    url = "https://example.org/guide"
    for _ in range(250):  # the cache write follows the fetch's return
        if lt._cached(url, time.monotonic()) == (True, "Late"):
            break
        threading.Event().wait(0.02)
    again = lt.attach_titles(_fold(), web=lambda u: None, gh_item=lambda u: None)
    assert _titles(again)["example.org/guide"] == "Late"


def test_titles_are_cached_per_url_and_a_miss_is_remembered():
    calls: List[str] = []

    def web(url: str):
        calls.append(url)
        return "Page" if url.endswith("/guide") else None

    lt.attach_titles(_fold(), web=web, gh_item=lambda url: None)
    first = len(calls)
    again = lt.attach_titles(_fold(), web=web, gh_item=lambda url: None)
    assert len(calls) == first, "the second open fetched again"
    assert _titles(again)["example.org/guide"] == "Page"


def test_lookups_per_request_are_capped(monkeypatch):
    monkeypatch.setattr(lt, "MAX_LOOKUPS", 3)
    rows = [_row("https://example.org/p%d" % i, "p%d" % i) for i in range(8)]
    calls: List[str] = []
    lt.attach_titles({"groups": [{"key": "other", "title": "Other", "total": 8, "links": rows}]},
                     web=lambda url: calls.append(url) or "T", gh_item=lambda url: None)
    assert len(calls) == 3


class _Resp:
    def __init__(self, body: bytes, status: int = 200, ctype: str = "text/html"):
        self._body = body
        self.status_code = status
        self.headers = {"content-type": ctype}
        self.encoding = "utf-8"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, _n):
        yield self._body


@pytest.mark.parametrize("resp, expected", [
    (_Resp(b"<html><head><title>\n  Report &amp; Plan \n</title></head>"), "Report & Plan"),
    (_Resp(b"<title>Claude</title>"), None),
    (_Resp(b"no title here"), None),
    (_Resp(b"<title>Gone</title>", status=404), None),
    (_Resp(b"<title>Data</title>", ctype="application/json"), None),
])
def test_page_title_reads_a_title_and_refuses_the_rest(monkeypatch, resp, expected):
    monkeypatch.setattr(lt.requests, "get", lambda *a, **k: resp)
    assert lt.page_title("https://example.org/") == expected


def test_page_title_gives_up_quietly_on_a_network_error(monkeypatch):
    def refuse(*a, **k):
        raise lt.requests.ConnectionError("refused")

    monkeypatch.setattr(lt.requests, "get", refuse)
    assert lt.page_title("http://localhost:9/") is None


def test_github_title_asks_gh_for_the_item_and_survives_its_failure(monkeypatch):
    seen: List[Any] = []
    monkeypatch.setattr(lt.github, "issue_title", lambda o, r, n, **k: seen.append((o, r, n)) or " The  PR ")
    assert lt.github_title("https://github.com/acme/widgets/pull/12") == "The PR"
    assert seen == [("acme", "widgets", 12)]
    assert lt.github_title("https://github.com/acme/widgets") is None

    def fail(*a, **k):
        raise lt.github.GhError("no auth")

    monkeypatch.setattr(lt.github, "issue_title", fail)
    assert lt.github_title("https://github.com/acme/widgets/issues/3") is None
