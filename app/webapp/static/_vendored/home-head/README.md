# `home-head` — the home-only header card

The fleet's canonical **home-head**: a home-only header card rendered as **one row** at the disclosure closed-summary geometry (`--row-md` 52px, `0 14px` inset), so it aligns as a peer of the cards below it — a leading title glyph + bold title on the left, an optional inline status that ellipsizes, and **two icon-only trailing actions pinned right — the theme toggle, then the Settings gear** — all vertically centered. Normalized from home-automation's shipped weather-tile shape (`.weather-tile` + `.weather-icon-btn`), generalized into a home header. Pairs with the theme-toggle contract (`~/.claude/design.md` → "Theme switching") and the `page-header` component: this is that shape, reused verbatim on **every** pane, not just Home.

## Files

| File | Role |
| --- | --- |
| `home-head.css` | Visual contract — the 52px header row, title cluster, ellipsizing status, the tone colours of its exception parts, right-pinned icon actions (theme toggle + Settings gear), their 44px hit areas. References design tokens only. |
| `home-head.js` | The live context line: `renderHeadStatus(el, exceptions, fallback)`, a pure render of at most two tone-coloured exception parts, else the plain fallback. |
| `home-head.html` | Markup skeleton to copy and adapt. |

## How to vendor

1. Copy this `home-head/` folder **verbatim** into your app's static dir (`app/webapp/static/_vendored/home-head/`). Do **not** edit `home-head.css` per-app.
2. Link the stylesheets (order does not matter — see "Load order" below). `home-head.css` carries only the header row and the toggles' placement; the toggles are `.icon-button`s, so [`icon-button/`](../icon-button/) rides along:
   ```html
   <link rel="stylesheet" href="/static/_vendored/card/card.css">
   <link rel="stylesheet" href="/static/_vendored/home-head/home-head.css">
   <link rel="stylesheet" href="/static/_vendored/icon-button/icon-button.css">
   ```
3. If a pane's `.status` is live, import the renderer (`import { renderHeadStatus } from '/static/_vendored/home-head/home-head.js'`) and call it from the code that refreshes that pane. A static header needs no script.
4. Paste the skeleton as the first child of **every** tab's pane, adapt the icon + title (the title names the current tab), drop the `.status` span if you don't need it, wire the toggle to your theme boot script and the gear to your Settings view.

## Markup contract

```html
<div class="card home-head">
  <span class="home-title">
    <svg class="icon" aria-hidden="true"><use href="#i-house"></use></svg>
    App name
  </span>
  <span class="status">All systems normal</span>
  <button type="button" class="icon-button home-toggle" aria-label="Toggle theme" title="Toggle theme">
    <svg class="icon" aria-hidden="true"><use href="#i-moon"></use></svg>
  </button>
  <button type="button" class="icon-button home-toggle home-settings" aria-label="Settings" title="Settings">
    <svg class="icon" aria-hidden="true"><use href="#i-settings"></use></svg>
  </button>
</div>
```

- It is a **`.card` modifier** (`class="card home-head"`) — `.card` supplies the surface (fill, hairline border, radius); `.card.home-head` supplies the row layout and overrides the card's padding to the 52px `0 14px` geometry.
- `.home-title` is the leading glyph + bold title, kept on one line. `.home-title .icon` is title-size (`--icon-title`, 18px), muted.
- `.status` is **optional** — an inline muted line that takes the remaining width and ellipsizes, so a long status can never wrap the row or shove the toggle off-screen. Omit the span entirely if the header carries no status.
- `.home-toggle` is an icon-only trailing action, and it is an **`.icon-button`** ([`icon-button/`](../icon-button/), fleet-config#1259): the markup carries both classes (`class="icon-button home-toggle"`) and `icon-button.css` owns the look — a glyph on nothing, **transparent at rest** with no border or shadow, glyph-colour hover/disabled states, its invisible box grown to the 44px target by its `::before`. `home-head.css` keeps only the header's specifics: the `34px` box (`--icon-btn-box`), the title-size glyph (`--icon-title`), and the placement. The first one is pinned right (`margin-left: auto`) even when no `.status` is present; the next sits beside it (`.home-toggle + .home-toggle` takes no auto margin). Swapping its glyph (sun ⇄ moon) and persisting the theme is the caller's job per design.md's "Theme switching" contract — this component owns the button's placement, not the theme logic.
- **The Settings gear** is the second `.home-toggle`, with `.home-settings` (a hook for your click handler) beside it and no button tier: it is an icon button like the toggle, never `.button-surface`. **Settings is never a tab:** the gear sits beside the theme toggle on **every** pane, always both, at every nav size, so Settings is one tap from each tab (design.md "Navigation" and `page-header`). Do not drop the gear on a tab that has "nothing to set", and do not add a Settings tab to `nav/`. Where the gear leads (a settings view, a modal, a sheet) is the app's call.
- **Both actions are 44px targets.** Each keeps an invisible 34px box (`--icon-btn-box`); icon-button's `::before` extends the hit area to the 44px floor, and the row's `--gap` (12px) is wider than the two 5px expansions together, so the pair never overlaps (design.md "Touch targets"). The e2e harness asserts both on the gallery.

## Context line

design.md `page-header`: the line is **live and names only the exceptions**, each part in its tone's `*-text`, at most two parts (about 22 characters at 390px), falling back to a plain `--muted` fact when nothing is wrong. The caller computes the exceptions from the same counts its list draws; this component only renders them.

```js
renderHeadStatus(document.querySelector('.home-head .status'), [
  { text: '1 needs you', tone: 'attention' },
  { text: '1 stalled', tone: 'danger' },
], '7 sessions');
// -> <span class="head-exception" data-tone="danger">1 stalled</span> · <span ... data-tone="attention">1 needs you</span>
```

- `tone` is `danger` (`--danger-text`), `attention` (`--attention-text`) or `neutral`. Neutral, or any other value, has no rule and keeps the slot's muted colour, which is the way to say "could not be read" without alarming.
- **Severity order, then caller order.** Parts are sorted danger, attention, then the rest with a stable sort, as the spec asks ("offline or failed first"), so a caller passes them in any order and ties keep the order given. Only the first two survive.
- No exceptions (an empty list, `null`, or entries without text) renders `fallback` as plain text, with no spans and no `data-tone` on the slot, exactly as a header without this script. An omitted `fallback` blanks the slot. A missing `el` is a no-op, so a header with no `.status` needs no guard.
- The slot also gets `data-tone` set to the worst part's tone, as a hook for an app that wants one; the colour comes from the part, never the slot.
- Text goes in with `textContent`, never as HTML.

**Reconciled from the two app copies** (app-launcher `home-head.js`, home-automation `head-status.js`), which agreed on the markup and the two-part cap and differed in three places: (1) home-automation sorted by severity and app-launcher kept the caller's order; this keeps the **sort**, as the spec states the rule, so an adopter whose callers relied on order (app-launcher's Code line, "needs you" before "stalled") now reads danger first. (2) home-automation defaulted a missing `fallback` to `''` and app-launcher passed it through, which prints "undefined"; this keeps the **default**. (3) app-launcher also rendered a `neutral` part; this keeps that, and gives a part with no tone `neutral` rather than the string "undefined". The per-tab bookkeeping of home-automation's `setHeadPart` (several modules feeding one line) stays app-side: it is state, and this renderer is pure.

## Load order

`home-head.css` overrides the card's own padding with the two-class rule `.card.home-head { padding: 0 14px }`, which beats `.card { padding: … }` on specificity regardless of link order — the same load-order-independent form the `disclosure` card uses for its `.card.card--collapsible` padding-zeroing. If your own stylesheet also sets padding on `.home-head`, match or exceed that specificity (e.g. `.card.home-head` too) rather than relying on source order.

## Required design tokens

| Token | Light value | Used for |
| --- | --- | --- |
| `--row-md` | `52px` | row height (the `rows.md` / disclosure closed-summary peer geometry) |
| `--gap` | `12px` | gap between title, status, and toggle |
| `--space-sm` | `8px` | gap inside the title cluster (glyph ↔ text) |
| `--icon-title` | `18px` | title + toggle and gear glyph size |
| `--ink` | `#1f2328` | title text |
| `--muted` | `#656d76` | status text (the toggle and gear glyph colour comes from `icon-button/`) |
| `--danger-text` | `#a40e26` (dark `#ff7b72`) | a `danger` exception part (6.08:1 light / 5.70:1 dark on its tint, higher on the card) |
| `--attention-text` | `#7d4e00` (dark `#e3b341`) | an `attention` exception part (5.75:1 light / 6.84:1 dark on its tint, higher on the card) |
| `--font-body` | `1rem` | title text size |
| `--font-label` | `0.875rem` | status text size |

## Don't diverge

`home-head.css` and `home-head.js` are vendored verbatim — to change the contract, change it **here in `project-scaffolding`** and re-vendor downstream. In particular, keep the row at the `--row-md` (52px) / `0 14px` geometry so it stays a visual peer of the disclosure cards below it — never an ad hoc height. If your own CSS declares the same selector this file touches (e.g. `.card`, `.status`), use longhand properties or a disjoint media condition — a shorthand property at equal specificity is decided by source order, and can silently override a rule you didn't intend to touch. Streamlit POC spikes are exempt.
