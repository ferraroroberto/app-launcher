# `action-row` — a list row that does something

The fleet's canonical **action-row** (`~/.claude/design.md` → "Component contracts" → action-row, `ferraroroberto/fleet-config#965`): a flat list row whose **tap performs its primary action** (launch, run, open), with every other action behind **one trailing 44px kebab**. A row may add **at most one leading toggle** (favorite) and **at most one other visible action**, and only when that action is the row's dominant verb (Run on a job row). Title: `body` at 600, one line, ellipsized. Context: at most one `body-sm` line in muted. No vertical rules between controls. A list that can exceed ~12 rows gets a filter field above it.

**Row verbs are plain icon-buttons.** Every visible verb on a row (Live, Wake, Start, Stop, Find, Run) is an unpainted [`icon-button`](../icon-button/) at the 44px row box, next to the kebab: no fill and no border at rest, and the glyph colour is the only state (muted at rest, ink on hover). A cluster of verbs is an [`.icon-button-group`](../icon-button/), with no track. The glyph and the `aria-label` say which verb it is; the glyph map is below. Owner decision `ferraroroberto/fleet-config#1420`, built in `project-scaffolding#386`. The tinted Run and the outlined Stop (`fleet-config#1386`) are retired.

Normalized from app-launcher's session rows (`app-launcher#1025`: one row button plus a kebab in its own slot) and the rendered UX audit's proposed row (`fleet-config#962`), to stop a fifth hand-built row shape (`project-scaffolding#268`). The optional leading avatar, leading check and trailing item (a switch, value, stepper or segmented verb in place of the kebab) were added in `project-scaffolding#341`, normalized from home-automation's shared row (`row.js`, home-automation#880). They are opt-in classes, so a row without them renders exactly as before. See [Extensions](#extensions-leading-avatar-or-check-trailing-item).

## Files

| File | Role |
| --- | --- |
| `action-row.css` | Visual contract: the full-bleed list card, the row and its `rows` heights, the stretched main button, title/meta truncation, the 44px accessories, the filter field. References design tokens only. |
| `action-row.html` | Markup skeleton to copy and adapt. |

It also needs two glyphs from the vendored `icons/` sprite: `i-ellipsis-vertical` (the kebab) and, for a favorite toggle, `i-star` + `i-star-fill`. `i-search` and `i-play` are used by the skeleton's filter and verb. The verb glyph map is in [Verb glyphs](#verb-glyphs). A check lead needs `i-circle` + `i-circle-check`. A trailing switch links [`switch/`](../switch/), and a segmented verb cluster links [`segmented/`](../segmented/).

## How to vendor

1. Copy this `action-row/` folder **verbatim** into your app's static dir (`app/webapp/static/_vendored/action-row/`). Do **not** edit `action-row.css` per-app.
2. Link it with `icon-button.css`, which owns the accessories' look (any order relative to `card.css` and `icon-button.css`; every override here is at higher specificity, so it wins either way):
   ```html
   <link rel="stylesheet" href="/static/_vendored/icon-button/icon-button.css">
   <link rel="stylesheet" href="/static/_vendored/action-row/action-row.css">
   ```
3. Paste the skeleton, drop the optional parts your rows don't have, and wire the behaviour (below).

## Markup contract

```html
<div class="card action-list">
  <label class="action-row-filter">                        <!-- optional: lists that can pass ~12 rows -->
    <svg class="icon" aria-hidden="true"><use href="#i-search"></use></svg>
    <input type="search" placeholder="Filter skills" aria-label="Filter skills">
  </label>
  <ul class="action-rows">
    <li class="action-row">
      <button type="button" class="icon-button action-row-fav" aria-pressed="false" aria-label="Favorite">  <!-- optional -->
        <svg class="icon action-row-fav-off" aria-hidden="true"><use href="#i-star"></use></svg>
        <svg class="icon action-row-fav-on" aria-hidden="true"><use href="#i-star-fill"></use></svg>
      </button>
      <button type="button" class="action-row-main">          <!-- or <a href> when the row navigates -->
        <span class="action-row-title">journal-daily</span>
        <span class="action-row-meta">Last run yesterday</span>  <!-- optional -->
      </button>
      <button type="button" class="icon-button action-row-verb" aria-label="Run">…</button>  <!-- optional, dominant verb only; a plain icon-button, whatever it does -->
      <button type="button" class="icon-button action-row-kebab" aria-label="More actions"
              aria-haspopup="menu" aria-expanded="false">
        <svg class="icon" aria-hidden="true"><use href="#i-ellipsis-vertical"></use></svg>
      </button>
    </li>
  </ul>
</div>
```

- **`.card action-list`** is a `.card` modifier that zeroes the card's padding: the rows are full-bleed, on a `--line-muted` hairline between rows (`.action-row + .action-row`), never nested cards. Rows placed in another host (a disclosure body) need that host's horizontal padding at zero too.
- **`.action-row-main`** is the primary action: a `<button>`, or an `<a>` when the row only navigates. It stretches to the row's full height, so the tap target is the row, not the words. The kebab (and the favorite, and the verb) are **siblings** of it, never children, so a tap on one never also fires the row.
- **Height** comes from the `rows` scale: a title-only row is `--row-md` (52px), a row with an `.action-row-meta` line is `--row-lg` (60px). Never a literal.
- **`.action-row-title` / `.action-row-meta`** are one line each, ellipsized. Put one context line at most; a row that needs more is a detail view.
- **The accessories are `.icon-button`s.** `.action-row-fav`, `.action-row-verb` and `.action-row-kebab` each carry `icon-button` as well (`class="icon-button action-row-kebab"`): [`icon-button/`](../icon-button/) owns the unpainted box, the glyph colour states and the target, and `action-row.css` sets their box to the 44px `--row-sm` (`--icon-btn-box`), so no expansion is needed. Only the rules below are the row's own.
- **`.action-row-fav`** is the one leading toggle. The caller flips `aria-pressed` only; CSS swaps the outline star for the filled one and colors it `--attention`. The fill is what reads without hue.
- **`.action-row-verb`** is the one extra visible action: a plain `icon-button` at 44px, with no fill and no border at rest, whatever it does (Run, Stop, Live, Wake). Use it only for the row's dominant verb; everything else goes in the menu. Keep it named (`aria-label="Stop"`), since the glyph is the only other cue. `action-row.css` ships no paint for it: the glyph colours come from `icon-button.css`, so link that file.
- **`.action-row-verb--stop`** is a **deprecated alias** that renders identically to `.action-row-verb` (the outlined Stop of `fleet-config#1386` and `project-scaffolding#361` is retired). `action-row.css` declares no rule for it, so markup that still emits it keeps working when an app re-vendors. Four fleet apps still carry it, one in live markup, so the alias stays; new markup omits it, and it goes when no app emits it.
- **`.action-row-kebab`** opens the row menu. Keep `aria-expanded` in step with the menu; the kebab takes `--accent` while it is open.
- **`.action-row-filter`** is a 44px field with a `--control-border` boundary on `--card`. Filtering is the caller's job (set `hidden` on the `<li>`s that don't match).

All accessories are real 44×44 boxes placed side by side with no gap, so their hit rectangles touch and never overlap (`design.md` → "Touch targets", adjacent cluster).

## Extensions: leading avatar or check, trailing item

A row has **exactly one leading slot** and, in place of the kebab, may have **exactly one trailing item** (design.md action-row). This fits a device list, a shopping list or a settings pane: the row tap opens the item's **detail sheet**, which holds every other action, and the one trailing control is the thing you change most often.

```html
<li class="action-row">
  <button type="button" class="action-row-main" aria-haspopup="dialog">   <!-- tap = open the detail sheet -->
    <span class="action-row-avatar" data-badge="up" aria-hidden="true">   <!-- optional leading avatar -->
      <svg class="icon"><use href="#i-plug"></use></svg>
    </span>
    <span class="action-row-text">
      <span class="action-row-title">Sample plug</span>
      <span class="action-row-meta">On · 12 W</span>
    </span>
  </button>
  <!-- one trailing item, in place of the kebab -->
  <button type="button" class="toggle on action-row-trail" role="switch"
          aria-checked="true" aria-label="Sample plug">
    <span class="knob"></span><span class="toggle-label">ON</span>
  </button>
</li>
```

**Leading slot: one of these per row.**

- **`.action-row-avatar`** (design.md `avatar`) is a 36px `rounded.md` squircle on `neutral-soft` with one 20px glyph (the item's kind or mode) in `fg`. It goes **inside** `.action-row-main`, so it is part of the tap target. The title and meta then move into an **`.action-row-text`** wrapper beside it, and the main button lays out as a row only when it holds an avatar. The avatar is decoration (`aria-hidden="true"`): whatever it says, the meta line says too. With no badge it is a plain kind squircle (a zone, a category).
- **The badge** is `data-badge="up"` (a `success` dot: connected and running) or `data-badge="down"` (a `danger` dot: should be reachable and is not), 12px, ringed in `card` so it reads cut out. It means **alive and nothing else**. Omit the attribute when the item is plainly off. A device that is legitimately absent gets no badge, and unreachable items of one kind fold into a single Offline row instead of a red mark on each. Only `up` and `down` paint, so a stray value never draws a success dot. `success` is never a chip or a fill elsewhere.
- **`.action-row-check`** is the other leading toggle (an item got, a step done): a 44px `.icon-button` sibling of the main button, like the favorite. It holds two glyphs, `.action-row-check-off` (`i-circle`) and `.action-row-check-on` (`i-circle-check`). The caller flips `aria-pressed` only. The glyph's shape is what reads without hue, and icon-button's pressed colour (`--accent-text`) is the second cue.
- The favorite (`.action-row-fav`, above) is the third choice. One row never carries two.

**Trailing item: exactly one, in place of the kebab.** Mark it **`.action-row-trail`**. It is a sibling of the main button, so a press on it never opens the sheet, and it is inset 10px so its edge lines up with the text column's inset. It brings its own 44px target:

- **A switch:** the vendored `.toggle` (44×26 with a vertical band to 44px), `class="toggle action-row-trail"`.
- **A value:** `<span class="action-row-trail action-row-value">`, one tabular figure in `fg` at 600 (a reading, a price, a count). It is not a control.
- **A stepper:** your app's stepper element with `action-row-trail` on its outer box. Each of its buttons is a real 44px target.
- **A verb cluster** (Live · Wake, Up · Stop · Down on a blind): `<span class="icon-button-group action-row-trail" role="group" aria-label="…">` holding plain `.icon-button`s, with no track. The row sets the group's box to 44px, so the gap is 0 and every verb is a real 44×44 target that touches its neighbour and never overlaps it.
- **A segmented mode selector** (an AC mode: Cool · Fan · Heat, where one segment is the selected state): `class="segmented segmented--verbs action-row-trail"` from [`segmented/`](../segmented/). It is a choice with a selected state, so it keeps its track; each segment is 44px wide on the 44px verbs track. Verbs that only fire (no selected state) are the icon-button cluster above, never a segmented control.
- **A verb or a kebab** stays as above (`.action-row-verb`, `.action-row-kebab`). A row whose tap opens a detail sheet usually needs neither.

Height still comes from the `rows` scale (a 36px avatar fits the 52px row), and the trailing item is centred in it.

## Verb glyphs

One glyph per verb, from the vendored [`icons/`](../icons/) sprite, so the same verb looks the same in every app:

| Verb | Glyph |
| --- | --- |
| Live | `i-video` |
| Wake | `i-power` |
| Start / Run | `i-play` |
| Stop | `i-square` |
| Find | `i-search` |

## The row menu stays app-side (for now)

This component ships the **anchor**, not the floating menu. The reference implementation is app-launcher's `row-menu.js` (one anchor opens a vertical list of icon + label items; it survives a poll re-render and closes on outside tap, Escape and a second tap). Whatever the app uses, the menu keeps the contract: **destructive actions** (Kill, Stop, Delete) are its **last item, after a divider, in `--danger-text`, behind a confirm**. A row never shows a visible danger button.

## Required design tokens

| Token | Light value | Used for |
| --- | --- | --- |
| `--row-sm` | `44px` | accessory squares, filter height (`hit-target.min`) |
| `--row-md` | `52px` | one-line row height |
| `--row-lg` | `60px` | row height with a context line |
| `--line-muted` | `#d8dee4` | hairline between rows (`border-muted`) |
| `--ink` | `#1f2328` | title text (the verb glyph on hover comes from `icon-button/`) |
| `--muted` | `#656d76` | context line, filter glyph (resting accessory glyphs come from `icon-button/`) |
| `--font-body` | `1rem` | title, filter input |
| `--font-body-sm` | `0.875rem` | context line (`typography.body-sm`) |
| `--icon-inline` | `16px` | filter glyph (the accessory glyphs take it through `icon-button/`; `icons.size.inline`) |
| `--radius-md` | `12px` | filter, main-button press shape |
| `--space-sm` | `8px` | filter glyph ↔ input |
| `--gap` | `12px` | filter margin |
| `--accent` | `#0969da` | open kebab, filter focus ring |
| `--accent-soft` | `color-mix(…16%…)` | row press feedback |
| `--accent-text` | `#0550ae` | pressed check glyph |
| `--attention` | `#9a6700` | pressed favorite |
| `--control-border` | `#818b98` | filter boundary |
| `--card` | `#ffffff` | filter fill, avatar badge ring |
| `--neutral-soft` | `color-mix(in srgb, var(--muted) 16%, transparent)` | avatar fill (only with an avatar) |
| `--success` | `#1a7f37` | avatar badge `up` (only with an avatar) |
| `--deficit` | `#cf222e` | avatar badge `down` (design.md `danger`; only with an avatar) |
| `--bottom-tabs-icon` | `20px` | avatar glyph (`icons.size.nav-tab`; falls back to 20px) |

## Don't diverge

`action-row.css` is vendored verbatim: to change the contract, change it **here in `project-scaffolding`** and re-vendor downstream. Don't add a second visible action, a second leading slot, a second trailing item, a vertical rule, or a visible danger button per-app; those are the shapes this component exists to retire. An app that grew its own avatar or trailing-item extension (home-automation's `.row-avatar` / `.action-row-trail`, app-launcher's copy) drops it for these classes when it re-vendors. If your own CSS declares a selector this file touches, use longhand properties or a disjoint media condition — a shorthand at equal specificity is decided by source order. Streamlit POC spikes are exempt.
