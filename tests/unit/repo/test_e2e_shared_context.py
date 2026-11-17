"""The e2e harness's loopback-port discipline: shared contexts and the named port-pool failure.

No browser here: the context objects are stand-ins, so this pins the decisions
(what is shared, what is discarded, what the failure says), while the e2e suite
itself proves the reset against a real engine.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tests.e2e import _shared_context as sc
from tests.e2e._port_pool import (
    PORT_POOL_HEADLINE,
    is_port_pool_failure,
)
from tests.e2e._port_pool import (
    write_summary as port_pool_summary,
)


class _Context:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Browser:
    def __init__(self) -> None:
        self.made: list[tuple[dict, _Context]] = []

    def new_context(self, **args: Any) -> _Context:
        ctx = _Context()
        ctx.init_scripts = []  # type: ignore[attr-defined]
        ctx.add_init_script = ctx.init_scripts.append  # type: ignore[attr-defined]
        self.made.append((args, ctx))
        return ctx


@pytest.fixture(autouse=True)
def _clean_taints() -> None:
    sc._TAINTED.clear()


def test_same_arguments_share_a_context_and_the_seed_is_added_once() -> None:
    browser = _Browser()
    shared = sc.SharedContexts(browser)  # type: ignore[arg-type]
    first, key, created = shared.acquire({"viewport": 1}, "seed")
    again, again_key, created_again = shared.acquire({"viewport": 1}, "seed")
    assert (first is again, key == again_key, created, created_again) == (True, True, True, False)
    assert first.init_scripts == ["seed"]  # type: ignore[attr-defined]


def test_other_arguments_or_no_seed_get_a_context_of_their_own() -> None:
    shared = sc.SharedContexts(_Browser())  # type: ignore[arg-type]
    a, _, _ = shared.acquire({"viewport": 1}, "seed")
    b, _, _ = shared.acquire({"viewport": 2}, "seed")
    c, _, _ = shared.acquire({"viewport": 1}, None)
    assert len({id(a), id(b), id(c)}) == 3


def test_a_tainted_context_is_closed_and_forgotten(monkeypatch: pytest.MonkeyPatch) -> None:
    reset: list[_Context] = []
    monkeypatch.setattr(sc, "reset_context", lambda ctx, origin: reset.append(ctx))
    shared = sc.SharedContexts(_Browser())  # type: ignore[arg-type]
    ctx, key, _ = shared.acquire({}, None)
    shared.begin_test()
    sc._TAINTED.append("Clock.install")
    assert shared.release(ctx, key, "https://x") == "tainted"  # type: ignore[arg-type]
    assert ctx.closed and not reset
    assert shared.acquire({}, None)[0] is not ctx


def test_a_clean_context_is_reset_and_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    reset: list[_Context] = []
    monkeypatch.setattr(sc, "reset_context", lambda ctx, origin: reset.append(ctx))
    shared = sc.SharedContexts(_Browser())  # type: ignore[arg-type]
    ctx, key, _ = shared.acquire({}, None)
    shared.begin_test()
    assert shared.release(ctx, key, "https://x") == "kept"  # type: ignore[arg-type]
    assert reset == [ctx] and not ctx.closed
    assert shared.acquire({}, None)[0] is ctx


def test_a_failed_reset_discards_the_context(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(ctx: Any, origin: str) -> None:
        raise RuntimeError("page crashed")

    monkeypatch.setattr(sc, "reset_context", broken)
    shared = sc.SharedContexts(_Browser())  # type: ignore[arg-type]
    ctx, key, _ = shared.acquire({}, None)
    shared.begin_test()
    assert shared.release(ctx, key, "https://x") == "reset-failed"  # type: ignore[arg-type]
    assert ctx.closed


def test_begin_test_forgets_the_harness_own_setup() -> None:
    sc._TAINTED.append("BrowserContext.on")
    sc.SharedContexts.begin_test()
    assert not sc._TAINTED


def test_taint_guard_marks_state_the_reset_cannot_undo() -> None:
    class Target:
        def install(self) -> str:
            return "ran"

    sc._taint_on_call(Target, "install")
    assert Target().install() == "ran"
    assert sc._TAINTED == ["Target.install"]


def test_taint_guard_covers_the_real_playwright_context_and_clock() -> None:
    sc.install_taint_guard()
    sc.install_taint_guard()  # idempotent: a second call must not double-wrap
    for cls, names in ((sc.BrowserContext, sc._CONTEXT_TAINTS), (sc._Clock, sc._CLOCK_TAINTS)):
        for name in names:
            method = getattr(cls, name)
            assert method.__tainting__
            assert not getattr(method.__wrapped__, "__tainting__", False)


def test_a_port_pool_failure_is_recognised_by_either_marker() -> None:
    assert is_port_pool_failure("Page.goto: net::ERR_ADDRESS_IN_USE at https://127.0.0.1:1/")
    assert is_port_pool_failure("Error: connect ENOBUFS 127.0.0.1:1")
    assert not is_port_pool_failure("Timeout 15000ms exceeded")


def test_the_summary_lists_only_the_tests_the_port_pool_failed() -> None:
    def report(nodeid: str, text: str) -> Any:
        return SimpleNamespace(nodeid=nodeid, longreprtext=text)

    lines: list[str] = []
    reporter = SimpleNamespace(
        stats={
            "failed": [
                report("a::drained", f"{PORT_POOL_HEADLINE}\n\nERR_ADDRESS_IN_USE"),
                report("a::broken", "AssertionError"),
            ],
            "error": [report("a::setup", f"{PORT_POOL_HEADLINE}\n\nconnect ENOBUFS")],
        },
        write_sep=lambda *a, **k: lines.append("SECTION " + a[1]),
        write_line=lines.append,
    )
    port_pool_summary(reporter)
    assert lines[0] == "SECTION PORT POOL EXHAUSTED"
    assert "  a::drained" in lines and "  a::setup" in lines and "  a::broken" not in lines


def test_the_summary_is_silent_when_no_test_hit_the_port_pool() -> None:
    lines: list[str] = []
    reporter = SimpleNamespace(
        stats={"failed": [SimpleNamespace(nodeid="a", longreprtext="boom")]},
        write_sep=lambda *a, **k: lines.append("x"),
        write_line=lines.append,
    )
    port_pool_summary(reporter)
    assert lines == []
