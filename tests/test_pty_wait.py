"""Contract tests for the real-PTY polled waits (issue #1108).

The flake these waits fix is load-dependent, so it cannot be pinned directly
by a deterministic test — a cold ConPTY boot is slow only when the box is
busy. What *can* be pinned is everything that made the flake expensive, and
each of these fails against the pre-fix shape:

* the ceiling is generous enough to survive load (pre-fix: an effective 5 s,
  against a 3.1 s idle cost — the margin that ran out);
* an expired wait raises its own type with its own message, instead of
  falling through to the caller's assertion and reporting a timing problem as
  byte loss;
* the wait returns as soon as the condition holds, so the generous ceiling
  costs nothing on the happy path.
"""

from __future__ import annotations

import time

import pytest

from tests import _pty_wait
from tests._pty_wait import (
    DELIVERY_CEILING_S,
    READY_CEILING_S,
    PtyWaitTimeout,
    wait_until,
)

# The measured idle cost of the readiness wait on this box is ~3.1 s (see the
# module docstring). Anything below this leaves the same thin margin that
# #1108 was filed about, so a future edit shrinking the ceiling back toward
# the old 5 s trips here rather than in a gate run three weeks later.
_MIN_DEFENSIBLE_CEILING_S = 15.0


def test_ceilings_keep_real_headroom_over_the_measured_idle_cost() -> None:
    assert READY_CEILING_S >= _MIN_DEFENSIBLE_CEILING_S
    assert DELIVERY_CEILING_S >= _MIN_DEFENSIBLE_CEILING_S


def test_ceilings_stay_under_the_pytest_timeout() -> None:
    """Both waits must fail by name before pytest-timeout's 120 s black box.

    ``pytest.ini`` sets ``timeout = 120``. A readiness wait and a delivery
    wait run back to back in the same test, so it is their *sum* that has to
    fit — otherwise a hang is reported as an opaque timeout with a thread
    dump rather than as the named ceiling that says which wait gave up.
    """
    assert READY_CEILING_S + DELIVERY_CEILING_S < 120


def test_returns_as_soon_as_the_condition_holds() -> None:
    """A generous ceiling must not cost anything once the condition is true."""
    elapsed = wait_until(
        lambda: True, ceiling_s=30, what="already true", env_var="X"
    )
    assert elapsed < 0.1


def test_polls_until_the_condition_becomes_true() -> None:
    started = time.monotonic()
    elapsed = wait_until(
        lambda: time.monotonic() - started >= 0.05,
        ceiling_s=30,
        what="becomes true",
        env_var="X",
    )
    assert 0.05 <= elapsed < 1.0


def test_expiry_raises_its_own_type_naming_the_wait_and_the_override() -> None:
    """The distinct-condition rule: a timeout must never read as byte loss.

    Pre-fix, the delivery wait simply fell out of its loop and let the
    caller's length assertion report "lost bytes at the PTY boundary" — a
    slow child misreported as the exact defect the test exists to catch.
    """
    with pytest.raises(PtyWaitTimeout) as excinfo:
        wait_until(
            lambda: False,
            ceiling_s=0.05,
            what="the child never did the thing",
            env_var="PTY_READY_CEILING_S",
            detail=lambda: "0 of 2048 bytes had landed",
        )
    message = str(excinfo.value)
    assert "the child never did the thing" in message
    assert "0 of 2048 bytes had landed" in message
    assert "PTY_READY_CEILING_S" in message
    # Its own type, so a caller can tell the two conditions apart.
    assert isinstance(excinfo.value, AssertionError)


def test_failure_message_is_ascii_for_the_gate_log() -> None:
    """Non-ASCII arrives as a replacement char under Windows console capture."""
    with pytest.raises(PtyWaitTimeout) as excinfo:
        wait_until(lambda: False, ceiling_s=0.01, what="nope", env_var="X")
    str(excinfo.value).encode("ascii")  # raises UnicodeEncodeError if it drifts


def test_detail_is_only_consulted_on_failure() -> None:
    """It describes state reached, so it must not run on the happy path."""
    calls: list[int] = []
    wait_until(
        lambda: True,
        ceiling_s=30,
        what="fine",
        env_var="X",
        detail=lambda: calls.append(1) or "",
    )
    assert calls == []


def test_a_detail_that_raises_does_not_mask_the_timeout() -> None:
    """The state being described can vanish; the ceiling still gets reported."""
    def exploding_detail() -> str:
        raise OSError("the file went away")

    with pytest.raises(PtyWaitTimeout) as excinfo:
        wait_until(
            lambda: False,
            ceiling_s=0.01,
            what="the child never finished",
            env_var="X",
            detail=exploding_detail,
        )
    assert "the child never finished" in str(excinfo.value)


def test_poll_interval_is_tighter_than_the_fixed_tick_it_replaced() -> None:
    """The happy path must get *faster*, not merely more tolerant (#1108)."""
    assert _pty_wait._POLL_MAX_S < 0.05
