# `usage-log` — local-only view, tap and device-class log

Records which views and controls a fleet web app's owner actually uses, and on which device class, so a design review reads data instead of asking "how often do you use X?". Two vendored halves, both local-only: this client module, which batches events and posts them to the app's **own origin**, and the server helper `src/usage_log.py` (manifest key `usage_log`), a FastAPI router that stores them in the app's runtime-data SQLite file. `scripts/usage_summary.py` in `project-scaffolding` reads that file for a review. Convention and rationale: `docs/webapp-pwa-conventions.md` → "Usage log" (`project-scaffolding#349`).

## Privacy is the contract

Never log message contents, document text, transcripts, names, file names, search queries, form values, URLs with query strings, IP addresses, or the raw user-agent. Only: event kind, view id, control id, a random per-visit session id, device class, and timestamps. Control ids are developer-chosen constants in `data-track`, never derived from element text or user data.

The server enforces it again: unknown kinds, ids outside the bounded id pattern and unknown device/OS classes are dropped, and the table has no column for anything else.

## Files

| File | Role |
| --- | --- |
| `usage-log.js` | `initUsageLog(opts?)` → `{ trackView, flush, currentView }`; also exports `deviceClass()` / `osClass()`. Batches events and sends them with `fetch` (`keepalive`) or `navigator.sendBeacon` when the page is hidden (a `keepalive` fetch instead when the `headers` option is set). No dependencies. |

The server half is `src/usage_log.py` (a separate manifest key, `usage_log`, because it lives in `src/`, not in this static tree).

## How to vendor

1. Copy this `usage-log/` folder **verbatim** into your app's static dir (`app/webapp/static/_vendored/usage-log/`), and `src/usage_log.py` verbatim into your `src/`. Do not edit either per-app.
2. Mount the router, with the database path resolved through `runtime_db_path` and the app's own auth:
   ```python
   from src.runtime_data import runtime_db_path
   from src.usage_log import usage_router

   app.include_router(usage_router(
       runtime_db_path("my-app", "usage.sqlite3", create=True),
       dependencies=[Depends(require_auth)],
   ))
   ```
3. Start the logger once and hand `trackView` to the nav:
   ```js
   import { initUsageLog } from '/static/_vendored/usage-log/usage-log.js';
   import { initNavTabs } from '/static/_vendored/nav/nav-tabs.js';

   const usage = initUsageLog();
   initNavTabs({ storageKey: 'my-app.tab', onChange: usage.trackView });
   ```
4. Add `data-track="<id>"` to the controls that matter. Not every control: the ones a design review would ask about.

## Authenticated endpoints

An app whose API sits behind a token (loopback bypasses it, a phone on the tunnel or tailnet does not) passes its auth header through `headers`, never by wrapping `window.fetch` or `navigator.sendBeacon`:

```js
const usage = initUsageLog({
  headers: () => ({ Authorization: 'Bearer ' + readToken() }),
});
```

- `headers` is an object, or a function returning one. A function is called on every send, so a token that rotates is read fresh. It is merged into the request headers; `Content-Type` stays `application/json`.
- A beacon cannot carry headers, so when `headers` is given the page-hide send is a `keepalive` fetch instead of `sendBeacon`. Without `headers`, sends are unchanged.
- Same-origin only and the privacy contract are unchanged: the headers go to the app's own endpoint, and nothing new is recorded. A `headers` function that throws is swallowed and the send goes without extra headers.

## Markup contract

- `data-track="<id>"` on a control records a `tap` with that id when the control (or anything inside it) is clicked. One delegated capture-phase listener on `document` handles every control; nothing scrapes element text.
- An id is a developer-chosen constant matching `^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$`: no spaces, no `/`, at most 64 characters. `refresh-all`, `job:cancel`, `settings.theme` pass; a sentence, a path or a URL does not, and is dropped on both sides.
- `trackView(id)` records a `view`. The nav's `onChange(tab)` calls it on every tab switch (including the initial restore), using the tab's `data-tab` value. Call it yourself for a sheet or dialog that deserves its own id.

## Event schema

The client posts `POST /api/usage/events` (same-origin path; an absolute or protocol-relative `endpoint` option disables the logger rather than send off-origin):

```json
{
  "session_id": "<random UUID, new per page load>",
  "device_class": "phone | tablet | desktop",
  "os_class": "ios | android | windows | other",
  "events": [
    {"kind": "session_start"},
    {"kind": "view", "id": "board"},
    {"kind": "tap", "id": "refresh-all", "view": "board"},
    {"kind": "session_end"}
  ]
}
```

| Kind | When |
| --- | --- |
| `session_start` | Page load, and each return to the foreground. |
| `view` | `trackView(id)`: a tab switch, or a sheet/dialog the app tracks. |
| `tap` | A click on a `[data-track]` control; carries the current view. |
| `session_end` | The page goes hidden (sent with `sendBeacon`). |

The server stamps the timestamp (UTC) on receipt and stores one row per event: `ts`, `session_id`, `kind`, `view_id`, `control_id`, `device_class`, `os_class`. Device class comes from the pointer type and the short side of the screen; OS class from a coarse match on the user-agent, which is never sent.

Batching: events queue and are sent at most every 10 s (`flushMs`), immediately at 50 queued, and with `sendBeacon` when the page is hidden. A few rows per visit, one transaction per batch, no per-tap `fsync`.

## Failure behaviour

Instrumentation never breaks the app. The client swallows every send failure (no route mounted, network error, any status). The server answers a bad envelope with 400, an oversized body with 413, and a locked or full database with 503 plus one info-level log line; none of it reaches the UI.

## Retention

Every insert prunes rows older than 365 days and caps the table at 200,000 rows (oldest first), so a runaway client cannot fill the disk. The file lives under the fleet runtime-data root, outside git; it needs the same backup treatment as other runtime SQLite state. Losing it loses history, not function.

## Required design tokens

None. The module renders nothing.

## Don't diverge

`usage-log.js` and `src/usage_log.py` are vendored verbatim: per-app variation is the `data-track` ids, the view ids and the database path. To change the contract, change it here in `project-scaffolding` and re-vendor downstream with `/propagate-vendored`. Streamlit POC spikes are exempt.
