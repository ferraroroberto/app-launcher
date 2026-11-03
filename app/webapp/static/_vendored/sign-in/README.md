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

The server's own `detail` text is never shown. The sheet only reports a lock-out the server enforces; it adds none of its own.

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
