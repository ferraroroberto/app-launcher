# `icons` — Lucide icon sprite + helper

The fleet's canonical **icon component**: one inline SVG sprite of the handful of [Lucide](https://lucide.dev) glyphs an app actually uses, plus a one-line JS helper to reference them. Lucide is the fleet-wide icon set (see `~/.claude/design.md` → "Icons") — 24×24 grid, 2px outline stroke, ISC-licensed, no web-font payload. Normalized from `ferraroroberto/home-automation#77`, the fleet's first Lucide adopter.

## Files

| File | Role |
| --- | --- |
| `icons-sprite.html` | The inline `<svg>` sprite — a set of `<symbol id="i-NAME">` glyphs. Copy-paste partial; paste once near the top of your `<body>`. Trim it to the glyphs your app uses. |
| `icons.js` | Behaviour. ESM module — `icon(name, extraClass)` returns the `<svg class="icon"><use href="#i-NAME"></use></svg>` string for use from JS. |

## How to vendor

1. Copy this `icons/` folder **verbatim** into your app's static dir (e.g. `app/webapp/static/_vendored/icons/`). Do **not** edit `icons.js` per-app. The only per-app change is **which `<symbol>` glyphs** you keep in `icons-sprite.html` — add the ones you need (paste the matching glyph's paths from [lucide.dev](https://lucide.dev), keeping the `i-NAME` id + `fill="none"`), remove the ones you don't. Don't bulk-import the whole library.
2. Paste the entire `<svg>` block from `icons-sprite.html` near the top of your `<body>` — once per page. It renders nothing (it's a 0-size hidden sprite); it just makes the symbols referenceable.
3. Add the **required `.icon` CSS contract** (below) to your stylesheet.
4. Reference glyphs:
   - **Static markup:** `<svg class="icon"><use href="#i-house"></use></svg>`
   - **From JS:**
     ```js
     import { icon } from '/static/_vendored/icons/icons.js';
     el.innerHTML = icon('house');            // <svg class="icon" …>
     el.innerHTML = icon('snowflake', 'tab-icon');  // extra class appended
     ```

## Markup contract

```html
<svg class="icon [extra-class]" aria-hidden="true"><use href="#i-NAME"></use></svg>
```

`NAME` is the glyph id without the `i-` prefix (`house`, `snowflake`, …). Icons are **decorative**: keep `aria-hidden="true"` and let the visible label / an existing `aria-*` on the parent carry the meaning. `icon()` sets `aria-hidden` for you.

## Required CSS contract

Unlike `nav/`, this component needs **no design tokens** — icons inherit `currentColor`, so they recolor for free in light + dark. It needs one small CSS rule, the `.icon` class the sprite's stroke styling is inherited from. Add it to your stylesheet verbatim (size with `width`/`height` or `font-size` at the call site):

```css
.icon {
  display: inline-block;
  width: 1em;
  height: 1em;
  flex: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
  vertical-align: -0.15em;
}
```

`stroke: currentColor` is what makes a glyph take the colour of its surrounding text — set the parent's `color` (or a token) and the icon follows. `fill="none"` lives on each `<symbol>` (not here) so the rare filled sub-elements — e.g. the `palette` dots, which set their own `fill="currentColor"` — survive.

## Why the sprite is inline (load-bearing — do not "tidy")

The sprite ships **in-document**, not as an external `/static/icons.svg` referenced with `<use href="icons.svg#i-NAME">`. This is deliberate and load-bearing:

- **iOS Safari does not resolve external `<use>` references** — `<use href="file.svg#id">` fails *silently* on iPhone/iPad, so every icon would vanish on the fleet's primary (phone) surface with no error. Inlining the symbols is the only reliable cross-engine path.
- The sprite is hidden via the **`width=0 height=0` + `position:absolute`** idiom, **not** `display:none` — some engines suppress `<use>` rendering when the source `<symbol>` is inside a `display:none` subtree.

A future app must not move this to an external file to "tidy up" — it will look fine on desktop and break on every iPhone.

## Don't diverge

`icons.js` and the `icons-sprite.html` glyph definitions are **vendored verbatim** — the same "copy byte-for-byte, never edit per-app" rule as `nav/` and the tray's `single_instance.py` / `tray_lifecycle.ps1`. To add a glyph to the shared set or change the helper, change it **here in `project-scaffolding`** and re-vendor downstream; don't fork it in a consuming app. Streamlit POC spikes are exempt — they don't serve this PWA shell.
