# Real-device timings

`/perf-review` measures a throttled load from this PC over loopback, so it cannot see the phone's real network. These timings come from the real device on every launch and every slow request, so an "it was slow" report can be traced to one leg: the connection, the server, or the page.

## What is recorded

`app/webapp/static/perf-timing.js` writes two kinds of row and posts them to `POST /api/perf/timings` at most once every 10 seconds, and when the page hides. A poll that stays fast sends nothing.

- **`load`**, one per page load: the build the server was running and the client's asset hash (a stale cached copy shows a different hash), device and OS class, PWA or browser tab, the navigation type (`navigate` is a cold launch; `reload` and `back_forward` are warm), whether the document came from cache, and `navigator.connection.effectiveType` where the browser has it (iOS does not). Then the legs, in milliseconds:
  - `dns`, `connect`, `tls`: setting up the connection for the document (0 when a connection was reused).
  - `ttfb`: from sending the document request to its first byte.
  - `download`: from the first byte to the last byte of the document.
  - `dom_ready`: `DOMContentLoaded`, from the start of navigation.
  - `boot`: when every boot fetch had settled, from the start of navigation.
  - `last_path`, `last_wait`, `last_server`, `last_download`: the boot request that landed last, which is the one that decided when boot was done, with its own split.
- **`api`**, one per `/api/` request that took 800 ms or more in total, polls included:
  - `queue`: time before the request went out that was not DNS or connect, i.e. waiting behind other requests on the same connection.
  - `connect`: connect plus TLS, when a new connection was needed.
  - `wait`: from sending the request to the first byte of the response.
  - `server`: the server's own time, read from the response's `Server-Timing: app;dur=<ms>` header.
  - `download`: from the first byte to the last.
  - `total`: the whole request.
  - `status`: the HTTP status, 0 where the browser does not expose it.

`network = wait - server` is the round trip over the connection, computed by the summary.

Not recorded: the timings endpoint itself, the usage log's sends, EventSource streams and WebSockets (Resource Timing does not report them as fetches). The method is not recorded either, because Resource Timing does not expose it.

## Privacy

The browser sends the request path without its query string to this app's own server, which replaces it with the template of the route that serves it (`/api/claude-code/sessions/{sid}/input`) before anything is stored, so ids, file names and queries never reach the table. A path that no route serves is dropped. Every kind, class and number is checked against fixed sets and the range 0–120 000 ms; anything else is dropped. Never stored: page text, IP address, user agent or hostname. Rows live in `perf.sqlite3` in the app's runtime-data directory, beside `usage.sqlite3`, and are pruned at 90 days or 50 000 rows on every insert.

## Reading the summary

`launcher.py perf-summary [--days N] [--json]` and `GET /api/perf/summary?days=N` (1–90, default 7) give the same report: median and p95 per leg for loads and for slow API requests, per device class, then the slowest API paths (total, server and network) and how often each request was the last to land on boot. A leg that no row measured reads `unknown` (`"n": 0` in JSON), never zero. The CLI exits 6 when nothing has been recorded yet.

Which leg points where:

| High leg | Points at |
|---|---|
| `dns`, `connect`, `tls` | the connection: the Tailscale relay or the TLS handshake |
| `wait` minus `server` (`network`) | the network round trip |
| `server` | the app itself |
| `download` | bandwidth, or a large response |
| `queue` | too many requests at once on one connection |
| `dom_ready` or `boot` well after `ttfb` + `download` | the page: script work, or a slow boot request (see `last_path`) |

Compare phone rows with desktop rows: a leg that is high on the phone and low on the PC is the network; a leg that is high on both is the app.

`server` reads unknown when the browser does not expose `Server-Timing` to the page. If the phone's rows all show it unknown, only the `wait` leg is available for that device, and the network and server shares cannot be separated.
