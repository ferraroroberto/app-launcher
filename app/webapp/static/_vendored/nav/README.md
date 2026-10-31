# `nav` — floating bottom-tab navigation

The fleet's canonical **primary navigation**: a top segmented control on desktop that becomes a single floating bottom-tab pill on touch / installed-PWA. This is the navigation contract every fleet web app must *feel* identical on (see `~/.claude/design.md` → "Navigation & interaction").

## Files

| File | Role |
| --- | --- |
| `nav-tabs.js` | Behaviour. ESM module — `initNavTabs(opts)`. Discovers tabs/panes from the DOM, persists the active tab, keeps ARIA + roving `tabindex` in sync, and returns `{ setTab, getTab, setBadge }`. |
| `nav-tabs.css` | Visual contract. The desktop segmented control, the wide-layout left rail, the `@media (pointer: coarse)` floating pill, the optional count badge and the modal-hide rule. References design tokens only. |
| `nav-tabs.html` | Markup skeleton to copy and adapt (3 example tabs). |

## How to vendor

1. Copy this `nav/` folder **verbatim** into your app's static dir (e.g. `app/webapp/static/_vendored/nav/`). Do **not** edit `nav-tabs.js` / `nav-tabs.css` per-app — that is the drift this component removes. Per-app changes go in *your* markup and *your* token values only.
2. Paste the `nav-tabs.html` skeleton into your `index.html` and adapt the tabs (rename `data-tab` / `aria-controls` / ids, swap the SVG icon + label, add/remove `<button class="tab">` + matching `<section class="pane">` pairs). **Keep `<nav class="tabs">` as a direct `<body>` child, sibling to `<main class="app">`, never nested inside the content wrapper/scroller.** iOS installed PWAs can capture fixed-position descendants of scroll containers and anchor them to scrolled content instead of the physical viewport. `<main class="app">` still wraps your page content because the mobile stylesheet reserves bottom padding there so the floating bar never occludes content.
3. Link the CSS and set the tab count:
   ```html
   <link rel="stylesheet" href="/static/_vendored/nav/nav-tabs.css">
   ```
   ```css
   /* No tab-count variable is required: the mobile grid auto-fits 4–6 tabs. */
   ```
4. **Put the standalone page head in your `index.html` `<head>`.** The installed app starts with its final viewport, and the bar is in place from the first frame of a cold launch, only with the `default` status-bar style, painted in the page colour by `theme-color`:
   ```html
   <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
   <meta name="apple-mobile-web-app-status-bar-style" content="default">
   <meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
   <meta name="theme-color" content="#0d1117" media="(prefers-color-scheme: dark)">
   ```
   `black-translucent` is the wrong value: it lets the installed app grow its viewport after launch, so the bar rides that growth, and it leaves the status bar to be hidden by an app-drawn scrim that this component no longer ships. Desktop browsers and Playwright's WebKit don't reproduce the installed app, so assert the pair in your e2e smoke test rather than waiting for a phone to show it:
   ```python
   assert page.locator('meta[name="apple-mobile-web-app-status-bar-style"]').get_attribute("content") == "default"
   assert "viewport-fit=cover" in page.locator('meta[name="viewport"]').get_attribute("content")
   ```
5. Wire up the switcher once the DOM is ready:
   ```js
   import { initNavTabs } from '/static/_vendored/nav/nav-tabs.js';
   const nav = initNavTabs({
     storageKey: 'my-app.tab',   // omit to disable PWA persistence
     onChange: (tab) => { /* lazy-load that pane, change poll rate, … */ },
     navEvent: (msg) => { /* optional: wire to a nav-debug recorder for on-device forensics */ },
     scrollResetSelector: '.app', // element scroller reset to top on tab switch (default; null to skip)
   });
   ```

   `initNavTabs` also self-heals the pinned bar (home-automation #300/#303 lessons): it clears the browser-tab transform when a `<dialog>` scroll-lock engages (`scroll-lock:engaged`/`released` events — apps without scroll-lock.js simply never fire them), re-pins after `[hidden]` overlay toggles, on `visibilitychange`/`load`, and via a 400 ms watchdog. Standalone PWAs still never get a measured translate.

## Markup contract

```html
<nav class="tabs" role="tablist" data-active-tab="home">
  <button class="tab" data-tab="NAME" role="tab"
          aria-controls="PANE_ID" aria-selected="…"> … </button>
</nav>
<main class="app">
  <section id="PANE_ID" class="pane" role="tabpanel" aria-labelledby="…"> … </section>
</main>
```

Each `.tab` carries `data-tab` (its name) and `aria-controls` (the id of the pane it shows). Start every non-default `.pane` with `hidden` so there's no flash before JS runs. A tab may omit its pane (e.g. an external link) — only its button state toggles.

## Tab icons

Each `.tab` holds one `<svg class="tab-icon">` stroke glyph and one `<span class="tab-label">` — and the icon is visible on **both** surfaces: beside the label in the desktop segmented control, above it in the mobile pill. Give the SVG a `24 24` viewBox and `<path>`s with no `fill`/`stroke` attributes of their own; `nav-tabs.css` paints them (`fill: none; stroke: currentColor`) so they inherit the active/inactive tab colour. Desktop sizes the icon at `1.05em` of the label's font-size; the pill uses `--bottom-tabs-icon`. Every icon size is scoped as `.tabs .tab-icon`, so an app's own single-class icon utility (a `.icon { width: 1em }` you also put on the glyph) can't resize it, whatever order the stylesheets load in (#303). Below 520px on a fine pointer (a squeezed desktop window) the tab stacks the icon over its label, the pill's shape at the `--font-caption` size, since an icon beside a 7–8 letter label no longer fits five tabs there. The nav never goes icon-only (`design.md` navigation contract, fleet-config#966); the label ellipsizes only as a last resort (#278).

## Count badge (optional)

A small count on a tab says "something over there needs you" from every tab, so no page header has to repeat it (app-launcher's Board tab carries the number of sessions waiting on the user). It is opt-in: `initNavTabs` returns `setBadge(tab, count, noun?, tone?)` next to `setTab` / `getTab`, and a nav that never calls it renders exactly as before, with none of the badge elements in the DOM.

```js
const nav = initNavTabs({ storageKey: 'my-app.tab' });
nav.setBadge('board', 2, 'waiting');   // badge "2"; the tab's name reads "Board, 2 waiting"
nav.setBadge('board', 12, 'waiting');  // badge "9+"; the name still says "12 waiting"
nav.setBadge('board', 0);              // removes it (null, undefined and non-numbers do too)
nav.setBadge('security', 1, 'triggered', 'danger');  // the same badge in the danger tone
```

- `tab` is the tab's `data-tab`. An unknown tab is ignored, so a poll loop can call it on every refresh. Calling it again with a new count only updates the text.
- `count` 1 to 9 shows the digit, 10 or more shows `9+`. The accessible name keeps the real number.
- `noun` is optional: what the count counts. It is spoken, not painted: `", 2 waiting"` is appended to the label, so a screen reader announces the tab once, with its count (the painted digit is `aria-hidden`). Without a noun the name reads `Board, 2`.
- `tone` is optional: `'attention'` (the default) or `'danger'`. It is set as `data-tone` on the badge, and the CSS maps `danger` to the `--deficit` fill under the same `--card` text. Use `danger` for a count that is an alarm (a triggered alarm) and `attention` for one that is a to-do (detector trouble, sessions waiting). Any other value renders as `attention`, and omitting it renders exactly as before. A later call with a different tone repaints the existing badge.
- The badge sits on the icon's top-right corner in the phone pill, the inline desktop tabs and the wide left rail. The first badge on a tab wraps its icon in a `<span class="tab-icon-wrap">`, the box the badge is positioned against; removing the last one unwraps it again. The active tab's tint cannot hide it: the fill is opaque.
- Style (`design.md`): `rounded.pill`, 12px semibold tabular numerals, 16px minimum height, `attention` fill (or `danger`, see `tone`) with `card` text. The size is in px so it does not reflow with the Large text size. It is never animated. Contrast is 4.87:1 in light and 6.85:1 in dark for `attention`, and 5.36:1 in light and 5.16:1 in dark for `danger`, asserted in `tests/e2e/test_vendored_nav.py`; if your `--attention` / `--deficit` / `--card` values differ, re-measure.
- Needs the `--attention` token (below), plus `--deficit` if you use the `danger` tone. They are read only while a badge is shown.

## Wide layout: left rail

At `(min-width: 1100px) and (pointer: fine)` (`design.md` `layout.wide`, fleet-config#968) the segmented control becomes a **left rail**: `layout.rail` (80px) wide, full height, on the `card` surface with a `line` hairline on its right edge. It shows the same tabs stacked top to bottom, each an `--icon-feature` glyph over a `--font-caption` label, never icon-only. Only the placement changes. The active tint, `aria-selected` and persistence are the same rules, and the markup is the same skeleton. The file offsets your content past the rail itself: `body:has(> .tabs)` gets `padding-left: var(--layout-rail)`, and `.tabs ~ .app` gets the top gap the sticky control used to supply. Both are keyed on the nav so they outrank an app's own `body` / `.app` padding shorthand loaded after this file. Below 1100px the control keeps the `layout.measure` column (`--layout-measure`, 772px); on a coarse pointer nothing changes at any width. Master-detail and a board's full-width exception are app layout, not nav, so they stay in your CSS (#281, lifted from app-launcher#1166).

## Bottom reserve and hit area (phone)

On the phone pill (`(pointer: coarse) and (max-width: 520px)`), `nav-tabs.css` sets `.app`'s `padding-bottom` and `scroll-padding-bottom` to one reserve:

```
env(safe-area-inset-bottom) + --bottom-tabs-margin + --bottom-tabs-height + --bottom-tabs-margin + --gap
```

That is 21 + 61 + 21 + 12 = 115px with no safe area, plus the home-indicator inset on an iPhone. Scrolled to the end, the last row's bottom edge sits at least `--bottom-tabs-margin + --gap` (33px) above the pill, so no tap rectangle of the last row meets a tab's. Do not add your own bottom padding for the nav on top of it.

Above the end, rows scroll under the floating pill by design, so the pill must win every tap in each tab's rectangle. Tabs are rectangles, but the bar's corners are rounded (`--radius-nav`), and a rounded corner takes no taps outside its curve. A transparent `.tabs::before` layer fills the bar's square padding box under the tabs, so a tap in an outer tab's corner sliver lands on the nav, not on the row beneath. It is never painted and exists only on the phone pill: the desktop control and the wide rail have no such layer (#348, design rubric TOUCH-02). The layer is the nav's own `::before`, so don't put an app rule on `.tabs::before`.

## Pill fade

Rows scroll under the floating pill, so without a fade they would show through its clear glass hard-edged. On the phone pill, `nav-tabs.css` adds one inert `body::before` band anchored to the screen bottom like the pill itself, above `.app` and below the pill (`z-index: 110`, `pointer-events: none`): transparent from 16px above the pill's top edge, the page background `--bg` from the pill's bottom edge to the screen edge. The bottom reserve still scrolls the last row fully clear of the fade, and every tap passes through the band. Reduce Transparency (`prefers-reduced-transparency: reduce`) drops both bands. A matching `body::after` band fades content out at the top, the same look as the shared nav under a translucent status bar: the opaque `default` status bar (painted in the page colour by `theme-color`) is the solid part, and the band draws only the eased 4px tail below it, inside the `.app` top padding, so the first card at scroll-top starts where the fade ends and is never dimmed.

It is CSS only, with no transform on the nav. Desktop and the wide rail carry no such layer. The bands are the nav's own `body::before` and `body::after`, so don't put an app rule on either. `tests/e2e/test_nav_offset.py` checks both bands' geometry on a 390x844 phone; the real look is confirmed on an iPhone.

## Required design tokens

`nav-tabs.css` references these CSS custom properties — define them in your app's `:root` / `[data-theme="dark"]` blocks, **wired to `~/.claude/design.md` (+ `design.dark.md`)**. Don't copy the spec; point your tokens at it. Reference values (light) from the canonical implementation. The mobile geometry below is the phone-validated standard promoted from `home-automation` issue #118: measured from 1290px-wide iPhone screenshots of the GitHub/VLC apps (~3x CSS pixels), then validated live on Roberto's iPhone.

| Token | Light value | Used for |
| --- | --- | --- |
| `--bg` | `#ffffff` (dark `#0d1117`) | pill fade |
| `--card` | `#ffffff` | tab bar surface (desktop) |
| `--accent-soft` | `color-mix(in srgb, var(--accent) 16%, transparent)` | active-tab fill, desktop and mobile (fleet-config#963) |
| `--accent-border-soft` | `color-mix(in srgb, var(--accent) 24%, transparent)` | active-tab border (mobile) |
| `--accent-text` | `#0550ae` (dark `#58a6ff`) | active-tab text/icon (text on the `accent-soft` tint, fleet-config#963) |
| `--attention` | `#9a6700` (dark `#d29922`) | count-badge fill; `--card` is its text — only read when `setBadge` shows a badge |
| `--deficit` | `#cf222e` (dark `#f85149`) | count-badge fill in the `danger` tone; `--card` is its text — only read when `setBadge` shows a `danger` badge |
| `--muted` | `#656d76` | inactive-tab text |
| `--line` | `#d1d9e0` | bar border, active-tab border (mobile) |
| `--space-xs` | `4px` | bar padding / gap (desktop) |
| `--gap` | `12px` | last term of the phone bottom reserve (see "Bottom reserve and hit area"); phone `.app` side padding (the phone top padding is `env(safe-area-inset-top)` alone, #288) |
| `--font-label` | `0.875rem` | tab label (desktop) |
| `--font-caption` | `0.75rem` | tab label (narrow desktop) |
| `--row-sm` | `44px` | stacked narrow-desktop tab min-height (`hit-target.min`) |
| `--row-lg` | `60px` | rail tab min-height |
| `--space-sm` | `8px` | rail tab padding |
| `--icon-feature` | `24px` | rail icon size (`icons.size.feature`) — optional, falls back to `24px` |
| `--layout-measure` | `772px` | desktop column the control spans — optional, falls back to `772px` |
| `--layout-rail` | `80px` | wide-layout rail width + content offset — optional, falls back to `80px` |
| `--radius-md` | `12px` | bar corners (desktop) |
| `--radius-pill` | `9999px` | tab corners |
| `--radius-nav` | `30px` | floating bar corners (mobile) |
| `--bottom-tabs-height` | `61px` | floating bar height — optional, falls back to `61px` |
| `--bottom-tabs-margin` | `21px` | floating bar inset from left/right/physical bottom — optional, falls back to `21px` |
| `--bottom-tabs-pill-height` | `53px` | per-tab pill height (mobile) — optional, falls back to `53px` |
| `--bottom-tabs-padding` | `4px` | mobile bar inner padding — optional, falls back to `4px` |
| `--bottom-tabs-gap` | `4px` | mobile tab gap — optional, falls back to `4px` |
| `--bottom-tabs-icon` | `20px` | mobile SVG icon size — optional, falls back to `20px` |
| `--bottom-tabs-label` | `11px` | mobile label font size — optional, falls back to `11px` |
| `--tabbar-bg` | `rgba(255,255,255,0.62)` (dark `rgba(42,45,52,0.55)`) | floating bar clear-glass fill |
| `--tabbar-border` | `rgba(0,0,0,0.08)` (dark `rgba(255,255,255,0.14)`) | floating bar hairline rim |
| `--tabbar-pill` | `rgba(0,0,0,0.07)` (dark `rgba(255,255,255,0.14)`) | the sliding capsule behind the active tab; **required**, with it undefined the active tab has no capsule |
| `--tabbar-shadow` | `0 8px 28px -6px rgba(20,24,40,0.2), 0 1px 4px rgba(20,24,40,0.06)` (dark `0 10px 30px -6px rgba(0,0,0,0.6)`) | floating bar drop shadow |

The tokens marked optional (`--icon-feature`, `--layout-measure`, `--layout-rail`, `--bottom-tabs-height`, `--bottom-tabs-margin`, `--bottom-tabs-pill-height`, `--bottom-tabs-padding`, `--bottom-tabs-gap`, `--bottom-tabs-icon`, `--bottom-tabs-label`) are optional per-context knob settings: `nav-tabs.css` reads each with an inline fallback, so an app that leaves one undefined gets the fallback named in its row and nothing renders wrong. Define one only to override it. Every unmarked token is read with no fallback and must be defined.

## The modal-hide rule

The floating bar hides whenever a modal is open so it never floats over a dialog. It uses `visibility: hidden` rather than `display: none` so the fixed layer stays in the render tree across modal open/close, which is gentler on iOS PWA repainting:

- Any native `<dialog open>` → handled automatically (`body:has(dialog[open]) .tabs`).
- A non-`<dialog>` overlay (e.g. a custom login screen) → add the class `nav-hidden` to `<body>` while it's open.

## Pinning the bar (iOS) — CSS-first, body-level nav

Hard-won contract, validated extensively on a real iPhone (`home-automation` #205/#214/#229/#232). The short version: **in an installed PWA the floating bar is pinned by CSS alone, and the nav must be outside the content wrapper/scroller.**

- **Body-level nav is load-bearing.** `nav-tabs.html` places `<nav class="tabs">` as a direct `<body>` child before `<main class="app">`. Do not move it inside `.app`. iOS can capture a fixed-position element nested inside a momentum/content scroller and position it against the scrolled content bottom; on a short tab that makes the bar float mid-screen. A body-level sibling pins against the viewport instead.
- **Standalone PWA → no measured JS transform.** `nav-tabs.css` positions the bar `fixed; bottom: …`, which is correct on its own in a fullscreen installed PWA (no browser chrome). The VisualViewport transform only ever existed to chase Safari's collapsing *browser* toolbar; in standalone every measured offset eventually risks stranding the bar **up** because iOS's layout and rendered geometry disagree there. So `initNavTabs` detects `display-mode: standalone` / `navigator.standalone` and never applies a measured translate.
- **Browser tab → minimal transform.** Only in a real browser tab (where the toolbar genuinely collapses) does it translate the bar up by the hidden slice — clamped to a toolbar's height (~160px) and suppressed while the soft keyboard is up (a focused field, or a viewport shrink past a toolbar's worth). Desktop's sticky top control is untouched; feature-detected on `window.visualViewport`.
- **Force the page scrollable.** `nav-tabs.css` sets `html { min-height: calc(100dvh + 1px) }` on the phone pill. An installed iOS app shrinks its viewport 59px whenever the document can't scroll (a tab shorter than the screen, or the body pinned by `scroll-lock.js` under an open dialog) and grows it back 1px at a time over ~3s, painting nothing below the moving edge. The rule is on the root, not `.app`, so it still holds while the body is pinned. (Cost: a barely-perceptible scroll on short tabs.)
- **Standalone anchors the bar from the top.** Document scroll is the same in a browser tab and the installed app, but in `@media (display-mode: standalone)` the bar and its bottom fade are placed with `top: calc(100lvh - …)`: iOS still grows the installed app's viewport after a cold launch, and `100lvh` is the final height from the first frame, so the bar never rides that growth. A browser tab keeps `bottom:`, since there `lvh` would put the bar under the toolbar.
- **The page head is part of the contract.** The top anchor and the bottom fade assume the installed app's web view starts with its final height, which holds with `apple-mobile-web-app-status-bar-style: default` plus `viewport-fit=cover` (How to vendor, step 4). `tests/test_nav_standalone_head.py` pins that head in the skeleton, the gallery and this README.
- **An app that pins its body under dialogs needs the same `scroll-lock.js` change.** Neither the nav nor `modal/` carries standalone-shell code any more, and `initNavTabs` no longer listens for `scroll-lock:engaged` / `scroll-lock:released`. An app-side `scroll-lock.js` that pins the body with `position: fixed` while a `<dialog>` is open must therefore be the plain pin (save `scrollY`, pin at `-scrollY`, restore on close) with no "standalone shell" branch and no event dispatch; it keeps working because the `html` min-height rule above leaves the document scrollable even while the body is pinned. home-automation's `scroll-lock.js` is the reference; the script is app code and is not vendored here.

**Recommended app-level hardening:**

- Set `overscroll-behavior: none` on `html, body` to kill rubber-band drag at the document edges. This lives in the consuming app, not the component, because the document scroller (`body`) is the app's.

## Don't diverge

`nav-tabs.js` and `nav-tabs.css` are **vendored verbatim** — the same "copy byte-for-byte, never edit per-app" rule as the tray's `single_instance.py` / `tray_lifecycle.ps1`. If the contract needs to change, change it **here in `project-scaffolding`** and re-vendor downstream; don't fork it in a consuming app. If your own CSS declares the same selector this file touches (e.g. `.app`, `.card`), use longhand properties or a disjoint media condition — a shorthand property at equal specificity is decided by source order, and can silently override a rule you didn't intend to touch. Streamlit POC spikes are exempt — they don't serve this PWA shell.
