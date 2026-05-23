# Cache hygiene: content-hash asset stamping + /api/version

**Issue:** [#30](https://github.com/ferraroroberto/app-launcher/issues/30) — Phone-validation 1/5.

## What changed

Iphone PWA cache nondeterminism was the source of most "shipped broken, had
to revert" incidents. Three defects were attacking it from different angles:

- Manual `?v=N` query strings on `styles.css` + `main.js`, with nothing on
  the other ES-module JS files at all.
- No explicit `Cache-Control` on `/static/*` — iOS Safari heuristic-cached
  everything except the index.
- No way to confirm *which build* the phone was running.

Now:

- Every `.js` / `.css` under `app/webapp/static/` (excluding `vendor/`) is
  hashed at app startup. A single **fleet hash** (sha256 over the sorted
  per-file hashes, first 8 hex chars) is stamped onto every asset URL —
  both in `index.html` and on the ES-module `import './foo.js'` lines
  inside JS files (rewritten at serve time, not on disk).
- The static mount is now a `_VersionedStatic(StaticFiles)` subclass that
  sets `Cache-Control: public, max-age=31536000, immutable` on hashed
  assets, `public, max-age=86400` on icons / manifest, defaults elsewhere.
  Safe to be aggressive because the URL changes on edit.
- `index.html` keeps its existing `no-cache, must-revalidate` so the
  hashed asset URLs the phone resolves are always fresh.
- New `GET /api/version` returns `{git_sha, built_at, asset_hash}`. Cached
  at module import; pythonw-safe subprocess (`stdin=DEVNULL`,
  `CREATE_NO_WINDOW`, `git -C <root>`). The SPA fetches it via `jsonApi`
  (auth-gated like every other API) and renders `Build: <sha> · <ts>` at
  the bottom of the Settings panel — visible proof of which build the
  phone is running.

## Why fleet-hash, not per-file hash

The ES-module graph contains a cycle (`sessions.js ↔ terminal.js`), so
per-file transitive hashing would need SCC handling. With ~10 small files
totalling ~150 KB, the cost of invalidating all asset URLs on any edit
is well under a second on LTE — not worth the complexity.

## How it works in practice

The day-to-day loop, for anyone editing static files later:

1. Edit any `.js` / `.css` under `app/webapp/static/` (not `vendor/`).
2. Restart the `:8445` webapp. The fleet hash is computed **once at
   startup** — not per request — so an unrestarted process keeps serving
   the old hash even though the files on disk changed.
3. The phone re-fetches `index.html` (it is served `no-cache,
   must-revalidate`, so it is never stale). The server stamps the new
   `?v=<hash>` onto every asset URL **at request time** — the query
   string is not written to the file on disk.
4. Every asset URL the phone resolves is one it has never cached, so it
   downloads the new bytes. Unchanged files keep their previous URL and
   stay served from the phone's cache.
5. Confirm the build that is actually live with `GET /api/version` —
   `asset_hash` should have changed; `git_sha` should match `HEAD`.

The hash *is* the content: it changes if and only if the bytes change.
The only manual step left is the restart in step 2.

## Why this beats manual `?v=N`

The pre-#30 scheme — hand-bumping `styles.css?v=18 → ?v=19` in
`index.html` — was the source of most "shipped broken, had to revert"
incidents. Content-hash stamping removes three distinct failure modes:

| Manual `?v=N` | Content-hash stamping |
|---|---|
| **You must remember to bump it.** Forget, and the phone serves stale CSS/JS against new HTML — the exact bug `test_iphone_revalidate.py` pins. | Impossible to forget — the hash is derived from the file bytes. |
| **Only `styles.css` + `main.js` carried a `?v=`.** The other ES-module files (`terminal.js`, `state.js`, …) had none, so editing them shipped nothing to the phone. | Covers every `.js`/`.css`, including the `import` lines between modules. |
| **`N` is arbitrary.** It records "I bumped it," not "the content changed" — a bump with no change still busts cache; a change with no bump does not. | The hash tracks content exactly: no false busts, no missed ones. |

Consequence for later work: **do not reintroduce `?v=N` query strings in
`index.html`.** They are redundant with the fleet hash and reintroduce
the manual step this mechanism exists to kill. Issue #36's body asked to
bump `?v=19`; that advice predated #30 and was correctly skipped — the
#36/#37 static-file changes (May 2026) cache-busted automatically via
the fleet hash with no version number touched anywhere.

## Files modified

- `src/static_versioning.py` *(new)* — pure helpers (`compute_asset_hashes`,
  `rewrite_js_imports`, `rewrite_index_html`).
- `app/webapp/server.py` — `_VersionedStatic` subclass; hash map computed
  in `create_app()` and stashed on `app.state`.
- `app/webapp/routers/misc.py` — `/` rewrites index URLs at request time;
  new `/api/version` route.
- `app/webapp/static/index.html` — drops hardcoded `?v=18`; adds
  `<p id="buildReadout">` in Settings.
- `app/webapp/static/main.js` + `state.js` — `fetchVersion()` renders
  the build line via `jsonApi`.
- `tests/test_webapp_api_basics.py` — four new asserts (index `no-cache`,
  asset URL stamping, immutable Cache-Control on JS, import rewriting,
  `/api/version` shape).

## Validation

- `pytest -q --ignore=tests/e2e` → 65 passed.
- Live tray smoke via curl on loopback: `/api/version` returns the
  expected shape; `/static/main.js` returns `Cache-Control: public,
  max-age=31536000, immutable` with 8 ES-module imports stamped.
- End-to-end via Playwright (headless Chromium) against the live tray
  with `?token=…`: `#buildReadout` reads `Build: <sha> · <ts>` after boot.
- Phone validation: edit `styles.css` (visible colour swap) → restart
  tray → open the PWA cold → the change appears within one normal app
  open. Settings panel shows the current SHA + timestamp.

## Out of scope (deliberately)

- Service-worker offline cache — separate, much larger change.
- Build-time bundler (Vite / esbuild) — vanilla JS by design.
- CI hooks for the validation pipeline — comes in Part 4 (#33).
