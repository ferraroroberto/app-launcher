# `button` — the four-tier action vocabulary

The fleet's canonical **button**: four tiers covering every action, settled by the button-tier sweep (fleet-config#296) after a deterministic CSS parse of ~135 button rule variants across 7 apps found the same class names meaning different buttons per app. Contract: `~/.claude/design.md` → "Component contracts" → **button tiers**.

- `.button-primary` — solid `accent-fill`. The one main action per view.
- `.button-tint` — accent-soft fill, `accent-text` text, soft accent border. Secondary emphasis. **A tint is not a ghost.**
- `.button-ghost` — transparent fill, hairline border, muted text. Quiet tertiary actions. **Ghost means transparent** — a tinted fill is a tint, never a "ghost".
- `.button-surface` — the control fill (`--input-bg`: `card-off` in light, `border-muted` `#21262d` in dark, never a black well) at control height; the dark label is `--ink` (`fg`, 12.88:1), not `--muted` (4.08:1) (fleet-config#1375, project-scaffolding#358). Labelled toolbar/utility buttons. A glyph-only control is not a button tier: it is [`icon-button/`](../icon-button/), unpainted at rest (fleet-config#1259).
- `.button-tint.danger` — the **outlined** destructive recipe: transparent fill, `--danger-text` label, `--deficit` hairline border. Never a fill or tint, the final Delete of a confirm dialog included (design.md, fleet-config#1403; project-scaffolding#379). The class keeps its historical name so apps' existing markup keeps working after a re-vendor; only its look changed.
- Text on a tint is always the `*-text` token and the primary fills with `--accent-fill` (fleet-config#963): the base hue drops under AA on its own tint, and white on the dark base accent is 3.75:1.
- One shared `:disabled` recipe applies to all four tiers (home-automation#362) — the flat control-fill/line/muted trio, never opacity on a solid fill. The disabled fill is `--input-bg`: `card-off` in light, `border-muted` (`#21262d`) in dark, so a disabled button never reads as a black well on a dark card (fleet-config#1368, project-scaffolding#356).

## Files

| File | Role |
| --- | --- |
| `button.css` | Visual contract — the four tiers, the outlined danger variant, the shared disabled recipe, the busy state. References design tokens only. |
| `busy.js` | `runBusy(button, work, opts)`: sets the busy state for a slow action, ignores a second tap in flight and reports the outcome through the [`toast`](../toast/). |
| `button.html` | Markup skeletons to copy and adapt. |

## How to vendor

1. Copy this `button/` folder **verbatim** into your app's static dir (`app/webapp/static/_vendored/button/`). Do **not** edit `button.css` per-app.
2. Link the CSS and use the class that matches the action's weight:
   ```html
   <link rel="stylesheet" href="/static/_vendored/button/button.css">
   ```
3. For a slow action, import `busy.js` and vendor `toast/` beside it (see "Busy state and `runBusy`").

## Markup contract

```html
<button type="button" class="button-primary">Save</button>
<button type="button" class="button-tint">Run now</button>
<button type="button" class="button-ghost">Cancel</button>
<button type="button" class="button-surface">Options</button>
<button type="button" class="button-primary" disabled>Save</button>
<button type="button" class="button-tint danger">Delete</button>
```

- Exactly **one** `.button-primary` per view — secondary emphasis is `.button-tint`, never a second solid fill.
- Disabled is the plain `disabled` attribute on any tier, not a separate class — the shared recipe handles the look.
- `.danger` only composes with `.button-tint` and is outlined, so use it for every destructive verb, a confirm dialog's Delete included (the vendored recipe covers that one combination; a primary/ghost/surface destructive action is out of scope until a real usage shows up).

## Busy state and `runBusy` — a slow action says it is working

A button that starts slow work (Refresh, Sync, Run, Save) shows it is working instead of looking dead. `[aria-busy="true"]` on any tier:

- keeps the label and draws an inline spinner before it, in place of a leading `.icon` glyph (hidden while busy);
- takes the disabled recipe's flat control fill and `--line` border, never opacity on a solid fill, so it reads the same in light and dark;
- keeps the label at AA — `--muted` in light (4.93:1), `--ink` in dark (12.88:1) — because a busy control is still saying what is happening, unlike a disabled one;
- ignores taps (`pointer-events: none`, `cursor: progress`) but is **not** `disabled`: focus stays on it and a screen reader hears the busy state;
- stops the ring turning under `prefers-reduced-motion`; the dimmed fill and the open arc still read as working.

`runBusy` owns the whole sequence, so an app never hand-rolls an in-flight flag:

```js
import { runBusy } from '/static/_vendored/button/busy.js';

refreshBtn.addEventListener('click', () => runBusy(refreshBtn, () => api.refresh(), {
  start: 'Refreshing',          // optional: shown while the work runs
  done: 'Refreshed',            // string, or (result) => string
  fail: 'Refresh failed',       // lead text; the error's own message follows
}));
```

- A second call for a button that is already busy runs nothing and resolves `{ ok: false, ignored: true }`.
- Busy is cleared in `finally`: on success, on a rejection and on a synchronous throw.
- A failure always toasts as an error with the error's text (`Refresh failed: timed out`), even with no `fail`; `fail` may be a function that builds the whole text.
- With `start` and no `done`, the start toast is dismissed when the work ends.
- The promise never rejects: it resolves `{ ok: true, value }` or `{ ok: false, error }`.
- `opts.host` is a sheet's own toast element, as for `showToast`, for a button inside an open dialog.

`busy.js` imports `../toast/toast.js`, so an app that uses it vendors `toast/` beside `button/`.

## Action → variant (one component per action)

The design-review rule `INV-02` fails when one normalised action is drawn by more than one component or variant across apps. This table is the single answer: pick the row for the action, use that component, and never invent a local class for it. Sizes are what each recipe renders (`--font-body` 16px, `--font-label` 14px, `--font-caption` 12px).

| Action | Component and variant | Renders | Where it goes |
| --- | --- | --- | --- |
| commit (save, apply, confirm, add, create, submit) | `.button-primary` | 16px / 700, 48px | The one main action per view; a dialog's footer commit, full width |
| opener (an "Add …" or "New …" control that opens a dialog, sheet or menu instead of writing anything) | `.button-tint` | 16px / 700, 48px | Wherever it sits; the commit lives inside what it opens, so the view's one primary stays free for its own main action |
| secondary run (run now, start, export) | `.button-tint` | 16px / 700, 48px | A second action of real weight beside the commit |
| cancel (back out of a form or dialog without a verb) | `.button-ghost` | 12px / 600, 36px | Beside a commit in a footer |
| dismiss (close a sheet, dialog, banner or toast) | `icon-button` with the `x` glyph | glyph only | The header corner of the sheet; never a labelled button |
| destructive (delete, remove, discard, clear) | `.button-tint.danger` | 16px / 700, outlined | A dialog or confirm footer, a menu's danger item; on a list row it is an `icon-button danger` |
| copy | `.button-surface` | 14px / 600, 36px | A labelled toolbar copy; inside a [`code-listing`](../code-listing/) header it is the unpainted `icon-button` |
| refresh (reload, re-scan) | `.button-surface` | 14px / 600, 36px | A toolbar above the data it reloads |
| edit | `icon-button` with the `pencil` glyph | glyph only | A row or card header verb |
| filter | `icon-button` with the `sliders-horizontal` glyph | glyph only | Opens the filter sheet; a filter *field* above a list is the [`action-row`](../action-row/) one |
| search | `icon-button` with the `search` glyph | glyph only | The header verb that opens search; the submit inside the search field is a commit |
| overflow menu | `icon-button` with the `ellipsis-vertical` glyph | glyph only | The row's kebab, opening [`row-menu`](../row-menu/) |
| row open | [`action-row`](../action-row/) button | row | The whole row is the tap target; a link row differs only in its element |

Rules that follow from the table:

- An action never moves between components for taste, size or space: a sheet too narrow for a labelled copy button keeps `.button-surface` and wraps, it does not swap to an icon.
- A glyph-only control is always `icon-button`, a labelled one is always a tier. The same action is two components only where the table says so by *context* (a copy inside a code listing, a destructive on a list row), never per app.
- A bespoke local class for any action above (`.save-btn`, `.close-x`, `.refresh-btn`, a hand-built kebab) is the defect `INV-02` and `INV-03` report: re-vendor and use the row's component.
- The verb does not decide the tier, what the tap does does: an "Add …" that opens a dialog or menu is an opener (`.button-tint`), and only the control that actually writes (the dialog's own "Add" footer) is the commit and takes `.button-primary`. An opener never counts as a view's primary, so one primary per view still holds.
- An action not in the table (rename, share, pin, record) takes the tier that matches its weight: primary only if it is the view's one main action, otherwise tint, ghost or surface, or `icon-button` when glyph-only.

The live demo is the "action to variant" card of `demo.html`.

## Bespoke buttons: which component each shape moves to

The design-review rule `INV-03` (button) lists every `<button>` that no vendored component class draws. Most of that volume is not a mislabelled action, so each shape falls in one of three buckets, read from the apps' markup and the newest per-screen inventory of each. The count is per control per screen, so a tile repeated in a list counts once per row.

**Bucket 1: a button tier already covers it.** A labelled control that does one action in the table above. Move it onto the tier and delete the local class.

| Bespoke shape | Moves to |
| --- | --- |
| A labelled utility or toolbar action (`Choose file`, `Re-check`, `Pause`, `Start <service>`, `All on`, `All off`, `Show hidden`, `Redo transcription`, a drawer's `Open the chat view`) | `.button-surface` |
| A drawer or sheet's one main verb beside those | `.button-tint` |
| A footer link-style control that switches to another view (`All activity`) | `.button-ghost` |
| A danger row in a sheet (`Reboot`, `Shut down`, `Force stop`) | `.button-tint.danger` |
| `Clear` of an unsaved form value (a chosen attachment, a draft) | `.button-ghost`: nothing is deleted, so it backs out like a cancel; `.button-tint.danger` only when stored data goes |

**Bucket 2: another vendored component covers it.**

| Bespoke shape | Moves to |
| --- | --- |
| A whole-row tap target with a title and one context line (a session row, a type row, a search hit) | [`action-row`](../action-row/) |
| A single-choice filter of 2–5 peers drawn as pills (`All`, `Needs checking`, `Checked`) | [`segmented`](../segmented/) |
| A native choice of 6+ options | [`select-native`](../select-native/) |
| A toggle chip: a pill with `aria-pressed` that joins or leaves a filter or a day set, count or label inside (store pills, weekday pills, a zone strip) | [`chip`](../chip/) |

A row that carries a trailing chevron keeps it as it is: the chevron is not part of this mapping.

**Bucket 3: no component covers it.** A shape that recurs in two or more apps gets a scaffold component (the toggle chip did, and is now `chip` in bucket 2); a shape that lives in one app stays app-owned.

| Shape | Decision |
| --- | --- |
| A stat tile that is a button (a summary tile that navigates, a metric tile that selects a trend) | Two apps but two jobs (navigate vs select): app-owned. A third app with the same job reopens this. |
| A data row with a share bar, a snippet or a selected state (a folder tree node, a contents row, a search result `role="option"`) | One app: app-owned |
| A readout that opens a sheet (a CPU reading, a usage meter), a combobox trigger with per-option notes, a floating "jump to latest" pill, a hero record toggle, a date quick-pick tile | One app each: app-owned |

An app-owned shape still draws from the design tokens and keeps its 44px hit target; it is the one place a local button class is allowed. Re-run `/design-review <app>` after a move: the shapes left on the list must all be in bucket 3.

## Legacy-class migration

Per-app adoption is tracked separately (fleet-config#279) — this table is the mapping when a consuming app re-skins its own buttons to the vendored classes:

| Legacy class | Vendored class |
| --- | --- |
| `.detail-save-btn` | `.button-primary` |
| `.big-btn` | `.button-tint` |
| `.ghost-btn` | `.button-ghost` |
| `.shaded-btn` | `.button-surface` |

## Required design tokens

Define these CSS custom properties in your app's `:root` / `[data-theme="dark"]` blocks, **wired to `~/.claude/design.md` (+ `design.dark.md`)**. Reference values (light):

| Token | Light value | Used for |
| --- | --- | --- |
| `--accent` | `#0969da` | base the soft tints mix from |
| `--accent-fill` | `#0969da` (dark `#1f6feb`) | primary fill |
| `--accent-text` | `#0550ae` (dark `#58a6ff`) | tint text |
| `--accent-fg` | `#ffffff` | primary text |
| `--accent-soft` | `color-mix(in srgb, var(--accent) 16%, transparent)` | tint fill |
| `--accent-border-soft` | `color-mix(in srgb, var(--accent) 24%, transparent)` | tint border |
| `--accent-border-strong` | `color-mix(in srgb, var(--accent) 28%, transparent)` | primary border |
| `--card-off` | `#f6f8fa` | fallback fill when `--input-bg` is undefined |
| `--input-bg` | `#f6f8fa` (dark `#21262d`) | surface fill and disabled fill (all tiers); falls back to `var(--card-off)` if undefined |
| `--ink` | `#1f2328` (dark `#e6edf3`) | surface label, dark theme only (`[data-theme="dark"]`) |
| `--line` | `#d1d9e0` | ghost/surface border, disabled border |
| `--muted` | `#656d76` | ghost text, light surface text, disabled text |
| `--deficit` | `#cf222e` (dark `#f85149`) | danger border (outlined destructive button) |
| `--danger-text` | `#a40e26` (dark `#ff7b72`) | danger label (7.87:1 light / 6.86:1 dark on the card) |
| `--radius-md` | `12px` | corners (`rounded.md`), all tiers |
| `--control-h` | `36px` | ghost and surface tier height; their `::before` grows the tap target to 44px vertically (36 + 2x4), so a stack needs a gap of at least 8px |
| `--font-body` | `1rem` | tint text |
| `--font-label` | `0.875rem` | surface text |
| `--font-caption` | `0.75rem` | ghost text |

## Don't diverge

`button.css` is vendored verbatim — to change a tier's recipe, change it **here in `project-scaffolding`** and re-vendor downstream. The modal's footer primary is a `.button-primary` (`_vendored/modal/` links this file; its `.detail-save-btn` only adds full width), so the spec's 48px height, never `--control-h` (#280), lives here once. If your own CSS declares the same selector this file touches (e.g. `.app`, `.card`), use longhand properties or a disjoint media condition — a shorthand property at equal specificity is decided by source order, and can silently override a rule you didn't intend to touch. Streamlit POC spikes are exempt.
