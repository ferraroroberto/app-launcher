# WS proxy: catch an upstream handshake rejection

**Issue:** #61 — Coding tab WS proxy lets a session-host handshake rejection
escape as an unhandled exception.

## Diagnosis

`proxy_session_ws` (`app/webapp/routers/sessions.py`) — the
browser ⇄ webapp ⇄ session-host WebSocket proxy for the Coding tab terminal —
wrapped the upstream `ws_connect` in `except (OSError, WebSocketDisconnect)`.

- `OSError` covers "session-host process not listening at all".
- `WebSocketDisconnect` covers a mid-stream drop.
- Neither covers a handshake that **completes a TCP connection but is rejected
  at the HTTP-upgrade layer** — the session-host answering **HTTP 403** for a
  reaped or unknown session. The `websockets` client raises
  `websockets.exceptions.InvalidStatus` (a subclass of `InvalidHandshake`)
  there, which escaped as an unhandled ASGI exception: a full traceback in the
  webapp log, and the browser socket dropped with no reason code.

Surfaced while diagnosing #58 — on the CI runner `claude` is absent, so the PTY
session dies and is reaped immediately; the webapp's input-proxy WS attempt
then hits the session-host's 403 and the unhandled traceback showed up in the
captured webapp log.

## What was done

- **`app/webapp/routers/sessions.py`** — added
  `from websockets.exceptions import InvalidHandshake` and widened the proxy's
  `except` tuple to `(OSError, WebSocketDisconnect, InvalidHandshake)`.
  `InvalidHandshake` is the base class — it covers `InvalidStatus` (the 403) as
  well as `InvalidMessage` / other malformed-upgrade cases. An upstream
  handshake rejection is now the same "upstream not usable" condition that
  already maps to a clean `4502` close back to the browser, with a `debug` log
  line instead of a traceback.
- **`tests/test_webapp_api_sessions.py`** — new `TestProxySessionWS` class:
  `test_upstream_handshake_rejection_closes_4502` stubs `ws_connect` to raise
  `InvalidHandshake` and asserts the browser socket closes with code `4502`;
  `test_upstream_unreachable_still_closes_4502` guards the existing `OSError`
  path. Both patch `LOOPBACK_HOSTS` so the `TestClient` host bypasses the
  Tailscale/passkey gate and reaches the `ws_connect` call under test.

## Files modified

- `app/webapp/routers/sessions.py`
- `tests/test_webapp_api_sessions.py`

## Validation

- `& .\.venv\Scripts\python.exe -m py_compile` on both modified files.
- `pwsh -File scripts/verify-before-ship.ps1` — full pre-ship gate, exit 0:
  135 non-e2e tests pass, 47 e2e pass / 7 skipped.

## Out of scope

Whether a reaped/unknown session should return `403` or `404` from the
session-host — the proxy fix holds either way (#61 notes this explicitly).
