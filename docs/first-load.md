# First load

Opening the app, the session list and the Board must feel instant, on a slow PC and on a phone. Three rules keep them that way.

## Last-known lists draw first

`app/webapp/static/shared/boot-snapshot.js` keeps the last session list and the last Board (with the chief's plan) this device received, one `localStorage` entry each. `boot()` in `main.js` draws both from there before any request goes out, so the lists show as soon as the scripts have run, whatever the server is doing. The live reads then replace them.

- A snapshot older than 24 hours is ignored; the list then shows its usual loading line.
- If the live read fails, the list keeps the snapshot under the usual stale line, `Last updated 14:05 · live data unavailable`, with the snapshot's time.
- The snapshots stay on the device. Nothing is sent anywhere, and blocked storage or a full quota only means the next load waits for the network.
- A body over 300 000 characters is not saved, so a huge Board never costs a poll its main-thread time.

## No boot read waits on another

The session list, the Board and the Jobs list are read beside `/api/config`, not after it. Both lists render safely before the config lands (Telegram sessions stay hidden until it says otherwise) and redraw once it has. The Board and Jobs reads self-gate to their tab; before they were part of boot, a reload on either tab waited for its first poll (5 s and 4 s).

On the server, boot reads that touch the disk (the agents' PATH check, the projects scan, the Life OS skills scan) run in a worker thread, so one slow disk read never queues every other request behind it on the event loop.

## Budgets and guards

| What | Budget |
|---|---|
| Board and session list usable, warm server, phone profile | ≤ 1 s |
| Cold start on a slow machine | ≤ 3 s |
| Any one first-load response, gzipped | < 100 KB |

- `tests/unit/shell/test_first_load_budget.py` fails when the served page, the stylesheet bundle or any module `main.js` imports statically reaches 100 KB gzipped, at the server's compression level. The stylesheet bundle is the closest to it.
- `tests/e2e/shell/test_first_load_never_waits.py` hangs every read the lists or boot could wait on and checks that both lists still draw from their snapshots, and that a reload on the Board reads it at boot rather than on its poll.

The phone profile is a 390×844 mobile viewport, 80 ms round trip, 10/5 Mbps and a 4× CPU throttle; the slow-machine profile is a desktop viewport with a 4× CPU throttle. Over plain HTTP/1.1 the first, cold load of ~180 modules is bound by six connections per origin; the served app speaks HTTP/2 over TLS, where they share one connection.
