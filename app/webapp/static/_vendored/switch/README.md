# `switch` — the one boolean control

The fleet's canonical **switch** (shadcn Switch shape): a compact 44×26 track + 20px sliding thumb, no text label — state is read from thumb position + track color, with `role="switch"` + `aria-checked` for assistive tech. **The on-track is green (`success`)** — the fleet default per `design.md` v2; a state toggle may substitute another status color only where that state carries its own meaning. One canonical size everywhere; never a native checkbox for on/off. Contract: `~/.claude/design.md` → `switch` token block + "Components" prose.

## Files

| File | Role |
| --- | --- |
| `switch.css` | Visual contract: track, thumb, on/disabled states, the 44px tap-target extension. References design tokens only. |
| `switch.js` | ESM builder — `switchEl(on, opts)` + `setSwitch(btn, on)`, the one write path so class and `aria-checked` can never drift apart. |
| `switch.html` | Static markup skeleton (prefer the builder). |

## How to vendor

1. Copy this `switch/` folder **verbatim** into your app's static dir. Do **not** edit `switch.css` / `switch.js` per-app.
2. Link the CSS:
   ```html
   <link rel="stylesheet" href="/static/_vendored/switch/switch.css">
   ```
3. Build from JS:
   ```js
   import { switchEl, setSwitch } from '/static/_vendored/switch/switch.js';
   const sw = switchEl(device.on, {
     label: 'Power ' + device.name,
     onToggle: (next, btn) => applyPower(device, next),  // you confirm, then:
   });
   row.appendChild(sw);
   // …after the backend confirms:
   setSwitch(sw, confirmedState);
   ```
   `onToggle` receives the *requested* state — optimistic flips are the caller's choice. Guard against double-taps with a busy flag while the request is in flight (home-automation#368).

## Markup contract

```html
<button type="button" class="toggle [on]" role="switch"
        aria-checked="true|false" aria-label="Power Desk lamp">
  <span class="knob"></span><span class="toggle-label">ON|OFF</span>
</button>
```

- The `.toggle-label` is visually hidden by contract (`display: none`) — the label text exists for legacy/robustness only; ARIA carries the state.
- Disabled: set the `disabled` attribute (opacity recipe in the CSS).
- The visible track is 26px; a `::before` extends the tap target to the 44px floor.

## Required design tokens

| Token | Light value | Used for |
| --- | --- | --- |
| `--toggle-track` | `var(--line)` (`#d1d9e0`) | off-track fill |
| `--toggle-knob` | `#ffffff` (light) / `var(--ink)` (dark) | thumb fill |
| `--on` | `#1a7f37` | on-track fill (`colors.success` — the green decision) |
| `--radius-pill` | `9999px` | track corners |

## Don't diverge

`switch.css` / `switch.js` are vendored verbatim — to change the contract, change it **here in `project-scaffolding`** and re-vendor downstream. In particular, don't re-inline the four-line render snippet per view file (the duplication this builder removes) and don't flip the on-track back to the accent — green is a recorded design decision. If your own CSS declares the same selector this file touches (e.g. `.app`, `.card`), use longhand properties or a disjoint media condition — a shorthand property at equal specificity is decided by source order, and can silently override a rule you didn't intend to touch. Streamlit POC spikes are exempt.
