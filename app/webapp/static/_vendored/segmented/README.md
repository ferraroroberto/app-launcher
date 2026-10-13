# `segmented` — one choice among 2–5 peers

The fleet's canonical **segmented control** (`~/.claude/design.md` → "Component contracts" → segmented control, token block `segmented`, `ferraroroberto/fleet-config#1334`): one control for a choice the user switches in place, such as a mode (alarm Off · Partial · Full, AC mode), a period (Day · Week · Month), a view (Items · Prices) or a scope (Mine · Issues · All). It is one `rounded.md` track on `neutral-soft` with a 3px inset and no outer border, holding equal borderless segments. **The selected segment is the state:** it is raised with a soft shadow, label in `fg`, `aria-pressed="true"`. Light fills it with `card`; dark fills it one step lighter than the track, so it reads raised rather than pressed in (see below).

Normalized from home-automation's `.segmented` (home-automation#879, #888, and the trailing cluster from #884), to stop a third variant (`project-scaffolding#341`).

It replaces [`range-tab/`](../range-tab/), the older row of separate ghost pills with an accent active state, which is **deprecated**; a new choice control uses this one, and `range-tab/README.md` has the migration recipe.

## Files

| File | Role |
| --- | --- |
| `segmented.css` | Visual contract: the track, the segments, the raised selected segment, the disabled cursor, the vertical-only 44px band, the `--verbs` modifier. References design tokens only. |
| `segmented.html` | Markup skeleton to copy and adapt (a picker and a verbs cluster). |

## How to vendor

1. Copy this `segmented/` folder **verbatim** into your app's static dir (`app/webapp/static/_vendored/segmented/`). Do **not** edit `segmented.css` per-app.
2. Link the CSS:
   ```html
   <link rel="stylesheet" href="/static/_vendored/segmented/segmented.css">
   ```
3. Paste the skeleton, adapt the labels, the `data-*` values and the group's `aria-label`, and wire the selection (below).

## Markup contract

```html
<div class="segmented" role="group" aria-label="Period">
  <button type="button" class="segmented-item" aria-pressed="true" data-value="day">Day</button>
  <button type="button" class="segmented-item" aria-pressed="false" data-value="week">Week</button>
  <button type="button" class="segmented-item" aria-pressed="false" data-value="month">Month</button>
</div>
```

- **`.segmented`** is the track, a `role="group"` with an accessible name. It carries no outer margin, so place it with your own layout.
- **`.segmented-item`** is each segment, a `<button>`. Exactly one carries `aria-pressed="true"` and the rest `"false"`. Flipping it is the caller's job, because the driving value differs per use (a mode, a period, a URL key). There is no shared builder:
  ```js
  group.addEventListener('click', (e) => {
    const seg = e.target.closest('.segmented-item');
    if (!seg || seg.disabled) return;
    group.querySelectorAll('.segmented-item').forEach((b) => {
      b.setAttribute('aria-pressed', String(b === seg));
    });
  });
  ```
  When the choice writes to a server, flip it after the write lands, or roll it back on failure, so the raised segment never lies about the state.
- **Labels** are one word, or one glyph plus an accessible name (`<svg class="icon">` + `aria-label` on the button). Glyphs are `--icon-title` (18px). Labels never wrap.
- **`.segmented--verbs`** is for a choice that acts on the world (an alarm mode, a blind's Up · Stop · Down). Its segments are a real 44px row. A plain picker keeps the 36px control height.
- **Unavailable segments** carry `disabled` and a `title` saying why. They keep the `fg` label: `fg-muted` on the track falls under 4.5:1 (4.3:1 light, 3.8:1 dark). The cursor and the title say it is unavailable. Never use opacity.
- **Never red for a normal mode.** An armed alarm or a heating AC is the selected segment, not a `danger` fill. `danger` is for the emergency (design.md "Modes are not alarms").
- **At most five options.** More than five, or options that are not peers, become a `select-native` (`_vendored/select-native/`) in a settings row.

### As a row's trailing item

A segmented verb cluster can be an [`action-row`](../action-row/)'s one trailing item (design.md action-row). In that case it sits in the row's trailing slot, outside the row's tap target, at 44px per segment:

```html
<div class="segmented segmented--verbs action-row-trail" role="group" aria-label="Mode">…</div>
```

`action-row.css` sets each segment's width to 44px there. See that README.

## Targets and contrast

- Segments sit side by side, so each reaches the 44px floor (`--row-sm`) **vertically only**: a `::before` band of (segment height − 44px) / 2, clamped at 0. Neighbouring bands touch and never overlap (design.md "Touch targets", adjacent cluster). Width comes from the equal columns: five or fewer options across a phone-width card are each well over 44px.
- Every label is `fg` on the track (12.8:1 light, 11.9:1 dark, measured in the gallery), and the selected segment's label is `fg` on its fill (12.8:1 light on `card`, 9.6:1 dark). The gallery harness asserts AA for both, in both themes, that the dark selected fill is lighter than the track, plus the 44px targets.

## Required design tokens

| Token | Light value | Used for |
| --- | --- | --- |
| `--neutral-soft` | `color-mix(in srgb, var(--muted) 16%, transparent)` | track fill (`colors.neutral-soft`) |
| `--card` | `#ffffff` | selected segment fill in light; the base of the dark fill |
| `--ink` | `#1f2328` | every label (`fg`) |
| `--muted` | `#656d76` | resting hover tint (10% mix); the dark selected fill is `color-mix(in srgb, var(--muted) 30%, var(--card))`, the track's own tint a step stronger |
| `--control-h` | `36px` | picker track height (`components.control.height`) |
| `--row-sm` | `44px` | target floor and the `--verbs` track (`hit-target.min`) |
| `--radius-md` | `12px` | track corners (segments are 3px tighter) |
| `--font-label` | `0.875rem` | label text |
| `--icon-title` | `18px` | segment glyph (`icons.size.title`) |
| `--space-xs` | `4px` | glyph ↔ word inside one segment |

**Dark selected fill (`project-scaffolding#377`).** In dark, `card` is darker than the track (a muted tint over the card), so a `card` fill read pressed in. The dark rule, scoped to `[data-theme="dark"]`, mixes `--muted` 30% into `--card` instead (track 16%): lighter than the track over a card, the page background and a raised surface. It needs no token beyond the two above. `--card-2` is deliberately not used: over a card the track composites to `#262c34`, lighter than the dark `--card-2` (`#21262d`), so it would still read pressed in. An app that themes dark some other way than `[data-theme="dark"]` keeps the light `card` fill until it adopts that attribute.

## Don't diverge

`segmented.css` is vendored verbatim: to change the contract, change it **here in `project-scaffolding`** and re-vendor downstream. Don't colour the selected segment, fade the unselected ones, or add a sentence above the control to say which is on; those are the shapes this component exists to retire. If your own CSS declares a selector this file touches, use longhand properties or a disjoint media condition — a shorthand at equal specificity is decided by source order. Streamlit POC spikes are exempt.
