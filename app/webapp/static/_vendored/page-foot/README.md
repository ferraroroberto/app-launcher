# `page-foot` — the build-identity footer

The fleet's canonical **page-foot**: a centered, quiet footer line — `Build: <sha> · YYYY-MM-DD HH:MM` — showing which build the PWA is running. This is the difference between "the tray restarted" and "the *new* build is live" (see project-scaffolding's `CLAUDE.md` "Build-identity footer" convention): a `/healthz` 200 passes on a stale process, a matching `git_sha` does not. Normalized from home-automation's shipped footer; whatsapp-radar#122's first feedback item was precisely a footer-format divergence from a private re-implementation of the same text — this component is that one shared function, written once.

## Files

| File | Role |
| --- | --- |
| `page-foot.css` | Visual contract — centered layout, muted text, and its own pinned type: the design.md `caption` style (system-ui, 12px, 600, line-height 1.1) with tabular numerals. Inherits nothing from the app's body or container font, and doesn't depend on external `.muted`/`.small` utility classes. |
| `page-foot.js` | ESM — `fmtBuildTime(iso)` + `buildReadoutText(sha, builtAtIso)`, the one shared text formatter so the footer string can't drift between apps. |
| `page-foot.html` | Markup skeleton to copy and adapt. |
| `restart-sheet.js` / `restart-sheet.css` | Optional restart on a tap of the readout; see "Restart from the readout". |

## How to vendor

1. Copy this `page-foot/` folder **verbatim** into your app's static dir (`app/webapp/static/_vendored/page-foot/`). Do **not** edit `page-foot.css` / `page-foot.js` per-app.
2. Link the CSS and paste the skeleton as the last child of the content wrapper `<main class="app">`, outside the tab panels:
   ```html
   <link rel="stylesheet" href="/static/_vendored/page-foot/page-foot.css">
   ...
   <footer class="page-foot">
     <p id="buildReadout"></p>
   </footer>
   ```
3. Wire your own version fetch (still your app's job — the endpoint is auth-gated per project-scaffolding's `/api/version` convention):
   ```js
   import { buildReadoutText } from '/static/_vendored/page-foot/page-foot.js';

   async function fetchVersion() {
     try {
       const body = await jsonApi('/api/version');
       els.buildReadout.textContent = buildReadoutText(body.git_sha || 'unknown', body.built_at || '');
     } catch (_) {
       els.buildReadout.textContent = '';
     }
   }
   ```

## First-time adopter checklist

An app that has never shown a build line, or has its own, does these in order:

1. Vendor the folder verbatim (How to vendor, step 1) and add it to the app's `[vendored]` table in `.fleet.toml`.
2. Serve `GET /api/version` → `{git_sha, built_at}` (`docs/webapp-pwa-conventions.md` "Webapp PWA required surfaces"), auth-gated with loopback bypass.
3. Link `page-foot.css`; paste the skeleton as the last child of `.app`, once, outside the tab panels.
4. Fetch `/api/version` and set the `<p>` with `buildReadoutText()`; on failure leave it empty.
5. Delete the app's own build line, footer markup and CSS. A Settings "Build" row may stay.
6. Define the tokens in the table below, and keep the nav's `--nav-bottom-reserve` on `.app`; add no bottom padding for the footer.
7. Open the app on a phone-size viewport, scroll each tab to the end and confirm the footer sits fully above the bottom fade.

## Markup contract

```html
<footer class="page-foot">
  <p id="buildReadout"></p>
</footer>
```

- Lives **outside every card**, as the last child of `.app` — it's app-wide identity, not a section's content.
- **Mount contract.** One `<footer class="page-foot">` per page, holding one `<p>` that `buildReadoutText()` fills from your own `GET /api/version` fetch (`git_sha`, `built_at`). It is the last child of the content wrapper (`.app`), so it ends each tab's content. Mount it once, outside the tab panels, not per tab.
- **Position contract.** The component adds block margin only (`--space-md` above and below, `--space-xs` at the sides). It adds no bottom padding for the nav: the space that scrolls it clear of the bottom fade and the pill is the nav's `--nav-bottom-reserve` on `.app` (see `nav/README.md` "Bottom reserve and hit area"). Scrolled to the end, the footer ends a `--gap` above the fade, fully undimmed. Don't add your own bottom padding for it.
- **Type contract.** Family, weight, line-height, letter-spacing, style, case and numeral style are set by the component, so the line is the same under any app's body font. `tests/e2e/test_vendored_home_head.py` mounts it under two different body typefaces and asserts equal computed style and equal pixels. Don't set `font-*`, `letter-spacing` or `text-transform` on `.page-foot` or its `<p>`.
- The `<p>`'s text is set entirely by `buildReadoutText()`; no additional class is needed (the styling is self-contained in `page-foot.css`, not borrowed from generic `.muted`/`.small` utilities).
- Any reload-on-stale-asset logic (e.g. home-automation's asset-hash comparison that triggers one automatic reload after a deploy) is orthogonal to the footer text and stays app-specific — out of scope here.

## Restart from the readout

Optional. An app whose tray declares a restart recipe can let the owner tap the build readout and restart the app from any client that can already use it, the phone over the tailnet included. Two halves, both vendored verbatim:

| File | Role |
| --- | --- |
| `restart-sheet.js` | `initRestartSheet(opts?)`: reads `GET /api/restart`; with no targets, or no route, the readout stays plain text. Otherwise the readout becomes a button (`data-track="build.restart-open"`) opening a `modal` sheet (`data-track-view="build.restart"`, `data-surface="app.restart-sheet"`). Also exports the pure `outcome()` decision and the `POLL_INTERVAL_MS` / `RESTART_TIMEOUT_MS` cadence. |
| `restart-sheet.css` | The tappable readout (cursor, dotted underline, a 44px hit area that moves nothing) and the sheet's status line and log. |
| `src/self_restart.py` | The server half (manifest key `self_restart`): `GET`/`POST /api/restart`, the detached runner and the run record. |

### Server

```python
from src.runtime_data import runtime_data_dir
from src.self_restart import TRAY_RESTART, RestartTarget, restart_router

app.include_router(restart_router(
    targets=[RestartTarget("app", "Restart app", TRAY_RESTART)],
    status_dir=runtime_data_dir("my-app", create=True) / "restart",
    guard=require_auth,   # the dependency the app's other write routes use
))
```

- **Auth is the app's.** `guard` is required and has no default: the same dependency that protects the app's other write routes, so any signed-in client may restart and nobody else can. The `POST` also needs an `Origin` matching the request's host, so another site cannot post a restart from a signed-in browser.
- **Only declared targets run.** Targets are declared in code, each a fixed argv run from the checkout root; the client sends a target id, never a command. `TRAY_RESTART` is the repo's own `tray.bat --restart`, spelled `cmd.exe /c .\tray.bat --restart` because `cmd` does not search the working directory when `NoDefaultCurrentDirectoryInExePath=1`. A target with a `warning` makes the sheet ask twice and show it. The `warning` is a string or a zero-argument callable: a callable runs on every `GET /api/restart`, so a warning that depends on live state ("this ends 3 live sessions") is current. A callable that raises degrades to a generic non-empty warning, never to none. More than one target adds a picker (`data-track="build.restart-target"`).
- **The runner leaves the tray's tree.** `tray.bat --restart` kills the tray's whole tree, webapp included, so the runner starts through `cmd /c start` and is no one's child when the kill comes. It runs the recipe with the target's timeout (default 180 s) and writes `last.json` (the record) and `last.log` (the recipe's output) under `status_dir`.
- **The recipe runs from the launch spelling.** The checkout root comes from the process's own `sys.prefix`, which keeps the path spelling the tray was started from; `Path.resolve()` would replace a junctioned fleet root with its target.
- **One run at a time.** A `POST` while a run is in flight answers `409`. A record still `running` 30 s past its timeout reads `unknown`, the runner having vanished, and frees the slot.

### Client

```html
<link rel="stylesheet" href="/static/_vendored/modal/modal.css">
<link rel="stylesheet" href="/static/_vendored/page-foot/restart-sheet.css">
```
```js
import { initRestartSheet } from '/static/_vendored/page-foot/restart-sheet.js';
initRestartSheet();   // or initRestartSheet({ fetchJson: jsonApi }) when reads carry a bearer
```

Each time the sheet opens it re-reads `GET /api/restart` and takes the targets' labels and warnings from that answer, with the confirm button held until the read lands, so the warning shown is the one true at that moment and an adopter needs no re-read of its own. A failed re-read keeps the list the page loaded with. Targets that appear or disappear after page load are not picked up until the next load.

The sheet needs the `modal`, `button`, `icon-button` and (with a picker) `select-native` stylesheets. Add `app.restart-sheet` to the app's UX surface map.

### Outcomes

After the `202`, the page polls `/api/version` and `/api/restart` every 1.5 s for up to 90 s. `built_at` is captured at process start, so a new process shows even when the SHA did not change.

| Outcome | When | Shown |
| --- | --- | --- |
| restarted | a new `built_at`, and the record says `ok` | "Restarted · Build: …", then one reload |
| failed | the record says `failed`, `timeout`, `error` or `unknown` | the reason, the exit code and the log tail; no reload |
| not confirmed | at 90 s: a new process without an `ok` record, or `ok` without a new process | "The app is back, but the restart result is not confirmed." |
| timed out | at 90 s: no new process and no `ok` | "The app did not come back in 90 s", with the PC command |
| refused | `401`/`403` or `409` on the `POST` | "Not allowed from here" / "A restart is already running" |

An unknown result is never shown as success. Pinned by `tests/vendored/test_restart_sheet.py` (the real module under Node) and `tests/vendored/test_self_restart.py` (the routes, every runner ending, and a runner that outlives a `taskkill /T` of its parent).

### Adopter test notes

With the restart on, the readout is a control, and an adopter's tests see it:

- **The hit area grows upward only.** `restart-sheet.css` makes the 44px target with a `::after` that extends above the readout and never below it, so a footer flush at the window bottom (`margin-block-end: 0`) adds no page scroll and needs no `--hit-min` override. The target overlaps about 15px of whatever sits above the footer's own top margin. A tap-target helper that only reads a `::before` expansion does not see this `::after` one: read both (`tests/e2e/_geometry.py` `EFFECTIVE_RECT_JS` does).
- **The readout is focusable and a button.** It carries `tabindex="0"` and `role="button"`, and the closed restart sheet is a `<dialog>` in the page. A generic locator such as `dialog .detail-save-btn` or `.detail-dialog h2` also matches the closed restart dialog (a strict-mode collision), and a count of all `dialog` elements is no longer zero. Scope such locators to the dialog under test (an id or `[open]`).

## Required design tokens

| Token | Light value | Used for |
| --- | --- | --- |
| `--muted` | `#656d76` | readout text color |
| `--font-caption` | `0.75rem` | readout text size (falls back to `0.75rem`) |
| `--space-md` | `16px` | outer vertical margin (falls back to `16px`) |
| `--space-xs` | `4px` | outer horizontal margin (falls back to `4px`) |
| `--ink` | `#1f2328` | restart sheet status text |
| `--danger-text` | `#a40e26` | restart sheet warning and failure text |
| `--hit-min` | `44px` | the tappable readout's touch target (falls back to `44px`) |
| `--space-sm` | `8px` | restart sheet status and log spacing (falls back to `8px`) |

The font family has no token: design.md names it per type style (`system-ui, -apple-system, "Segoe UI", sans-serif`), so the CSS carries it literally. An app's own `--font` is deliberately not read.

## Don't diverge

`page-foot.css` / `page-foot.js` are vendored verbatim — to change the contract (text format, styling), change it **here in `project-scaffolding`** and re-vendor downstream. In particular, don't re-implement `fmtBuildTime` per-app — that private-reimplementation drift is exactly what whatsapp-radar#122 caught and this component removes. If your own CSS declares the same selector this file touches (e.g. `.app`, `.card`), use longhand properties or a disjoint media condition — a shorthand property at equal specificity is decided by source order, and can silently override a rule you didn't intend to touch. Streamlit POC spikes are exempt.
