# `build-watch` — the "New version available · Reload" prompt

Tells a page that stayed open across a deploy that the server now runs a newer build, and offers a **Reload**. On an installed iPhone PWA the app sits in memory across restarts and keeps the old shell and modules; without this prompt the only way to pick up a deploy is to force-quit it. The prompt never reloads by itself, so an unsent draft survives. Normalized from app-launcher's `build-watch.js`.

## Files

| File | Role |
| --- | --- |
| `build-watch.js` | `initBuildWatch(opts?)` plus the pure decisions it runs on: `verdict(boot, live)`, `shouldCheck(now, lastOk, throttle)`, `stampOf(url)`, and the `CHECK_THROTTLE_MS` / `CHECK_INTERVAL_MS` cadence. |
| `build-watch.css` | Lays the prompt out on the vendored `toast`'s glass and lifts it one toast-height above where a routine toast lands. |

## Contract

- **Boot build.** The `?v=` stamp the app's static versioning puts on its `/static` URLs, read in this order: the page's entry `<script type="module" src>`, then the module's own `import.meta.url` (an app that rewrites JS imports stamps it), then the first stamped `<script src>` or stylesheet `<link href>`. The shell is served `no-cache`, so those stamps are the build the page booted on. No stamp anywhere (a dev tree, an app without static versioning) → `initBuildWatch()` returns `false` and does nothing.
- **Live build.** `GET /api/version` (or `opts.endpoint`) returns `asset_hash` equal to the entry module script's `?v=` stamp, beside the `git_sha`/`built_at` the build-identity footer and the restart recipes already read. With one fleet hash (`src/static_versioning.py`) every stamp is that hash; an app that stamps each file with its own hash reports its entry script's. An app whose version endpoint lacks `asset_hash` adds it. A literal `"missing"` counts as unknown.
- **Cadence.** A check when the page becomes visible, skipped while the last good read is under 60 s old, and one every 5 minutes while the page stays visible. A failed read does not count against the throttle, so the next trigger retries it.
- **Verdict.** Stale only when both hashes are known and differ. Unknown is never stale: a missing stamp, an empty, absent or `"missing"` `asset_hash`, a 401, a non-2xx answer or a network failure shows nothing.
- **Prompt.** One `div#buildPrompt.toast.build-prompt` appended to `<body>`, `role="status"`, `aria-live="polite"`: the text "New version available", a `button-primary` **Reload** (`data-track="build.reload"`) and an `icon-button` dismiss (`data-track="build.dismiss"`, an inline Lucide `x`, so no icon sprite is needed). Reload is only ever the user's tap (`location.reload()`). A dismissed prompt stays down until the server moves to yet another build; a server back on the boot build hides it.

## How to vendor

1. Copy this `build-watch/` folder **verbatim** into `app/webapp/static/_vendored/build-watch/`. It needs the vendored `toast/`, `button/` and `icon-button/` stylesheets on the page.
2. Link the stylesheet after `toast.css`, and start the watch once at boot:
   ```html
   <link rel="stylesheet" href="/static/_vendored/build-watch/build-watch.css">
   ```
   ```js
   import { initBuildWatch } from './_vendored/build-watch/build-watch.js';
   initBuildWatch();
   ```
3. If the app's API reads add their own auth (a bearer token from storage), pass the app's reader: `initBuildWatch({ fetchJson: jsonApi })`. It must resolve to the parsed body and reject on a non-2xx answer. The default reader is a same-origin `fetch` with `cache: 'no-store'`.
4. Make sure `/api/version` returns `asset_hash`, and that the version route answers the page's own session (the default reader sends cookies).

## Required design tokens

Those of `toast` (`--tabbar-bg`, `--tabbar-border`, `--ink`, `--radius-md`, `--font-label`, `--shadow`), `button` and `icon-button`, plus:

| Token | Value | Used for |
| --- | --- | --- |
| `--hit-min` | `44px` | the Reload button's minimum height (falls back to 44px) |
| `--bottom-tabs-height` / `--bottom-tabs-margin` | `61px` / `21px` | optional; fall back to the nav's own defaults |

## Don't diverge

`build-watch.js` and `build-watch.css` are vendored verbatim. To change the cadence, the copy or the verdict, change them **here in `project-scaffolding`** and re-vendor downstream. Pinned by `tests/vendored/test_build_watch.py`, which drives the real module under Node. Streamlit POC spikes are exempt.
