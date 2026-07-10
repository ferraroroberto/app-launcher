# `nav` — floating bottom-tab navigation

The fleet's canonical **primary navigation**: a top segmented control on desktop that becomes a single floating bottom-tab pill on touch / installed-PWA. This is the navigation contract every fleet web app must *feel* identical on (see `~/.claude/design.md` → "Navigation & interaction").

## Files

| File | Role |
| --- | --- |
| `nav-tabs.js` | Behaviour. ESM module — `initNavTabs(opts)`. Discovers tabs/panes from the DOM, persists the active tab, keeps ARIA + roving `tabindex` in sync. |
| `nav-tabs.css` | Visual contract. The desktop segmented control + the `@media (pointer: coarse)` floating pill + the modal-hide rule. References design tokens only. |
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
4. Wire up the switcher once the DOM is ready:
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

Each `.tab` holds one `<svg class="tab-icon">` stroke glyph and one `<span class="tab-label">` — and the icon is visible on **both** surfaces: beside the label in the desktop segmented control, above it in the mobile pill. Give the SVG a `24 24` viewBox and `<path>`s with no `fill`/`stroke` attributes of their own; `nav-tabs.css` paints them (`fill: none; stroke: currentColor`) so they inherit the active/inactive tab colour. Desktop sizes the icon at `1.05em` of the label's font-size; the pill uses `--bottom-tabs-icon`. Below 520px on a fine pointer the label is clipped to the accessibility tree and the icon stands alone.

`.tab-emoji` is **legacy** — an emoji span the desktop control used to show instead of the icon, superseded by SVG glyphs fleet-wide (`home-automation#77`, fixed here in `project-scaffolding#142`). `nav-tabs.css` hides it at every width, so an app still shipping the span picks up its desktop icon by re-vendoring the CSS alone; delete the span from your markup when you next touch it. If your app kept a per-app `.tab-icon { display: … }` override to work around the old rule, drop that too — it now fights the vendored file.

## Required design tokens

`nav-tabs.css` references these CSS custom properties — define them in your app's `:root` / `[data-theme="dark"]` blocks, **wired to `~/.claude/design.md` (+ `design.dark.md`)**. Don't copy the spec; point your tokens at it. Reference values (light) from the canonical implementation. The mobile geometry below is the phone-validated standard promoted from `home-automation` issue #118: measured from 1290px-wide iPhone screenshots of the GitHub/VLC apps (~3x CSS pixels), then validated live on Roberto's iPhone.

| Token | Light value | Used for |
| --- | --- | --- |
| `--card` | `#ffffff` | tab bar surface (desktop) |
| `--card-off` | `#f6f8fa` | active-tab fill |
| `--accent` | `#0969da` | active-tab text/icon |
| `--muted` | `#656d76` | inactive-tab text |
| `--line` | `#d1d9e0` | bar border, active-tab border (mobile) |
| `--space-xs` | `4px` | bar padding / gap (desktop) |
| `--gap` | `12px` | bottom-padding reserve |
| `--font-label` | `0.92rem` | tab label (desktop) |
| `--font-caption` | `0.78rem` | tab label (narrow desktop) |
| `--radius-md` | `12px` | bar corners (desktop) |
| `--radius-pill` | `9999px` | tab corners |
| `--radius-nav` | `30px` | floating bar corners (mobile) |
| `--bottom-tabs-height` | `61px` | floating bar height |
| `--bottom-tabs-margin` | `21px` | floating bar inset from left/right/physical bottom |
| `--bottom-tabs-pill-height` | `53px` | per-tab pill height (mobile) |
| `--bottom-tabs-padding` | `4px` | mobile bar inner padding |
| `--bottom-tabs-gap` | `4px` | mobile tab gap |
| `--bottom-tabs-icon` | `20px` | mobile SVG icon size |
| `--bottom-tabs-label` | `11px` | mobile label font size |
| `--tabbar-bg` | `rgba(255,255,255,0.85)` | floating bar glass fill |
| `--tabbar-border` | `rgba(31,35,40,0.12)` | floating bar border |

## The modal-hide rule

The floating bar hides whenever a modal is open so it never floats over a dialog. It uses `visibility: hidden` rather than `display: none` so the fixed layer stays in the render tree across modal open/close, which is gentler on iOS PWA repainting:

- Any native `<dialog open>` → handled automatically (`body:has(dialog[open]) .tabs`).
- A non-`<dialog>` overlay (e.g. a custom login screen) → add the class `nav-hidden` to `<body>` while it's open.

## Pinning the bar (iOS) — CSS-first, body-level nav

Hard-won contract, validated extensively on a real iPhone (`home-automation` #205/#214/#229/#232). The short version: **in an installed PWA the floating bar is pinned by CSS alone, and the nav must be outside the content wrapper/scroller.**

- **Body-level nav is load-bearing.** `nav-tabs.html` places `<nav class="tabs">` as a direct `<body>` child before `<main class="app">`. Do not move it inside `.app`. iOS can capture a fixed-position element nested inside a momentum/content scroller and position it against the scrolled content bottom; on a short tab that makes the bar float mid-screen. A body-level sibling pins against the viewport instead.
- **Standalone PWA → no measured JS transform.** `nav-tabs.css` positions the bar `fixed; bottom: …`, which is correct on its own in a fullscreen installed PWA (no browser chrome). The VisualViewport transform only ever existed to chase Safari's collapsing *browser* toolbar; in standalone every measured offset eventually risks stranding the bar **up** because iOS's layout and rendered geometry disagree there. So `initNavTabs` detects `display-mode: standalone` / `navigator.standalone` and never applies a measured translate.
- **Browser tab → minimal transform.** Only in a real browser tab (where the toolbar genuinely collapses) does it translate the bar up by the hidden slice — clamped to a toolbar's height (~160px) and suppressed while the soft keyboard is up (a focused field, or a viewport shrink past a toolbar's worth). Desktop's sticky top control is untouched; feature-detected on `window.visualViewport`.
- **Force the page scrollable (browser tab).** `nav-tabs.css` sets `.app { min-height: calc(100dvh + 1px) }`. iOS standalone can anchor a `position: fixed` bar to the *content* bottom on a non-scrolling page, so a short tab may float the bar up; the extra 1px keeps the page technically scrollable, which helps iOS anchor fixed elements at the screen bottom. (Cost: a barely-perceptible scroll on short tabs.)
- **Standalone → the fixed-inset `.app` scroller is the contract (home-automation#303), not normal document scroll.** Document scroll is the **browser-tab** behavior above. In an *installed* standalone PWA the home-screen WKWebView's native scroll bounce moves the visual viewport itself (home-automation#300), dragging every `position: fixed` element with it — `overscroll-behavior: none` doesn't govern that native bounce the way it does in a Safari tab. So in standalone the document must never scroll at all: `nav-tabs.css` makes `.app` a `position: fixed; inset: 0` element scroller (sized `height: 100vh` → `100lvh` so it has its final geometry from the first frame of iOS's cold-launch viewport-expansion animation, `overflow-y: auto`, `overscroll-behavior: none`), and an inert `body::after` spacer (`calc(100dvh + 1px)`) keeps the *document* technically scrollable — which keeps iOS's layout viewport expanded to the full physical screen — while no touch gesture can ever reach that 1px, so nothing meaningfully unlocked is left for the bounce to move. The `.tabs` bar anchors from the stable **top** edge via `100lvh` (a bottom anchor visibly floats down for ~2s during the cold-launch expansion) and refuses pan gestures (`touch-action: none`) since it's the one fixed surface a drag could still reach the 1px-scrollable document through. This revives the inner-scroller shell an earlier fleet round (home-automation#232) rejected for leaving an unusable bottom safe-area dead band — the `100lvh` sizing (large-viewport unit, stable from the first frame while iOS animates the layout-viewport expansion) is what resolves that dead band, which is why the shell is now the accepted standalone contract rather than a fallback of last resort. `design_lint.py`'s nav-contract check (`fleet-config#282`) keys on exactly this block, so a verbatim adopter of this file passes it automatically.

**Recommended app-level hardening:**

- Set `overscroll-behavior: none` on `html, body` to kill rubber-band drag at the document edges. This lives in the consuming app, not the component, because the document scroller (`body`) is the app's.

## Don't diverge

`nav-tabs.js` and `nav-tabs.css` are **vendored verbatim** — the same "copy byte-for-byte, never edit per-app" rule as the tray's `single_instance.py` / `tray_lifecycle.ps1`. If the contract needs to change, change it **here in `project-scaffolding`** and re-vendor downstream; don't fork it in a consuming app. If your own CSS declares the same selector this file touches (e.g. `.app`, `.card`), use longhand properties or a disjoint media condition — a shorthand property at equal specificity is decided by source order, and can silently override a rule you didn't intend to touch. Streamlit POC spikes are exempt — they don't serve this PWA shell.
