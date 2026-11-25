"""A registered tray counts as ready on its health and build, not on an open port.

``probe_tray`` reports one of four states: answering, stale, bound but not
answering, down. Each test installs fake answers for the HTTP calls and a fake
HEAD, so nothing here reaches a live tray.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import pytest
import requests

import app.tray.registered_trays as rt_mod

HEAD = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


class _Resp:
    def __init__(self, status: int = 200, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def _serve(monkeypatch, answers: dict[str, Callable[[], _Resp]]) -> list[str]:
    """Answer each ``<scheme>://127.0.0.1:<port><path>`` from ``answers`` (keyed
    ``scheme + path``); an unlisted URL refuses the connection."""
    seen: list[str] = []

    def fake(method, url, *, timeout, verify=True, **kwargs):
        seen.append(url)
        key = url.split("://")[0] + "//" + url.split("/", 3)[3]
        if key not in answers:
            raise requests.ConnectionError("refused")
        return answers[key]()

    monkeypatch.setattr(rt_mod, "pooled_request", fake)
    return seen


def _bound(monkeypatch, bound: bool = True) -> None:
    monkeypatch.setattr(rt_mod, "port_listening", lambda port: bound)


def _head(monkeypatch, head: Optional[str]) -> None:
    monkeypatch.setattr(rt_mod, "run_git", lambda repo, args, **kw: head)


def _version(sha: Optional[str]) -> Callable[[], _Resp]:
    return lambda: _Resp(200, {"git_sha": sha} if sha is not None else {})


def test_a_closed_port_is_down(monkeypatch, tmp_path: Path):
    _bound(monkeypatch, False)
    seen = _serve(monkeypatch, {})

    probe = rt_mod.probe_tray(8447, tmp_path)

    assert probe.state == rt_mod.TRAY_DOWN
    assert seen == []


def test_a_bound_port_with_a_failing_healthz_is_bound_not_answering(monkeypatch, tmp_path: Path):
    _bound(monkeypatch)
    _serve(monkeypatch, {
        "https//healthz": lambda: _Resp(503),
        "http//healthz": lambda: _Resp(503),
        "https//api/version": _version(HEAD),
        "http//api/version": _version(HEAD),
    })
    _head(monkeypatch, HEAD)

    probe = rt_mod.probe_tray(8447, tmp_path)

    assert probe.state == rt_mod.TRAY_BOUND_NOT_ANSWERING
    assert "HTTP 503" in probe.detail


def test_a_bound_port_that_answers_nothing_is_bound_not_answering(monkeypatch, tmp_path: Path):
    _bound(monkeypatch)
    _serve(monkeypatch, {})

    probe = rt_mod.probe_tray(8447, tmp_path)

    assert probe.state == rt_mod.TRAY_BOUND_NOT_ANSWERING
    assert "ConnectionError" in probe.detail


def test_a_healthy_tray_without_a_version_is_bound_not_answering(monkeypatch, tmp_path: Path):
    _bound(monkeypatch)
    _serve(monkeypatch, {"https//healthz": lambda: _Resp(200, {"ok": True})})

    probe = rt_mod.probe_tray(8447, tmp_path)

    assert probe.state == rt_mod.TRAY_BOUND_NOT_ANSWERING


def test_a_stale_git_sha_is_reported_stale(monkeypatch, tmp_path: Path):
    _bound(monkeypatch)
    _serve(monkeypatch, {
        "https//healthz": lambda: _Resp(200, {"ok": True}),
        "https//api/version": _version("0000000deadbeef"),
    })
    _head(monkeypatch, HEAD)

    probe = rt_mod.probe_tray(8447, tmp_path)

    assert probe.state == rt_mod.TRAY_STALE
    assert "0000000deadbeef" in probe.detail
    assert HEAD[:12] in probe.detail


@pytest.mark.parametrize("served", [HEAD, HEAD[:7], HEAD[:12]])
def test_a_matching_git_sha_is_answering(monkeypatch, tmp_path: Path, served: str):
    _bound(monkeypatch)
    _serve(monkeypatch, {
        "https//healthz": lambda: _Resp(200, {"ok": True}),
        "https//api/version": _version(served),
    })
    _head(monkeypatch, HEAD)

    assert rt_mod.probe_tray(8447, tmp_path).state == rt_mod.TRAY_ANSWERING


def test_plain_http_is_tried_when_https_does_not_answer(monkeypatch, tmp_path: Path):
    _bound(monkeypatch)
    seen = _serve(monkeypatch, {
        "http//healthz": lambda: _Resp(200, {"ok": True}),
        "http//api/version": _version(HEAD),
    })
    _head(monkeypatch, HEAD)

    probe = rt_mod.probe_tray(8447, tmp_path)

    assert probe.state == rt_mod.TRAY_ANSWERING
    assert probe.detail.startswith("http ")
    assert seen[0].startswith("https://")


def test_a_version_with_no_git_sha_is_answering_and_says_the_build_was_not_compared(
    monkeypatch, tmp_path: Path
):
    _bound(monkeypatch)
    _serve(monkeypatch, {
        "https//healthz": lambda: _Resp(200, {"ok": True}),
        "https//api/version": _version(None),
    })
    _head(monkeypatch, HEAD)

    probe = rt_mod.probe_tray(8447, tmp_path)

    assert probe.state == rt_mod.TRAY_ANSWERING
    assert "no git_sha" in probe.detail


def test_an_unreadable_head_is_not_a_mismatch(monkeypatch, tmp_path: Path):
    _bound(monkeypatch)
    _serve(monkeypatch, {
        "https//healthz": lambda: _Resp(200, {"ok": True}),
        "https//api/version": _version("0000000deadbeef"),
    })
    _head(monkeypatch, None)

    probe = rt_mod.probe_tray(8447, tmp_path)

    assert probe.state == rt_mod.TRAY_ANSWERING
    assert "not compared" in probe.detail


def test_the_wait_ignores_a_bound_port_until_it_answers(monkeypatch, tmp_path: Path):
    (tmp_path / ".fleet.toml").write_text("port = 8447\n", encoding="utf-8")
    states = iter([
        rt_mod.TrayProbe(rt_mod.TRAY_BOUND_NOT_ANSWERING, "wedged"),
        rt_mod.TrayProbe(rt_mod.TRAY_STALE, "old"),
        rt_mod.TrayProbe(rt_mod.TRAY_ANSWERING, "ok"),
    ])
    monkeypatch.setattr(rt_mod, "probe_tray", lambda port, repo_dir=None: next(states))
    monkeypatch.setattr(rt_mod.time, "sleep", lambda s: None)

    assert rt_mod._wait_for_tray_ready(tmp_path) is True
