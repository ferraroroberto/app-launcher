"""Tests for src.desktop.prevent_lock on a fake injector, idle probe, keep-awake
hold and clock: no test moves a real pointer or asks Windows to stay awake."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Callable, List, Optional, Sequence, Tuple

import pytest

from src.desktop import input as desktop_input
from src.desktop import prevent_lock as pl
from src.desktop.input import InputEvent, MouseEvent


class World:
    """One clock for the monotonic timer and the tick Windows reports, plus the
    injector that, like Windows, counts its own input as input."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.tick = 100_000
        self.last_input = 0
        self.moves: List[MouseEvent] = []
        self.accept = True

    def clock(self) -> float:
        return self.now

    def probe(self) -> Optional[Tuple[int, int]]:
        return self.last_input, self.tick

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.tick += int(seconds * 1000)

    def real_input(self) -> None:
        self.last_input = self.tick

    def send(self, events: Sequence[InputEvent]) -> int:
        if not self.accept:
            return 0
        for event in events:
            assert isinstance(event, MouseEvent)
            self.moves.append(event)
        self.last_input = self.tick
        return len(events)


class FakeKeepAwake:
    def __init__(self, grant: bool = True) -> None:
        self.grant = grant
        self.held = False
        self.acquired = 0
        self.released = 0

    def acquire(self) -> bool:
        self.acquired += 1
        self.held = self.grant
        return self.grant

    def release(self) -> None:
        self.released += 1
        self.held = False


def make(grant: bool = True) -> Tuple[pl.PreventLock, World, FakeKeepAwake]:
    world = World()
    hold = FakeKeepAwake(grant)
    ctl = pl.PreventLock(
        injector=SimpleNamespace(send=world.send),  # type: ignore[arg-type]
        idle_probe=world.probe,
        keep_awake=hold,
        clock=world.clock,
    )
    return ctl, world, hold


def dys(world: World) -> List[int]:
    return [m.dy for m in world.moves]


def test_off_holds_nothing_and_sends_nothing() -> None:
    ctl, world, hold = make()
    for _ in range(3):
        assert ctl.step(False) == "off"
        world.advance(15)
    assert world.moves == []
    assert hold.acquired == 0 and hold.released == 0


def test_on_nudges_up_then_down_every_interval() -> None:
    ctl, world, hold = make()
    results = []
    for _ in range(4):
        results.append(ctl.step(True))
        world.advance(14.9)
        results.append(ctl.step(True))
        world.advance(0.1)
    assert results == ["moved", "waiting"] * 4
    assert dys(world) == [-1, 1, -1, 1]
    assert all(m.dx == 0 and m.flags == desktop_input.MOUSEEVENTF_MOVE for m in world.moves)
    assert hold.acquired == 1 and hold.held


def test_the_nudge_never_counts_as_real_input() -> None:
    ctl, world, _ = make()
    for _ in range(6):
        assert ctl.step(True) == "moved"
        world.advance(pl.NUDGE_INTERVAL_S)
    assert len(world.moves) == 6


def test_real_input_skips_the_beat_and_keeps_the_direction() -> None:
    ctl, world, _ = make()
    assert ctl.step(True) == "moved"
    world.advance(pl.NUDGE_INTERVAL_S)
    world.advance(-3)
    world.real_input()
    world.advance(3)
    assert ctl.step(True) == "skipped"
    world.advance(pl.NUDGE_INTERVAL_S)
    assert ctl.step(True) == "moved"
    assert dys(world) == [-1, 1]


def test_recent_input_before_the_first_beat_skips_it() -> None:
    ctl, world, _ = make()
    world.last_input = world.tick - 5_000
    assert ctl.step(True) == "skipped"
    assert world.moves == []
    world.advance(pl.NUDGE_INTERVAL_S)
    assert ctl.step(True) == "moved"


def test_idle_clock_wrap_still_reads_recent_input() -> None:
    ctl, world, _ = make()
    world.tick = 500
    world.last_input = 2**32 - 1000
    assert ctl.step(True) == "skipped"


def test_unreadable_idle_clock_still_nudges() -> None:
    ctl, world, _ = make()
    ctl._idle_probe = lambda: None
    assert ctl.step(True) == "moved"


def test_refused_move_retries_without_flipping() -> None:
    ctl, world, _ = make()
    world.accept = False
    assert ctl.step(True) == "failed"
    world.advance(pl.NUDGE_INTERVAL_S)
    assert ctl.step(True) == "failed"
    world.accept = True
    world.advance(pl.NUDGE_INTERVAL_S)
    assert ctl.step(True) == "moved"
    assert dys(world) == [-1]


def test_a_refused_keep_awake_request_does_not_stop_the_nudge() -> None:
    ctl, world, hold = make(grant=False)
    assert ctl.step(True) == "moved"
    assert not hold.held


def test_turning_off_releases_and_turning_on_again_starts_fresh() -> None:
    ctl, world, hold = make()
    ctl.step(True)
    assert hold.held
    assert ctl.step(False) == "off"
    assert not hold.held and hold.released == 1
    # Forgetting the last nudge makes it look like real input for one interval.
    world.advance(pl.NUDGE_INTERVAL_S)
    assert ctl.step(True) == "moved"
    assert hold.acquired == 2
    assert dys(world) == [-1, -1]


def test_shutdown_releases_the_request() -> None:
    ctl, _, hold = make()
    ctl.step(True)
    ctl.shutdown()
    assert hold.released == 1 and not hold.held
    ctl.shutdown()
    assert hold.released == 1


# ---------------------------------------------------------------- the hold


class FakeState:
    """SetThreadExecutionState: records the calls and the thread that made each."""

    def __init__(self, result: int = 0x80000000) -> None:
        self.result = result
        self.calls: List[Tuple[int, int]] = []

    def __call__(self, flags: int) -> int:
        self.calls.append((flags, threading.get_ident()))
        return self.result


def test_hold_sets_the_flags_and_clears_them_from_the_same_thread() -> None:
    state = FakeState()
    hold = pl.ExecutionStateHold(set_state=state)
    assert hold.acquire() is True
    assert hold.acquire() is True
    assert [c[0] for c in state.calls] == [pl.HOLD_FLAGS]
    assert pl.HOLD_FLAGS & pl.ES_SYSTEM_REQUIRED and pl.HOLD_FLAGS & pl.ES_DISPLAY_REQUIRED
    hold.release()
    assert [c[0] for c in state.calls] == [pl.HOLD_FLAGS, pl.ES_CONTINUOUS]
    assert state.calls[0][1] == state.calls[1][1] != threading.get_ident()
    hold.release()
    assert len(state.calls) == 2


def test_hold_reports_a_refusal_and_can_try_again() -> None:
    state = FakeState(result=0)
    hold = pl.ExecutionStateHold(set_state=state)
    assert hold.acquire() is False
    state.result = 1
    assert hold.acquire() is True
    hold.release()


# --------------------------------------------------------------- the loop


async def _until(check: Callable[[], bool]) -> None:
    for _ in range(300):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def test_loop_applies_the_switch_on_a_poke_and_releases_on_cancel() -> None:
    ctl, world, hold = make()
    app = SimpleNamespace(state=SimpleNamespace(webapp_config=SimpleNamespace(prevent_lock=False)))

    async def run() -> None:
        task = asyncio.create_task(pl.tick_forever(app, ctl))
        await asyncio.sleep(0)
        assert hold.acquired == 0 and world.moves == []
        app.state.webapp_config = SimpleNamespace(prevent_lock=True)
        await _until(lambda: pl._wake is not None)
        pl.poke()
        await _until(lambda: hold.held and len(world.moves) == 1)
        app.state.webapp_config = SimpleNamespace(prevent_lock=False)
        pl.poke()
        await _until(lambda: not hold.held)
        app.state.webapp_config = SimpleNamespace(prevent_lock=True)
        pl.poke()
        await _until(lambda: hold.held)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not hold.held
        assert pl._wake is None

    asyncio.run(run())
