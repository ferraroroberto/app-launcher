# `nav` — floating bottom-tab navigation

The fleet's canonical **primary navigation**: a top segmented control on desktop that becomes a single floating bottom-tab pill on touch / installed-PWA. This is the navigation contract every fleet web app must *feel* identical on (see `~/.claude/design.md` → "Navigation & interaction").

## Files

| File | Role |
| --- | --- |
| `nav-tabs.js` | Behaviour. ESM module — `initNavTabs(opts)`. Discovers tabs/panes from the DOM, persists the active tab, keeps ARIA + roving `tabindex` in sync, switches on a tap, on the arrow keys, Home and End, or on a press-and-slide across the phone pill, and returns `{ setTab, getTab, setBadge }` (`setTab(null)` clears the selection for a screen that is not a tab). |
| `nav-tabs.css` | Visual contract. The desktop segmented control, the wide-layout left rail, the `@media (pointer: coarse)` floating pill and its sliding capsule, the optional count badge and the modal-hide rule. References design tokens only. |
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
4. **Put the standalone page head in your `index.html` `<head>`.** It is the page half of the translucent top-bar recipe validated on the installed iPhone app: the `black-translucent` status-bar style with `viewport-fit=auto`, and `theme-color` following the page colour. The nav's top glass (see "Scroll-edge fades") is the other half:
   ```html
   <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=auto">
   <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
   <meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
   <meta name="theme-color" content="#0d1117" media="(prefers-color-scheme: dark)">
   ```
   On the device this launches the installed app's web view below the status bar and above the home indicator, with every `env(safe-area-inset-*)` at 0, and `theme-color` paints the status bar in the page colour. The pill's bottom offset is measured from that edge: under `viewport-fit=cover` the web view runs under the home indicator and the pill lands on the screen's bottom edge, so the app no longer matches the rest of the fleet. Desktop browsers and Playwright's WebKit don't reproduce the installed app, so the gate holds the pair instead of a phone: list `"nav-standalone-head"` in `.fleet.toml` `[conventions_ratchet] rules` (`docs/conventions-ratchet.md` in project-scaffolding), and every page that links `nav-tabs.css` fails the gate without it.
5. Wire up the switcher once the DOM is ready:
   ```js
   import { initNavTabs } from '/static/_vendored/nav/nav-tabs.js';
   const nav = initNavTabs({
     storageKey: 'my-app.tab',   // omit to disable PWA persistence
     onChange: (tab) => { /* lazy-load that pane, change poll rate, … */ },
     defaultTab: 'home',         // tab to open when nothing is stored (default: the first tab)
   });
   ```

   Every switch scrolls the window to the top: the document is the only scroller. The options are `navSelector` (default `.tabs`), `storageKey`, `onChange` and `defaultTab`; there are no others.

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

## Keyboard

The tablist follows the WAI-ARIA tabs pattern with automatic activation. Exactly one tab is in the Tab order, the open one (roving `tabindex`: `0` on it, `-1` on the rest), so Tab enters the bar on the open tab and the next Tab leaves it.

- **Right Arrow** and **Down Arrow** move focus to the next tab and open it; **Left Arrow** and **Up Arrow** move to the previous one. Past either end it wraps. Up and Down are accepted too because the wide layout stacks the tabs vertically.
- **Home** and **End** move to the first and last tab.
- A tab an app has hidden (not laid out) is skipped. A key with Alt, Ctrl, Meta or Shift held is left to the browser.
- Opening a tab this way is the same switch as a tap: `aria-selected`, the pane, `data-active-tab`, the stored tab and `onChange` all change once. Focus stays on the tab and the page is not scrolled to bring it into view.
- Enter and Space on the focused tab click it, as on any button.

`tests/e2e/test_vendored_nav.py` drives the keys in the desktop control and in the phone pill.

## A screen that is not a tab

When the app shows a screen no tab owns (Settings, an archive, a detail opened from elsewhere), call `setTab(null)`. It is the one supported way to say "no tab active"; never clear `.active` or `aria-selected` on the buttons yourself, because the nav paints those from its own state and puts them back on the next re-layout.

- No tab carries the accent, `aria-selected="true"` or the phone pill's capsule, `data-active-tab` is removed, and `getTab()` returns `null`.
- The nav keeps that state, through rotations and tab cells re-laying out, until the next real selection: a tap, a slide, a key or `setTab('<name>')`. The capsule then appears on that tab without springing from where it last was.
- Nothing else changes. The panes, the stored tab, the scroll position and `onChange` are the app's to handle, since the app opened the screen; a reload still restores the last real tab.
- The bar stays in the Tab order: its first laid-out tab holds the one tab stop, and the arrow keys open tabs from there.
- Any other unknown name, `undefined` included, still selects the default tab, as before.

`tests/e2e/_nav_state_checks.py` selects a tab, sets no tab, and asserts that none is active in the desktop control and in the phone pill.

## Tab icons

Each `.tab` holds one `<svg class="tab-icon">` stroke glyph and one `<span class="tab-label">` — and the icon is visible on **both** surfaces: beside the label in the desktop segmented control, above it in the mobile pill. Give the SVG a `24 24` viewBox and `<path>`s with no `fill`/`stroke` attributes of their own; `nav-tabs.css` paints them (`fill: none; stroke: currentColor`) so they inherit the active/inactive tab colour. Desktop sizes the icon at `1.05em` of the label's font-size; the stacked layouts (the rail and a squeezed desktop window) use `--icon-feature`, and the phone pill fixes it at 24px, the iOS tab-bar glyph size, so the 11px label cannot shrink it. Every icon size is scoped as `.tabs .tab-icon`, so an app's own single-class icon utility (a `.icon { width: 1em }` you also put on the glyph) can't resize it, whatever order the stylesheets load in (#303). Below 520px on a fine pointer (a squeezed desktop window) the tab stacks the icon over its label, the pill's shape at the `--font-caption` size, since an icon beside a 7–8 letter label no longer fits five tabs there. The nav never goes icon-only (`design.md` navigation contract, fleet-config#966); the label ellipsizes only as a last resort (#278).

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

## Press-and-slide (phone)

On the phone pill a finger can press one tab and, without lifting, slide across the pill: the capsule follows the finger, the tab under it takes the accent colour, and lifting chooses that tab, as on a native iOS tab bar. A plain tap works as before.

- A press becomes a slide once the finger has moved 8px sideways; less is a tap. Past either end the capsule stops on the last tab.
- Nothing switches until the finger lifts. `aria-selected`, the panes, `data-active-tab` and `onChange` change once, on the lift, exactly as a tap changes them; a slide sends no click of its own. Lifting on the tab that is already open, or a touch the system cancels, changes nothing and does not scroll the page to the top.
- The bar takes `touch-action: none` and `user-select: none`, so a drag that starts on the pill never pans the page, selects a label or opens the long-press callout. The page still scrolls from anywhere else, rows under the pill included.
- A click that arrives within 300 ms of the end of a slide is ignored, because a phone can still send one after a short slide. A click with no pointer behind it (keyboard, assistive tech) switches as before.
- It is plain pointer events, no library, and only on the phone pill: the inline control and the rail have no capsule and ignore a drag.
- At rest nothing moved. The capsule sits at the active tab's own `offsetLeft` and `offsetWidth`, whole pixels, and a slide moves it between those positions. `tests/e2e/shots/gallery-nav-phone-light.png` and `-dark.png` are the pill's at-rest screenshot baseline, compared on every nav edit; `tests/e2e/_nav_slide_checks.py` drives the slide with real touch input.

## Wide layout: left rail

At `(min-width: 1100px) and (pointer: fine)` (`design.md` `layout.wide`, fleet-config#968) the segmented control becomes a **left rail**: `layout.rail` (80px) wide, full height, on the `card` surface with a `line` hairline on its right edge. It shows the same tabs stacked top to bottom, each an `--icon-feature` glyph over a `--font-caption` label, never icon-only. Only the placement changes. The active tint, `aria-selected` and persistence are the same rules, and the markup is the same skeleton. The file offsets your content past the rail itself: `body:has(> .tabs)` gets `padding-left: var(--layout-rail)`, and `.tabs ~ .app` gets the top gap the sticky control used to supply. Both are keyed on the nav so they outrank an app's own `body` / `.app` padding shorthand loaded after this file. Below 1100px the control keeps the `layout.measure` column (`--layout-measure`, 772px); on a coarse pointer nothing changes at any width. Master-detail and a board's full-width exception are app layout, not nav, so they stay in your CSS (#281, lifted from app-launcher#1166).

## Bottom reserve and hit area (phone)

On the phone pill (`(pointer: coarse) and (max-width: 520px)`), `nav-tabs.css` sets `.app`'s `padding-bottom` and `scroll-padding-bottom` to one reserve, `--nav-bottom-reserve`, the bottom fade's full height plus a gap:

```
env(safe-area-inset-bottom) + max(--nav-fade-tail, --bottom-tabs-margin) + --bottom-tabs-height + --bottom-tabs-margin + --gap
```

That is 24 + 61 + 21 + 12 = 118px with no safe area, plus the home-indicator inset on an iPhone. Scrolled to the end, the last row, and the build-identity footer (`page-foot`) when it is `.app`'s last child, ends at least `--gap` above the bottom fade's top edge, so it is never dimmed, and at least `--nav-fade-tail + --gap` (36px) above the pill, so no tap rectangle of the last row meets a tab's. The reserve follows an app's own `--nav-fade-tail`. Do not add your own bottom padding for the nav on top of it.

Above the end, rows scroll under the floating pill by design, so the pill must win every tap in each tab's rectangle. Tabs are rectangles, but the bar's corners are rounded (`--radius-nav`), and a rounded corner takes no taps outside its curve. A transparent `.tabs::before` layer fills the bar's square padding box under the tabs, so a tap in an outer tab's corner sliver lands on the nav, not on the row beneath. It is never painted and exists only on the phone pill: the desktop control and the wide rail have no such layer (#348, design rubric TOUCH-02). The layer is the nav's own `::before`, so don't put an app rule on `.tabs::before`.

## Scroll-edge fades

Content fades gently at the top and bottom edges of the phone pill layout, a hint that more lies past the edge, never a wall. The fully visible middle stays 100% opaque. Both layers are inert (`z-index: 110`, above `.app` and below the pill, `pointer-events: none`). Two tokens size them: `--nav-fade-band` (16px, the top glass's fade and reveal distance) and `--nav-fade-tail` (24px, how far the bottom band reaches above the pill). An app may tune them but never re-draws the layers.

- **Bottom:** a `body::before` band anchored to the screen bottom like the pill, from `--nav-fade-tail` above the pill's top edge down to the screen edge, page colour `--bg` drawn over the content on one eased curve with no solid zone: from the screen edge inward the content's opacity runs 0, 0.25, 0.5, 0.75, 0.92 and 1 at 0, 10, 25, 50, 75 and 100% of the band. Rows show through the pill's glass and fade out under it. The bottom reserve scrolls the last row a gap clear of the whole band, and every tap passes through the band.
- **Top:** a `body::after` glass layer, the translucent top bar the owner validated on the installed iPhone app. It is pinned to the viewport top and runs through the safe area plus `--nav-fade-band`: `color-mix(in srgb, var(--bg) 50%, transparent)` under `blur(20px) saturate(180%)`, with a mask that fades its last `--nav-fade-band` to transparent so it has no bottom edge, on its own compositing layer (`translateZ(0)`). There is no hairline and no header row. Its opacity is `--nav-fade-on`, which `initNavTabs` raises from 0 at scroll-top to 1 over the first `--nav-fade-band` of scroll. At scroll-top the layer is not generated at all: `initNavTabs` sets `data-nav-top-glass` on `<html>` only while the page is scrolled. Scrolling is the only thing that switches it: a resize, a launch or a return to the foreground never drops or regenerates it. At rest nothing is dimmed or blurred: the first row, including an app's rounded header card, static or sticky, sits right under the status bar at full opacity, and the installed app's 1px startup scroll leaves the glass off once it returns to the top. In a browser tab the phone `.app` top padding is the safe area alone.
- **Installed app: a real top-edge strip.** iOS blurs the top edge of an installed (home-screen) web app, a system scroll-edge effect with no opt-out, unless that edge resolves to a real, fixed, full-width element that paints a background. A pseudo-element resolves to `<body>` and a transparent element is skipped, so in the standalone display mode `initNavTabs` inserts a `<div class="nav-top-edge" aria-hidden="true">` as a direct `<body>` child just before the nav (no markup change in the app). At rest it is an opaque `--bg` strip, the safe area plus 12px (the system ignores 10px or less), and `.tabs ~ .app` starts the content below it, so nothing is hidden. Once scrolled it carries the top glass above at full opacity, in place of `body::after`. The browser tab gets no strip.
- **Every standalone surface follows the same top rule.** A full-screen view, a session or terminal view, a sheet or a drawer that an app pins to the top of the viewport starts at `env(safe-area-inset-top, 0px)` (plus its own spacing), the same edge `.app` uses, and adds no fixed offset of its own; the top glass and the installed app's strip stay the nav's own layers. A surface that puts its own fixed layer over the top edge paints a background on it, or the system blur returns there. `modal/`'s phone sheet already anchors at `env(safe-area-inset-top, 0px) + --space-lg`.

### Top-edge blur in the installed app: cause

A soft, frosted band over the top of an installed (home-screen) web app is the iOS 26+ system scroll-edge effect. It is not the nav's glass, and it is not a stale backdrop texture that a repaint would clear. The evidence comes from the installed app on an iOS 27.2 simulator, the same OS the owner's iPhone runs (Safari's user agent reports a frozen 18.x OS token, so read the OS from the `Version/` token or the device itself). The fix was then confirmed on the iPhone:

- **Where it shows:** a band fixed to the screen, roughly 0–95pt from the top. It softens whatever sits there, a header card's text and icons included, and the row just below it is sharp. It appears only in the installed app; the same page in a Safari tab is sharp.
- **What doesn't change it:** removing the nav entirely (its CSS, its JS, and so every glass layer), the app's own scripts, the vendored stylesheets, `theme-color`, the status-bar style (`default` or `black-translucent`), `viewport-fit` (`auto` or `cover`) and an inner scroller in place of document scroll. So none of the nav's timing (a rebuild once the viewport settles, a rebuild on resume, dropping layer promotion for a frame) can cause or cure it.
- **What turns it off:** the top edge resolving to a real, fixed, full-width element that paints a background. An opaque 12px strip is enough, and so is a partly transparent glass `<div>`. A transparent element doesn't count. The same glass CSS as a `body::after` pseudo-element still blurs, because a pseudo-element resolves to `<body>`, which isn't fixed.

That is why the installed app gets the real `.nav-top-edge` strip above, and why the nav keeps no viewport-settle or resume-time glass rebuild: the top glass follows the scroll position alone. If a soft *bottom* bar or capsule is ever seen on a device, record that observation before adding a repaint mechanism back.

Reduce Transparency (`prefers-reduced-transparency: reduce`) drops both layers; the installed app's strip stays, solid `--bg` when scrolled too. It is CSS only, with no transform on the nav. Desktop and the wide rail carry no such layer. The layers are the nav's own `body::before` and `body::after`, so don't put an app rule on either, nor on `.nav-top-edge`. `tests/vendored/test_nav_top_glass.py` pins the top glass's and the strip's declarations to the recipe. `tests/e2e/test_nav_offset.py` checks both layers' geometry, the bottom band's stops and the top glass's tint in light and dark on a 390x844 phone, that the top glass is not generated at scroll-top and is full after one band of scroll, that the installed app's strip is fixed, full-width and opaque at rest with the first card below it, glass once scrolled and absent in a browser tab, and in WebKit measures the bottom band's coverage row by row with no step. The real look is confirmed on an iPhone.

**On-device check page:** the gallery opened as `demo.html?nav` (its `demo-nav.js` module) mounts the real nav with the gallery as its long Home tab and two short tabs, and keeps `theme-color` on the page colour as its theme toggle flips. Serve `app/webapp/static/` over HTTP and open it on the phone (add it to the Home Screen for the installed-app behaviour). It mounts the nav after load, so it shows the fades and the keyboard behaviour, not a cold launch.

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
| `--gap` | `12px` | last term of the phone bottom reserve (see "Bottom reserve and hit area"); phone `.app` side padding (the phone top padding is `env(safe-area-inset-top)` alone) |
| `--font-label` | `0.875rem` | tab label (desktop) |
| `--font-caption` | `0.75rem` | tab label (narrow desktop) |
| `--row-sm` | `44px` | tab min-height on a narrow fine pointer and on any coarse pointer wider than the pill (`hit-target.min`) |
| `--row-lg` | `60px` | rail tab min-height |
| `--space-sm` | `8px` | rail tab padding |
| `--icon-feature` | `24px` | icon size where the tab stacks icon over label: the rail and a squeezed desktop window (`icons.size.feature`) — optional, falls back to `24px` |
| `--layout-measure` | `772px` | desktop column the control spans — optional, falls back to `772px` |
| `--layout-rail` | `80px` | wide-layout rail width + content offset — optional, falls back to `80px` |
| `--radius-md` | `12px` | bar corners (desktop) |
| `--radius-pill` | `9999px` | tab corners |
| `--radius-nav` | `30px` | floating bar corners (mobile) |
| `--bottom-tabs-height` | `61px` | floating bar height — optional, falls back to `61px` |
| `--bottom-tabs-margin` | `21px` | floating bar inset from left/right/physical bottom — optional, falls back to `21px` |
| `--bottom-tabs-pill-height` | `53px` | per-tab pill height (mobile) — optional, falls back to `53px` |
| `--nav-fade-band` | `16px` | the top glass's masked fade below the safe area, and the scroll distance over which it switches on — optional, falls back to `16px` |
| `--nav-fade-tail` | `24px` | how far the bottom fade reaches above the pill's top edge, and so part of the phone bottom reserve — optional, falls back to `24px` |
| `--bottom-tabs-padding` | `4px` | mobile bar inner padding — optional, falls back to `4px` |
| `--bottom-tabs-gap` | `4px` | mobile tab gap — optional, falls back to `4px` |
| `--bottom-tabs-label` | `11px` | mobile label font size — optional, falls back to `11px` |
| `--tabbar-bg` | `rgba(255,255,255,0.62)` (dark `rgba(42,45,52,0.55)`) | floating bar clear-glass fill |
| `--tabbar-border` | `rgba(0,0,0,0.08)` (dark `rgba(255,255,255,0.14)`) | floating bar hairline rim |
| `--tabbar-pill` | `rgba(0,0,0,0.07)` (dark `rgba(255,255,255,0.14)`) | the sliding capsule behind the active tab; **required**, with it undefined the active tab has no capsule |
| `--tabbar-shadow` | `0 8px 28px -6px rgba(20,24,40,0.2), 0 1px 4px rgba(20,24,40,0.06)` (dark `0 10px 30px -6px rgba(0,0,0,0.6)`) | floating bar drop shadow |

The tokens marked optional (`--icon-feature`, `--layout-measure`, `--layout-rail`, `--bottom-tabs-height`, `--bottom-tabs-margin`, `--bottom-tabs-pill-height`, `--bottom-tabs-padding`, `--bottom-tabs-gap`, `--bottom-tabs-label`) are optional per-context knob settings: `nav-tabs.css` reads each with an inline fallback, so an app that leaves one undefined gets the fallback named in its row and nothing renders wrong. Define one only to override it. Every unmarked token is read with no fallback and must be defined.

## The modal-hide rule

The floating bar hides whenever a modal is open so it never floats over a dialog. It uses `visibility: hidden` rather than `display: none` so the fixed layer stays in the render tree across modal open/close, which is gentler on iOS PWA repainting:

- Any native `<dialog open>` → handled automatically (`body:has(dialog[open]) .tabs`).
- A non-`<dialog>` overlay (e.g. a custom login screen) → add the class `nav-hidden` to `<body>` while it's open.

## Pinning the bar (iOS) — CSS-first, body-level nav

Hard-won contract, validated extensively on a real iPhone (`home-automation` #205/#214/#229/#232). The short version: **in an installed PWA the floating bar is pinned by CSS alone, and the nav must be outside the content wrapper/scroller.**

- **Body-level nav is load-bearing.** `nav-tabs.html` places `<nav class="tabs">` as a direct `<body>` child before `<main class="app">`. Do not move it inside `.app`. iOS can capture a fixed-position element nested inside a momentum/content scroller and position it against the scrolled content bottom; on a short tab that makes the bar float mid-screen. A body-level sibling pins against the viewport instead.
- **No measured JS transform, anywhere.** `nav-tabs.css` positions the bar `fixed; bottom: …` and `nav-tabs.js` never moves it: it neither reads the visual viewport to chase a browser toolbar nor translates the bar. Every measured offset risked stranding the bar **up** on iOS, where layout and rendered geometry disagree. What the script does write is state the CSS reads: `data-active-tab`, the `.sliding` class, the capsule's `--pill-x` and `--pill-w`, and the top glass's `data-nav-top-glass` and `--nav-fade-on` on `<html>`.
- **The first scroll is nudged in the installed app.** At startup, when `display-mode: standalone` matches and the page has not been scrolled, `initNavTabs` scrolls 1px and back to the top on the next frame. That asks iOS for the full-height viewport at once rather than growing it over ~3s.
- **Force the page scrollable.** `nav-tabs.css` sets `html { min-height: calc(100dvh + 1px) }` on the phone pill. An installed iOS app shrinks its viewport 59px whenever the document can't scroll (a tab shorter than the screen, or the body pinned by `scroll-lock.js` under an open dialog) and grows it back 1px at a time over ~3s, painting nothing below the moving edge. The rule is on the root, not `.app`, so it still holds while the body is pinned. (Cost: a barely-perceptible scroll on short tabs.) In the installed app (`display-mode: standalone`) the floor is `calc(100lvh + 1px)` instead: iOS resizes that viewport on a cold launch and each time the keyboard closes, and a `dvh` floor re-laid out the document on every frame of the resize, which painted as the page sliding; `lvh` never moves. For the same reason the capsule is re-placed only when a tab cell changes size, so a keyboard resize writes nothing to the bar.
- **The capsule follows the cells, not the window.** `initNavTabs` parks the capsule at the active tab's own `offsetLeft` and `offsetWidth` and re-places it, without the spring, whenever a tab cell changes size: a rotation, an app revealing or hiding a tab after its data arrives (the equal-column grid re-divides), the bar's width settling after a cold launch. A placement that would not move the capsule writes nothing, and an iOS keyboard resize changes no cell, so it writes nothing to the bar.
- **The glass is never rebuilt on a timer.** Neither a launch, a viewport resize nor a return to the foreground drops the bar's `backdrop-filter`, its layer promotion or the top glass. The installed app's top-edge blur is the system scroll-edge effect, which the `.nav-top-edge` strip turns off ("Top-edge blur in the installed app: cause"). `tests/e2e/test_vendored_nav.py` pins that, in a stubbed installed app, a burst of resizes, a `visibilitychange` and a back-forward `pageshow` while scrolled write nothing that drops the glass.
- **Standalone anchors the bar from the top.** Document scroll is the same in a browser tab and the installed app, but in `@media (display-mode: standalone)` the bar and its bottom fade are placed with `top: calc(100lvh - …)`: iOS still grows the installed app's viewport after a cold launch, and `100lvh` is the final height from the first frame, so the bar never rides that growth. A browser tab keeps `bottom:`, since there `lvh` would put the bar under the toolbar.
- **The page head is part of the contract.** The top glass is written for `apple-mobile-web-app-status-bar-style: black-translucent` plus `viewport-fit=auto`, with `theme-color` following the page colour (How to vendor, step 4). `tests/test_nav_standalone_head.py` pins that head in the skeleton, the gallery and this README.
- **An app that pins its body under dialogs needs the same `scroll-lock.js` change.** Neither the nav nor `modal/` carries standalone-shell code, and `initNavTabs` listens for no scroll-lock events. An app-side `scroll-lock.js` that pins the body with `position: fixed` while a `<dialog>` is open must be the plain pin (save `scrollY`, pin at `-scrollY`, restore on close) with no "standalone shell" branch and no event dispatch; it keeps working because the `html` min-height rule above leaves the document scrollable even while the body is pinned. home-automation's `scroll-lock.js` is the reference; the script is app code and is not vendored here.

**Recommended app-level hardening:**

- Set `overscroll-behavior: none` on `html, body` to kill rubber-band drag at the document edges. This lives in the consuming app, not the component, because the document scroller (`body`) is the app's.

## Don't diverge

`nav-tabs.js` and `nav-tabs.css` are **vendored verbatim** — the same "copy byte-for-byte, never edit per-app" rule as the tray's `single_instance.py` / `tray_lifecycle.ps1`. If the contract needs to change, change it **here in `project-scaffolding`** and re-vendor downstream; don't fork it in a consuming app. If your own CSS declares the same selector this file touches (e.g. `.app`, `.card`), use longhand properties or a disjoint media condition — a shorthand property at equal specificity is decided by source order, and can silently override a rule you didn't intend to touch. Streamlit POC spikes are exempt — they don't serve this PWA shell.
