# Life OS relay reply states

A relay-enabled Telegram bot steers its skill's conversations through the launcher's relay routes (`app/webapp/routers/life_os_relay.py`), called by `scripts/telegram_relay.py`. Every reply, success or refusal, names a `state`, so the bot can say what happened and never folds a failure into "ok". The states are declared once, in `src/life_os/relay_state.py`, as the `life-os-relay-state` state machine in `.fleet.toml`, so the conventions ratchet's state rules report any other module that spells them as raw strings. The idle close (`app/webapp/relay_idle_close.py`) reads the stop outcomes from the same set.

## States

| Kind | States | CLI exit |
| --- | --- | --- |
| Outcome | `found`, `none`, `resumed`, `opened`, `started`, `already_open`, `delivered`, `queued`, `unconfirmed`, `reply`, `stopped`, `open` | 0 |
| Not an answer | `unavailable` (the reply could not be read; `reason` says why) | 1 |
| Refusal | `refused`, `relay_off`, `not_found`, `not_this_skill`, `not_resumable`, `host_unreachable`, `stranded`, `gone`, `unknown`, `not_relay_owned`, `busy`, `capture_unconfirmed`, `stop_unconfirmed` | 1 |
| CLI only | `no_reply_yet` (`ask` sent once, no finished reply within the wait) | 1 |

`OUTCOMES` is the exit-0 set. What each state means for the chat is in the CLI's own docstring, which the bot reads.

## The steer

A steer is the one state derived from facts, by `steer_state(deferred, confirmed)`:

| Session host said | State |
| --- | --- |
| the input was held because the target is mid-turn | `queued` |
| the submit was confirmed | `delivered` |
| neither | `unconfirmed` |

A held message is `queued` whatever the submit fields say: reporting it `delivered` would make `ask` pass the turn the target was already on off as the answer.

## The CLI copy

`scripts/telegram_relay.py` imports nothing from the repo, so it keeps its own `RelayState` (the launcher's set plus `no_reply_yet`), `OUTCOMES` and `exit_code(state)`, declared as the `life-os-relay-state-cli` state machine. `tests/unit/life_os/test_relay_state.py` fails when the copy drifts from the launcher's set, and pins that a refusal or an unread reply never exits 0.
