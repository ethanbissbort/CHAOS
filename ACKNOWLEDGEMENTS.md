# Acknowledgements

Project CHAOS (`homestead-twin`) is licensed under Apache-2.0. It contains no
third-party code, but its operator console borrows ideas — and in one case a
technique — from two MIT-licensed projects. This file records what was taken
from where, both because the MIT licence asks for attribution to travel with
derived work and because the people who did the thinking deserve the credit.

Neither project's source is vendored, copied or redistributed here. What follows
is influence, described precisely enough that a reader can go and check.

---

## Reticle

- **Project:** Reticle — "the infrastructure diagram you can operate"
- **Author:** Matt Anderson (MA Software LLC) — <https://github.com/mannders00/reticle>
- **Licence:** MIT. `Copyright (c) 2026 Matt Anderson (MA Software LLC)`
- **Version consulted:** 1.2.1

### What was drawn from it

`src/homestead_twin/web/tokens.css` follows the structure and the stated
philosophy of Reticle's `src/styles/base.css`:

- **A single explicit token layer, no Tailwind, no build pipeline.** Reticle's
  header says it plainly: "Hand-tuned dark theme. No Tailwind: we want precise
  control over the premium aesthetic, and avoid a build pipeline." CHAOS has the
  same constraint for a different reason — the console has to work with the
  uplink down — and arrives at the same answer.
- **Ranked scales rather than named one-offs.** Reticle's `--bg-0..3`,
  `--stroke-1/2/strong` and `--text-1/2/3/faint` are the model for CHAOS's
  `--surface-0..3` / `--surface-sunken` / `--surface-input`, `--stroke-1/2/strong`
  and `--text-1/2/3`. The idea being copied is that a surface's *rank* is the
  token, not its use site, so a new panel does not need a new colour.
- **A status scale with soft companions.** Reticle pairs `--ok/--warn/--err/--unknown`
  with `-soft` variants for tinted backgrounds. CHAOS keeps that pattern and
  widens the vocabulary to the eleven words its API actually speaks
  (`--status-*` and `--sev-*`, each with a `-soft`).
- **Shape, motion and elevation as tokens.** `--radius-sm/-lg/-pill`,
  `--dur-fast/--dur/--dur-slow`, a single `--ease` curve, and `--shadow-1/2/3`
  are taken more or less directly in shape from `base.css`, with CHAOS's own
  values.
- **Focus as a first-class token, not an afterthought.** Reticle's
  `--shadow-focus` and its `button:focus-visible { box-shadow: var(--shadow-focus) }`
  are the origin of CHAOS's `--focus`, `--focus-ring-width`, `--focus-ring-offset`
  and `--focus-halo`, and of the rule that every interactive element keeps a
  visible focus state.

Chrome details in `src/homestead_twin/web/styles.css` follow Reticle's
`chrome.css`, `inspector.css`, `node.css`, `canvas.css` and `terminal.css`:

- the flat panel + 1px hairline + radius treatment for cards
  (`inspector.css` `.inspector-card`);
- toolbar controls that shift *surface* on hover and press down 1px on
  `:active` (`chrome.css` `.tool-btn`);
- the status/toolbar strip as a bordered, quieter band than the content
  (`chrome.css` `.statusbar`);
- the idea, from `node.css`, that a card can carry its category as a left
  keyline — used in CHAOS for `.card[data-status="alarm"|"degraded"]` and
  `.alarm-sev-bar`;
- subtle scrollbar styling from `base.css`.

### What was deliberately not taken

Reticle's palette is blue-accented and its type scale is fixed at 14px. CHAOS
keeps its own green accent, its own `--fs-base` that grows for wall-display
mode, and a much larger contrast budget: Reticle is a desktop app read at arm's
length, CHAOS is read from across a shipping container.

---

## Rackula

- **Project:** Rackula — drag-and-drop rack visualiser
- **Author:** Gareth Evans (@ggfevans) — <https://github.com/RackulaLives/Rackula>
- **Licence:** MIT. `Copyright (c) 2026 Gareth Evans (@ggfevans)`
- **Version consulted:** 26.7.0

### What was drawn from it

The `--cat-*` category scale in `tokens.css` follows Rackula's device-fill
palette in `src/lib/types/constants.ts` (`CATEGORY_COLOURS`) and
`src/lib/constants/colourPresets.ts`:

- **The muted register, and the reason for it.** Rackula's palette is a
  *desaturated, darkened* Dracula set rather than the bright Dracula accents,
  because a device renders its name in near-white (`--neutral-50`) on top of its
  fill and the bright accents fail WCAG AA against that label (their issue
  #3005). CHAOS's category scale exists as a separate scale from `--accent` and
  `--sev-*` for exactly that reason, is solved to the same constraint, and says
  so at the top of `tokens.css`.
- **A named label colour that the palette is solved against.** Rackula's
  `--neutral-50` becomes CHAOS's `--cat-label`, documented as the only text
  colour guaranteed legible on a category fill.
- **Categories, not classes, as the unit of colour**, with passive/unknown
  things falling back to a neutral. Rackula splits "active categories" (muted
  Dracula) from "passive categories" (Dracula neutrals); CHAOS has fourteen
  families and a `--cat-fallback`.

The bigger borrowing is the **testing technique**, from
`src/tests/colour-presets-contrast.test.ts`:

> "This asserts every preset actually clears 4.5:1 against that label colour via
> a computed contrast ratio, not a hardcoded hex comparison, so a future preset
> addition that regresses contrast is caught."

`tests/test_design_tokens.py` is the CHAOS answer to that test. It parses
`tokens.css`, resolves `var()` aliases the way the browser does, composites
translucent tints over their real backgrounds, and computes the WCAG 2.1
contrast ratio for every foreground/background pairing the console renders, in
both themes. As in Rackula, nothing is asserted as a hex literal: a colour is
allowed to change, a ratio is not allowed to fall.

CHAOS extends the idea in two directions its own domain demanded — Lab colour
separation, and the same separation re-measured under simulated protanopia,
deuteranopia and tritanopia — so that the platform's load-bearing distinctions
(`stale` is not `ok`; `no_data` is not a healthy zero) survive an operator who
cannot tell green from amber.

### What was deliberately not taken

Rackula's actual hex values are not used. CHAOS's asset classes are not
Rackula's device categories (there is no `pump`, `valve`, `tank` or `inverter`
in a rack designer), the surfaces they sit on are different, and the palette was
re-solved from scratch against CHAOS's own `--cat-label` and `--surface-*`.
Rackula's Dracula heritage does not follow.

---

## Standards and references

- WCAG 2.1 relative-luminance and contrast-ratio formulae (W3C), implemented in
  `tests/test_design_tokens.py`.
- Machado, Oliveira & Fernandes (2009), *A Physiologically-based Model for
  Simulation of Color Vision Deficiency* — the severity-1.0 matrices used for the
  colour-vision-deficiency checks in the same file.
- CIE 1976 L\*a\*b\* and ΔE\*ab, used for the colour-separation assertions.
