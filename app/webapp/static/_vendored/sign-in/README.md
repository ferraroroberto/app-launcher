# `sign-in` — the one password sign-in sheet

The fleet's password sign-in: a native `<dialog>` on the vendored [`modal`](../modal/) shell, styled like app-launcher's sign-in card, with a lock-glyph title, one hint line, one password field, one inline error line and one full-width `button-primary`. The JS builds the sheet and exposes one awaitable call, so any number of API calls that hit a 401 can wait on the same sign-in. It is UI only: it posts the password to the app's own route and hands the token back, and it never reads or writes storage. Contract: `~/.claude/design.md` → `modal` token block and "Component contracts".

Every app that asks for its own password uses this sheet. The conventions ratchet's `password-input-vendored` rule ([docs/conventions-ratchet.md](../../../../../docs/conventions-ratchet.md)) fails any password input outside a `_vendored/sign-in/` folder.

## Files

| File | Role |
| --- | --- |
| `sign-in.js` | `requestSignIn()`: builds the `<dialog>` once (a direct `<body>` child), posts `{password}`, maps each refusal to its fixed message, shows the busy state, resolves with the token. |
| `sign-in.css` | What the sheet adds to the modal: the title glyph, the hint line, the full-width field and the error line. Tokens only. |

There is no markup skeleton to paste: the sheet's markup lives in `sign-in.js`, so the password field cannot be re-authored per app.

## How to vendor

1. Copy this `sign-in/` folder **verbatim** into the app's static dir, beside the components it builds on, and declare it in the app's `[vendored]` table (manifest key `sign-in`).
2. Link the CSS with its companions, and keep `i-lock` in the app's icons sprite (without it the title shows no glyph):
   ```html
   <link rel="stylesheet" href="/static/_vendored/select-native/select-native.css">
   <link rel="stylesheet" href="/static/_vendored/button/button.css">
   <link rel="stylesheet" href="/static/_vendored/modal/modal.css">
   <link rel="stylesheet" href="/static/_vendored/sign-in/sign-in.css">
   ```
3. Call it wherever the app learns it is signed out (at boot with no token, or on any 401), and store the token the app's own way:
   ```js
   import { requestSignIn } from '/static/_vendored/sign-in/sign-in.js';

   const TOKEN_KEY = 'launcher.token';   // the app's existing key, unchanged

   async function authorizedFetch(url, init = {}) {
     const send = () => fetch(url, { ...init, headers: { ...init.headers,
       Authorization: `Bearer ${localStorage.getItem(TOKEN_KEY) || ''}` } });
     const res = await send();
     if (res.status !== 401) return res;
     await requestSignIn({
       endpoint: '/api/login',
       hint: 'Enter the password to use the launcher.',
       onToken: (token) => localStorage.setItem(TOKEN_KEY, token),
     });
     return send();
   }
   ```
4. Delete the app's own login overlay, its CSS and its submit wiring in the same change.

## API

`requestSignIn({ endpoint, onToken, hint })` → `Promise<string>` (the token).

| Option | Required | What |
| --- | --- | --- |
| `endpoint` | yes | The app's login route. The sheet posts `{"password": "..."}` as JSON and expects `200 {"token": "..."}`. |
| `onToken` | yes | Stores the token the app's own way (its own `localStorage` key, a cookie call, memory). May return a promise; the sheet closes only after it settles. If it throws, the sheet stays open and shows the fallback line. |
| `hint` | no | One line under the title. Omitted, the line is hidden. |

While the sheet is open, every further call returns the **same promise** and ignores its options, so one sign-in answers every waiting request. Nothing else is configurable: the title, the labels, the messages and the markup are the contract.

## Behaviour

- **Opens** with `showModal()`, focused on the password field (the one thing to do is type), unlike an editor modal, whose title takes focus. The fleet nav hides while it is open (`body:has(dialog[open])`).
- **Cannot be dismissed.** There is no ×, and Esc does nothing: an app that asks for its password cannot be used without it, so the sheet stays until a sign-in succeeds.
- **The primary** reads "Sign in" and stays disabled while the field is empty. While the request is pending it reads "Signing in…", is disabled with `aria-busy="true"`, and the field is read-only.
- **Errors** are one line of `danger-text` under the field (`role="alert"`, never a tinted box). The message set is fixed, so the same condition reads the same in every app:

| Server reply | Message |
| --- | --- |
| `401` | Wrong password (the field is selected for retyping) |
| `429` with `Retry-After: N` (seconds or an HTTP date) | Too many attempts — try again in N s |
| `429` without a usable `Retry-After` | Too many attempts — try again later |
| `503` | Password sign-in isn't set up on this server |
| no reply (the request failed) | Can't reach the app |
| any other status, or `200` with no token | Sign-in failed (HTTP *status*) |
| `onToken` threw | Sign-in failed — try again |

The server's own `detail` text is never shown. The sheet only reports a lock-out the server enforces; it adds none of its own. The server half is below.

## Server side — `src/login_lockout.py`

The login route enforces the lock-out with the vendored, stdlib-only `src/login_lockout.py` (manifest key `login_lockout`; copy it byte-for-byte with `/propagate-vendored login_lockout`). The conventions ratchet's `login-lockout` rule ([docs/conventions-ratchet.md](../../../../../docs/conventions-ratchet.md)) fails a `@<x>.post(".../login")` route that does not use it.

**Policy** (the defaults; each is a keyword argument): 5 wrong passwords per client are free; each further one makes the client wait 2 s, doubling to a 300 s cap; a client with no failure for 1 hour starts over; a correct password clears the count. A locked client is refused before its password is read and without adding to its count. There is no limit across all clients, so nobody can lock the owner out. At most 1,024 clients are tracked. State is in memory, so a restart clears it.

**Wiring.** One instance per app, three calls in front of the existing handler. The password check, the `503` replies and the token are unchanged:

```python
from src.login_lockout import LoginLockout, retry_after_header

lockout = LoginLockout(on_alert=alert_owner)      # once, in create_app(); see "The alert"

@router.post("/api/login")
async def login(request: Request) -> dict[str, str]:
    client = request.client.host if request.client else "?"   # the address the app already logs
    exempt = is_pc_itself(request)                             # the app's own loopback trust check
    wait = 0.0 if exempt else lockout.retry_after(client)
    if wait > 0:
        raise HTTPException(429, "too many attempts", headers=retry_after_header(wait))
    ...                                                        # the app's own 503 checks
    if not secrets_equal(presented, password):
        if not exempt:
            lockout.record_failure(client)
        raise HTTPException(401, "bad password")
    lockout.reset(client)
    return {"token": token}
```

- **The key** is the client address the app already resolves and writes to its auth log. The module never reads addresses or headers.
- **The exemption** is the app's call, with the check it already uses to skip the token for the PC itself (app-launcher: `trust.is_pc_itself`). Do not exempt a raw `127.0.0.1` or `::1`: a local tunnel delivers every public request over loopback.
- **The log**: the module writes one info line per lock (`🔒 login locked for N s after K failures`) that names no client or password. The app's own auth-log lines stay as they are.
- **The test**: each app adds one route test. Six wrong passwords get five `401`s and then a `429` with a `Retry-After` header; a correct password after the wait gets the token.

### The alert — three wrong passwords in a row

`on_alert` is called once per failure streak, when a client reaches 3 consecutive wrong passwords (`alert_after`). The streak ends on a correct password or after the hour of quiet; until then the client raises no second alert, however many more times it fails. A global `alert_cooldown` (300 s) limits alerts across all clients. Alerts it holds back are counted and reported in the next one, so an attacker who rotates addresses sends one message, not hundreds. The callback receives a `LockoutAlert(key, failures, suppressed)`, which never holds a password or a token. It runs inside the login request, so it must return at once: hand the send to a thread. If it raises, the module logs the exception type and the login carries on.

The alert goes to the fleet's **`coding alerts`** Telegram chat: the act-now chat, which `fleet-config` routes as the `attention` category (`telegram_chat_attention`; chat table in `fleet-config`'s `docs/telegram-workflow.md`). Like the app's other machine alerts, the app sends it with its own vendored [`src/notify`](../../../../../src/notify/README.md) notifier, its bot token and the `coding alerts` chat id read from its own ignored config:

```python
import threading

from src.notify import NotifierError, TelegramConfig, build_notifier

notifier = build_notifier(cfg.notifier, TelegramConfig(cfg.telegram_bot_token, cfg.telegram_alert_chat_id))

def alert_owner(alert: LockoutAlert) -> None:
    if notifier is None:          # notifier = "none": no alert channel configured
        return
    held = f" (+{alert.suppressed} more held back)" if alert.suppressed else ""
    text = f"🚨 {APP_NAME}: {alert.failures} wrong passwords in a row from {alert.key}{held}"

    def send() -> None:
        try:
            notifier.send_text(text)
        except NotifierError as exc:
            logger.warning(f"⚠️ login alert not delivered: {type(exc).__name__}")

    threading.Thread(target=send, daemon=True).start()
```

The message carries the app, the client address and the failure count, and nothing else. An app that has no notifier config of its own can send the same text through `fleet-config`'s Telegram transport instead: `notify_send.py --category attention --text "..."` from its `hooks/` folder, spawned with `CREATE_NO_WINDOW`. That transport resolves the `coding alerts` chat itself.

## Usage log

The sheet is `data-track-view="sign-in"` and its primary is `data-track="sign-in-submit"`, so the usage log records each open, its time on screen and each submit. The password, the hint and the server reply are never recorded.

## Required design tokens

| Token | Light value | Used for |
| --- | --- | --- |
| `--muted` | `#656d76` | the hint line |
| `--danger-text` | `#a40e26` (dark `#ff7b72`) | the error line |
| `--space-sm` | `8px` | title glyph gap, space above the error |
| `--space-md` | `16px` | space under the hint |
| `--font-body-sm` | `0.875rem` | hint and error |
| `--icon-title` | `18px` | the lock glyph |

Plus every token the [`modal`](../modal/README.md#required-design-tokens), `button` and `select-native` components require.

## Not for

- **A separate sign-in page with a different session** (a cookie session, an access token *or* a password in one `secret` field, a second "who are you" step): the sheet's request and response do not fit. Such a page keeps its own field under a `password-input-vendored-exempt: <reason>` marker until the sheet grows that mode.
- **A password that is not this app's own** (a field that types into another machine's page): it is a form field, not a sign-in, and carries the same marker with its reason.
- **Passkeys and step-up checks**, which are separate gates on top of a signed-in session.

## Don't diverge

`sign-in.js` and `sign-in.css` are vendored verbatim. To change the sheet, change it here in `project-scaffolding` and re-vendor with `/propagate-vendored sign-in`. Streamlit POC spikes are exempt.
