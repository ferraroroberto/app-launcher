# `usage-log` — local-only view, tap and device-class log

Records which views and controls a fleet web app's owner actually uses, and on which device class, so a design review reads data instead of asking "how often do you use X?". Two vendored halves, both local-only: this client module, which batches events and posts them to the app's **own origin**, and the server helper `src/usage_log.py` (manifest key `usage_log`), a FastAPI router that stores them in the app's runtime-data SQLite file. `scripts/usage_summary.py` in `project-scaffolding` reads that file for a review. Convention and rationale: `docs/webapp-pwa-conventions.md` → "Usage log" (`project-scaffolding#349`).

## Privacy is the contract

Never log message contents, document text, transcripts, names, file names, search queries, form values, URLs with query strings, IP addresses, or the raw user-agent. Only: event kind, view id, control id, a random per-visit session id, device class, and timestamps. Control ids are developer-chosen constants in `data-track` and in `usage.track(id)` calls, never derived from element text or user data.

The one widening is identified mode (below): a public site only, only after its visitor consented, never a fleet app.

The server enforces it again: unknown kinds, ids outside the bounded id pattern and unknown device/OS classes are dropped, and the table has no column for anything else.

## Files

| File | Role |
| --- | --- |
| `usage-log.js` | `initUsageLog(opts?)` → `{ trackView, track, flush, currentView, stop }`; also exports `deviceClass()` / `osClass()`. Batches events and sends them with `fetch` (`keepalive`) or `navigator.sendBeacon` when the page is hidden (a `keepalive` fetch instead when the `headers` option is set). No dependencies. |

The server half is `src/usage_log.py` (a separate manifest key, `usage_log`, because it lives in `src/`, not in this static tree). A Starlette app with no FastAPI of its own adds `src/usage_log_starlette.py` (manifest key `usage_log_starlette`); see "Starlette apps" below.

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
4. Give **every** interactive control a constant `data-track="<id>"` and every `<dialog>` a `data-track-view="<id>"`. The usage track ratchet (`scripts/usage_track_ratchet.py` in `project-scaffolding`, manifest key `usage_track_ratchet`) fails the gate on a new control or sheet without one; vendor it with this component.

## Starlette apps

`usage_router` is a FastAPI router. An app built on bare Starlette vendors `src/usage_log_starlette.py` verbatim beside `src/usage_log.py` (manifest key `usage_log_starlette`; FastAPI must be installed, as `usage_router` needs it) and mounts one route instead of calling `include_router`:

```python
from src.runtime_data import runtime_db_path
from src.usage_log_starlette import usage_route

routes = [
    usage_route(runtime_db_path("my-app", "usage.sqlite3", create=True)),
    ...,  # the app's other routes, catch-alls and `/api/{token}`-style routes included
]
```

The route hands every `/api/usage/{rest:path}` request to a small FastAPI app that holds the router, with interactive docs and the OpenAPI document switched off. Declare it **before** any parameterised or catch-all `/api/...` route, so a usage request is never read as something else.

**Reads are owner-only.** `POST /api/usage/events` is open to every caller of the page, guests included, because the page's controls are what the log counts. `GET /api/usage/summary` and `/time` show how the owner uses the tool, so they answer `403 {"detail": "owner only"}` to anyone else. The route takes no `dependencies` argument; an app that wants its own auth in front of the events endpoint puts it in a middleware.

Who the owner is cannot come from the peer address alone: a server bound to loopback is reached by a guest through Tailscale Funnel, which connects from the machine itself. So the headers Tailscale's proxy sets decide:

| Request | Owner? |
| --- | --- |
| Peer on loopback or the tailnet (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`), no proxy header | yes |
| Peer on loopback with `Tailscale-Funnel-Request` | no |
| Peer on loopback with `X-Forwarded-For` whose last address is loopback or on the tailnet | yes |
| Peer on loopback with `X-Forwarded-For` that is public, empty or does not parse | no |
| Any other peer (LAN, public, missing, unparsable) | no |

The last address of `X-Forwarded-For` decides, because Tailscale's proxy replaces the header with the caller's address, so a spoofed leading entry cannot promote a guest. An app fronted by some other proxy must not rely on this check.

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

## Public sites (`site: true`)

A public website served by a Cloudflare Worker (not the owner's machine) vendors the third piece, `src/usage_log_worker.js` (manifest key `usage_log_worker`), and mounts it in its Worker on the same paths:

```js
import { handleUsageEvents, usageSummary } from './usage_log_worker.js';
if (url.pathname === '/api/usage/events') return handleUsageEvents(request, env.DB);
// GET /api/usage/summary, behind the site's own auth:
return Response.json(await usageSummary(env.DB, 30));
```

and starts the client in site mode:

```js
initUsageLog({ site: true });
```

Site mode adds, on top of everything above: the page as a `view` on load (`pageId(location.pathname)`: `/` is `home`, `/a/b` is `a:b`), one `referrer` event per page load when the visitor came from another host (id = that host, `linkedin.com`), and every followed link that has no `data-track` of its own as a `tap` (`linkId`: `nav:<page id>`, `out:<host>`, `mail`, `tel`; in-page anchors are skipped). The Cloudflare half stores the same rows plus `country` (two letters from the edge) and `visitor` (a SHA-256 of a random salt, the address and the user agent, cut to 16 hex; the salt is new each UTC day and deleted after it). It never stores the address, the user agent, a URL, a query string or a cookie. Without `site`, the client sends exactly what it always did.

## Identified mode (public sites, only behind consent)

A public site whose owner chose to keep a returning-visitor id and the raw address runs this mode, and only after the visitor has accepted a consent banner. Fleet apps never use it.

```js
// Called only once the consent banner reports `granted`; nothing runs before it.
const usage = initUsageLog({ site: true, visitorCookie: 'uv' });
// Consent withdrawn:
usage.stop();
```

```js
return handleUsageEvents(request, env.STATS, {
  identify: { visitorCookie: 'uv', consentCookie: 'consent', ipRetentionDays: 30 },
});
```

- **Client.** Importing the module does nothing: no request, no cookie, no listener. `visitorCookie` names a first-party cookie holding a random 128-bit hex id (`Path=/`, `SameSite=Lax`, `Secure` on https, lifetime `visitorCookieDays`, default 396 days, about 13 months). It is created when absent or malformed and never extended by a later visit. The id is not in the posted body; the browser sends the cookie to the same-origin endpoint. A cookie name outside the id pattern disables the logger. `stop()` detaches every listener, drops the queue unsent, deletes the visitor cookie and makes every later call a no-op; it exists in every mode.
- **Server, consent enforced again.** With `identify`, a request whose `consentCookie` is not `granted` stores nothing and touches no table: it answers `200 {accepted: 0, rejected: <n>}` with one `usage.no_consent` log line. With consent, `visitor` holds the cookie id (32 lowercase hex, else NULL) instead of the daily hash, no daily salt is used, and the `ip` column holds `cf-connecting-ip` (an address literal, else NULL). A missing cookie name or a non-positive `ipRetentionDays` is a 503 with a `usage.misconfigured` line, never a silent default.
- **Address retention.** Every identified write clears `ip` on rows older than `ipRetentionDays`; country, device, pages and the visitor id stay. The same purge is exported as `purgeIps(db, days)` for a read path (an admin page) to run too, since a Pages project has no timer.
- **Summary.** `usageSummary` and `usage_log.summarize` add `returning`: visitors in the window also seen on another UTC day, earlier stored history included. It is 0 for the daily hash, which changes every day.
- **Schema.** The `ip` column is created in place on an older D1 database and on an older local file; `src/usage_log.py` carries it always empty, so `scripts/usage_summary.py` reads an exported D1 file.

## Markup contract

- `data-track="<id>"` on a control records a `tap` with that id when the control (or anything inside it) is clicked. One delegated capture-phase listener on `document` handles every control; nothing scrapes element text.
- An id is a developer-chosen constant matching `^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$`: no spaces, no `/`, at most 64 characters. `refresh-all`, `job:cancel`, `settings.theme` pass; a sentence, a path or a URL does not, and is dropped on both sides.
- `usage.track(id)` records the same `tap` (same id pattern, same current view, same batch) with no click, for a feature counted when it **succeeded**: an upload finished, a transcript landed, a send was acknowledged. The id is a string literal in the call, never built from element text, a file name, a message or any user data; an id outside the pattern queues nothing. When the logger is disabled (off-origin `endpoint`) the call is a no-op, so callers need no guard. Never fake a click on a probe element with a runtime-set `data-track` instead.
- `trackView(id)` records a `view`. The nav's `onChange(tab)` calls it on every tab switch (including the initial restore), using the tab's `data-tab` value. It is for tabs; a sheet uses `data-track-view` instead.
- `data-track-view="<id>"` on a `<dialog>` (or any element whose `open` attribute toggles) records a `view` with that id when it opens and returns to the view under it when it closes or leaves the document, so its minutes stop the moment it goes. Open sheets stack: closing a sheet opened from a sheet returns to its parent. A tab switch while a sheet is open records the tab at once, and closing the sheet then stays on it. The client watches `open` with one `MutationObserver`, so `showModal()`, `show()` and `close()` need no extra call. A sheet already open in the markup at page load is not seen.

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
| `view` | `trackView(id)` on a tab switch; a `data-track-view` sheet opening, or closing back to the view under it. |
| `tap` | A click on a `[data-track]` control, or a `usage.track(id)` call; carries the current view. |
| `session_end` | The page goes hidden (sent with `sendBeacon`). |
| `referrer` | `site` mode only: once per page load when the referrer is another host; the id is that host. |

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
