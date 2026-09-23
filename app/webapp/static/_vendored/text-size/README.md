# `text-size` — the text-size setting and its pre-paint boot

The fleet's canonical **text-size setting** (`~/.claude/design.md` → "Layout" → "Text size", `ferraroroberto/fleet-config#967`). Every fleet PWA locks the viewport (`maximum-scale=1, user-scalable=no`), which fails WCAG 1.4.4 (Resize Text) unless the app offers its own way to enlarge text. This is that escape: a persisted **Small / Default / Large** setting that scales the root `font-size` (93.75% / 100% / 112.5%), stamped before first paint by the same boot script that stamps the theme (`project-scaffolding#276`).

## Files

| File | Role |
| --- | --- |
| `text-size-boot.html` | The inline `<head>` boot script. Stamps `html[data-theme]` from `<app>.theme` (else `prefers-color-scheme`) and `html[data-textsize]` from `<app>.textsize` (else `default`). Replaces the app's own theme boot script. |
| `text-size.css` | The three `html[data-textsize]` → root `font-size` steps. |
| `text-size.js` | `bindTextSize(group, app)` wires the control; `readTextSize(app)`, `applyTextSize(size)` and `TEXT_SIZES` are exported too. |
| `text-size.html` | The control: a `range-tabs` row with three `data-textsize` buttons, and the one-line binding. |

## How to vendor

1. Copy this `text-size/` folder **verbatim** into your app's static dir (`app/webapp/static/_vendored/text-size/`). Do **not** edit `text-size.css` or `text-size.js` per-app.
2. **Boot:** paste the `<script>` from `text-size-boot.html` inline in `<head>`, before any stylesheet link, **in place of** your existing theme boot script. Set its `app` variable to your localStorage prefix (e.g. `'app-launcher'`): the same prefix your theme toggle writes `<app>.theme` under. This is the one per-app edit.
3. Link the CSS (order doesn't matter):
   ```html
   <link rel="stylesheet" href="/static/_vendored/text-size/text-size.css">
   ```
4. **Control:** paste `text-size.html` into your Settings, with the vendored `range-tab` component linked for its look, and bind it with the same prefix:
   ```js
   import { bindTextSize } from '/static/_vendored/text-size/text-size.js';
   bindTextSize(document.getElementById('textSizeControl'), 'app-launcher');
   ```

## Contract

- **Key and values:** `localStorage['<app>.textsize']` is `small`, `default` or `large`. Anything else, including an unreadable store, falls back to `default`, in both the boot script and `readTextSize`.
- **Pre-paint:** the boot script runs inline in `<head>`, so the first paint already uses the stored size and theme; nothing reflows. Don't move it to a `<script src>`, which would block first paint on a request.
- **What scales:** the root `font-size`, so every rem-based type role (`--font-body`, `--font-body-sm`, `--font-label`, …) scales with it.
- **What doesn't scale:** geometry stays in **px**. That covers the nav pill, the `rows` heights, the 44px hit target, the `control` height (`--control-h`) and the icon sizes. Never express geometry in rem, or the Large step reflows the nav and every row along with the text.
- **Selected step:** `bindTextSize` puts `.active` (the range-tab selected look) and `aria-pressed="true"` on the stored step's button, and keeps them in step on every click.

## Required design tokens

None of its own. The steps are the spec's `text-size.small` / `default` / `large` values, fixed in `text-size.css`. The control borrows `range-tab`'s tokens (see that README).

## Don't diverge

`text-size.css` and `text-size.js` are vendored verbatim, and the boot script differs per app only in its `app` prefix. To change the steps or the key, change them **here in `project-scaffolding`** (after the spec changes in `fleet-config`'s `design.md`) and re-vendor downstream. Keep one boot script per page: two scripts stamping the theme will disagree the moment their keys drift. Streamlit POC spikes are exempt.
