"""The declared Life OS relay reply states (src/life_os/relay_state.py) and the CLI's copy."""

from __future__ import annotations

import pytest

from scripts import telegram_relay as cli
from src.life_os.relay_state import OUTCOMES, RelayState, steer_state


@pytest.mark.parametrize(
    ("deferred", "confirmed", "state"),
    [
        (False, False, RelayState.UNCONFIRMED),
        (False, True, RelayState.DELIVERED),
        (True, False, RelayState.QUEUED),
    ],
)
def test_steer_state_table(deferred, confirmed, state):
    assert steer_state(deferred, confirmed) is state


def test_a_held_message_is_never_delivered() -> None:
    """A message the host held mid-turn stays ``queued`` even when the submit reads confirmed.

    state-machine-proof: life-os-relay-state
    """
    assert steer_state(deferred=True, confirmed=True) is RelayState.QUEUED


def test_cli_copy_matches_the_launcher() -> None:
    """The CLI's stdlib copy is the launcher's set plus its own ``no_reply_yet``."""
    assert {m.value for m in cli.RelayState} == {m.value for m in RelayState} | {"no_reply_yet"}
    assert {str(s) for s in cli.OUTCOMES} == {str(s) for s in OUTCOMES}


@pytest.mark.parametrize("state", sorted(set(cli.RelayState) - cli.OUTCOMES))
def test_cli_never_exits_0_on_a_refusal_or_unread_reply(state) -> None:
    """A refusal, an unread reply or no reply yet exits 1, never as an outcome.

    state-machine-proof: life-os-relay-state-cli
    """
    assert cli.exit_code(state) == 1
    assert cli.exit_code(str(state)) == 1


@pytest.mark.parametrize("state", sorted(cli.OUTCOMES))
def test_cli_exits_0_on_an_outcome(state) -> None:
    assert cli.exit_code(str(state)) == 0
