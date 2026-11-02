# `page-foot` — the build-identity footer

The fleet's canonical **page-foot**: a centered, quiet footer line — `Build: <sha> · YYYY-MM-DD HH:MM` — showing which build the PWA is running. This is the difference between "the tray restarted" and "the *new* build is live" (see project-scaffolding's `CLAUDE.md` "Build-identity footer" convention): a `/healthz` 200 passes on a stale process, a matching `git_sha` does not. Normalized from home-automation's shipped footer; whatsapp-radar#122's first feedback item was precisely a footer-format divergence from a private re-implementation of the same text — this component is that one shared function, written once.

## Files

| File | Role |
| --- | --- |
| `page-foot.css` | Visual contract — centered layout, muted text, and its own pinned type: the design.md `caption` style (system-ui, 12px, 600, line-height 1.1) with tabular numerals. Inherits nothing from the app's body or container font, and doesn't depend on external `.muted`/`.small` utility classes. |
| `page-foot.js` | ESM — `fmtBuildTime(iso)` + `buildReadoutText(sha, builtAtIso)`, the one shared text formatter so the footer string can't drift between apps. |
| `page-foot.html` | Markup skeleton to copy and adapt. |

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
- **Type contract.** Family, weight, line-height, letter-spacing, style, case and numeral style are set by the component, so the line is the same under any app's body font. `tests/e2e/test_vendored_page_foot.py` mounts it under two different body typefaces and asserts equal computed style and equal pixels. Don't set `font-*`, `letter-spacing` or `text-transform` on `.page-foot` or its `<p>`.
- The `<p>`'s text is set entirely by `buildReadoutText()`; no additional class is needed (the styling is self-contained in `page-foot.css`, not borrowed from generic `.muted`/`.small` utilities).
- Any reload-on-stale-asset logic (e.g. home-automation's asset-hash comparison that triggers one automatic reload after a deploy) is orthogonal to the footer text and stays app-specific — out of scope here.

## Required design tokens

| Token | Light value | Used for |
| --- | --- | --- |
| `--muted` | `#656d76` | readout text color |
| `--font-caption` | `0.75rem` | readout text size (falls back to `0.75rem`) |
| `--space-md` | `16px` | outer vertical margin (falls back to `16px`) |
| `--space-xs` | `4px` | outer horizontal margin (falls back to `4px`) |

The font family has no token: design.md names it per type style (`system-ui, -apple-system, "Segoe UI", sans-serif`), so the CSS carries it literally. An app's own `--font` is deliberately not read.

## Don't diverge

`page-foot.css` / `page-foot.js` are vendored verbatim — to change the contract (text format, styling), change it **here in `project-scaffolding`** and re-vendor downstream. In particular, don't re-implement `fmtBuildTime` per-app — that private-reimplementation drift is exactly what whatsapp-radar#122 caught and this component removes. If your own CSS declares the same selector this file touches (e.g. `.app`, `.card`), use longhand properties or a disjoint media condition — a shorthand property at equal specificity is decided by source order, and can silently override a rule you didn't intend to touch. Streamlit POC spikes are exempt.
