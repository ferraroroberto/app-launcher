# `home-head` — the home-only header card

The fleet's canonical **home-head**: a home-only header card rendered as **one row** at the disclosure closed-summary geometry (`--row-md` 52px, `0 14px` inset), so it aligns as a peer of the cards below it — a leading title glyph + bold title on the left, an optional inline status that ellipsizes, and an icon-only theme toggle pinned right, all vertically centered. Normalized from home-automation's shipped weather-tile shape (`.weather-tile` + `.weather-icon-btn`), generalized into a home header. Pairs with the theme-toggle contract: `~/.claude/design.md` → "Theme switching".

## Files

| File | Role |
| --- | --- |
| `home-head.css` | Visual contract — the 52px header row, title cluster, ellipsizing status, right-pinned icon toggle. References design tokens only. |
| `home-head.html` | Markup skeleton to copy and adapt. |

## How to vendor

1. Copy this `home-head/` folder **verbatim** into your app's static dir (`app/webapp/static/_vendored/home-head/`). Do **not** edit `home-head.css` per-app.
2. Link the CSS **after** `card.css` (see "Load order" below):
   ```html
   <link rel="stylesheet" href="/static/_vendored/card/card.css">
   <link rel="stylesheet" href="/static/_vendored/home-head/home-head.css">
   ```
3. Paste the skeleton as the first child of your Home view, adapt the icon + title, drop the `.status` span if you don't need it, and wire the toggle to your theme boot script.

## Markup contract

```html
<div class="card home-head">
  <span class="home-title">
    <svg class="icon" aria-hidden="true"><use href="#i-house"></use></svg>
    App name
  </span>
  <span class="status">All systems normal</span>
  <button type="button" class="home-toggle" aria-label="Toggle theme" title="Toggle theme">
    <svg class="icon" aria-hidden="true"><use href="#i-moon"></use></svg>
  </button>
</div>
```

- It is a **`.card` modifier** (`class="card home-head"`) — `.card` supplies the surface (fill, hairline border, radius); `.home-head` supplies the row layout and overrides the card's padding to the 52px `0 14px` geometry.
- `.home-title` is the leading glyph + bold title, kept on one line. `.home-title .icon` is title-size (`--icon-title`, 18px), muted.
- `.status` is **optional** — an inline muted line that takes the remaining width and ellipsizes, so a long status can never wrap the row or shove the toggle off-screen. Omit the span entirely if the header carries no status.
- `.home-toggle` is the icon-only theme toggle, a 34px square with a subtle-contrast fill (`--close-bg`) — the same recipe as the modal's `.detail-close`, never a bare floating glyph. It is pinned right (`margin-left: auto`) even when no `.status` is present. Swapping its glyph (sun ⇄ moon) and persisting the theme is the caller's job per design.md's "Theme switching" contract — this component owns the button's look, not the theme logic.

## Load order

`home-head.css` overrides the card's own padding with a **single-class** rule (`.home-head { padding: 0 14px }`) at the same specificity as `.card { padding: … }`, so it must be **linked after `card.css`** — exactly the source-order convention the `disclosure` card uses for its `.card--collapsible` padding-zeroing. If your own stylesheet also sets padding on `.home-head`, keep it after this file or use a higher-specificity selector.

## Required design tokens

| Token | Light value | Used for |
| --- | --- | --- |
| `--row-md` | `52px` | row height (the `rows.md` / disclosure closed-summary peer geometry) |
| `--gap` | `12px` | gap between title, status, and toggle |
| `--space-sm` | `8px` | gap inside the title cluster (glyph ↔ text) |
| `--icon-title` | `18px` | title + toggle glyph size |
| `--radius-md` | `12px` | toggle corners |
| `--close-bg` | `var(--card-off)` | toggle fill |
| `--ink` | `#1f2328` | title text |
| `--muted` | `#656d76` | status + toggle glyph |
| `--font-body` | `1rem` | title text size |
| `--font-label` | `0.92rem` | status text size |

## Don't diverge

`home-head.css` is vendored verbatim — to change the contract, change it **here in `project-scaffolding`** and re-vendor downstream. In particular, keep the row at the `--row-md` (52px) / `0 14px` geometry so it stays a visual peer of the disclosure cards below it — never an ad hoc height. If your own CSS declares the same selector this file touches (e.g. `.card`, `.status`), use longhand properties or a disjoint media condition — a shorthand property at equal specificity is decided by source order, and can silently override a rule you didn't intend to touch. Streamlit POC spikes are exempt.
