# First load

Opening the app, the session list and the Board must feel instant, on a slow PC and on a phone. Three rules keep them that way.

## Last-known lists draw first

`app/webapp/static/shared/boot-snapshot.js` keeps the last session list and the last Board (with the chief's plan) this device received, one `localStorage` entry each. `boot()` in `main.js` draws both from there before any request goes out, so the lists show as soon as the scripts have run, whatever the server is doing. The live reads then replace them.

- A snapshot older than 24 hours is ignored; the list then shows its usual loading line.
- If the live read fails, the list keeps the snapshot under the usual stale line, `Last updated 14:05 · live data unavailable`, with the snapshot's time.
- The snapshots stay on the device. Nothing is sent anywhere, and blocked storage or a full quota only means the next load waits for the network.
- A body over 300 000 characters is not saved, so a huge Board never costs a poll its main-thread time.

## No boot read waits on another

The session list is read beside `/api/config`, not after it, and so is the Board or the Jobs list when the page boots on that tab. The lists render safely before the config lands (Telegram sessions stay hidden until it says otherwise) and redraw once it has. Without the boot read, a reload on the Board or Jobs tab would wait for its first poll (5 s and 4 s).

## Tab views load on first open

The Board and Jobs views are not in the boot module graph: `app/webapp/static/shared/lazy-views.js` imports each one (`board.js` with `board/`, `jobs.js` with `jobs/`) the first time its tab opens, wires it, paints the Board's last-known lanes and reads its data. Every static import is one more request queued on the browser's six HTTP/1.1 connections, and a launch shows one tab, so a view nobody opened costs nothing. Their polls run only once the view has loaded. Code is the default tab and stays static, and so do Life OS and Apps, whose modules serve every tab (the project list, the skills, the Telegram settings). A caller outside a lazy view goes through `loadView()` (the chief row's Start, Resume and Restart load the Board's chief lifecycle on the tap) or `ifViewLoaded()` (a Settings change repaints the Board only if it is loaded). The Board's tab badge never needs the view: `home-head.js` counts it from the sessions poll.

On the server, boot reads that touch the disk (the agents' PATH check, the projects scan, the Life OS skills scan) run in a worker thread, so one slow disk read never queues every other request behind it on the event loop.

## Budgets and guards

| What | Budget |
|---|---|
| Board and session list usable, warm server, phone profile | ≤ 1 s |
| Cold start on a slow machine | ≤ 3 s |
| Any one first-load response, gzipped | < 100 KB |

- `tests/unit/shell/test_first_load_budget.py` fails when the served page, the stylesheet bundle or any module `main.js` imports statically reaches 100 KB gzipped, at the server's compression level. The stylesheet bundle is the closest to it.
- The ES-module bundle is the one exception to the per-response budget. Its shared chunk holds most of the boot graph, about 190 KB gzipped, because the modules import each other too densely for esbuild to split them into pieces under 100 KB without dozens of requests. It carries about the same bytes as the separate modules did, in 2 boot requests instead of ~155. On the `/perf-review` phone profile (80 ms round trip, 10 Mbps, no CPU throttle) over HTTP/2 that cut cold ready from about 1015 ms to about 717 ms (median of 15 alternated loads).
- `tests/e2e/board/test_board_first_load.py` hangs every read the lists or boot could wait on and checks that both lists still draw from their snapshots, and that a reload on the Board sends its read at boot rather than on its poll.

The phone profile is a 390×844 mobile viewport, 80 ms round trip, 10/5 Mbps and a 4× CPU throttle; the slow-machine profile is a desktop viewport with a 4× CPU throttle. Over plain HTTP/1.1 the first, cold load of ~155 modules is bound by six connections per origin; the served app speaks HTTP/2 over TLS, where they share one connection.

## The module bundle

At start the server bundles `main.js`'s module graph with a pinned esbuild (`src/static_bundles/js_bundle.py`) into an entry, a shared chunk and one chunk per lazy view, served from `/static/js-bundle/` with source maps. Each module is still its own request round trip of discovery over HTTP/2, so bundling, not the protocol, removes the module graph's wait. The build is cached under `webapp/js-bundle/` per fleet hash, so any module edit rebuilds it on the next start and a restart with no edit reads the cache. Without Node, or when the build fails, the server logs a warning and serves `main.js` and its modules separately. A request carrying the cookie `launcher_js_bundle=off` gets the separate modules too: an e2e test that imports a live module by URL needs it, since under the bundle that URL would be a second copy with its own state.
