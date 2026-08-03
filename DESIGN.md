---
name: rotector-selfbot
description: A pure black-and-white data field where scale is the argument and the flagged few are the only marks that break it.
colors:
  ink: "#ffffff"
  ground: "#000000"
  rule: "#2a2a2a"
  texture-1: "#6f6f6f"
  texture-2: "#3a3a3a"
  quiet-stroke: "#9a9a9a"
  comment: "#8d8d8d"
  invert-rule: "#c4c4c4"
  invert-texture: "#b4b4b4"
  invert-label: "#555555"
  invert-comment: "#6a6a6a"
typography:
  display:
    fontFamily: "Azeret Mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "clamp(2.6rem, 7.6vw, 4.9rem)"
    fontWeight: 800
    lineHeight: 0.9
    letterSpacing: "-0.045em"
  display-thin:
    fontFamily: "Azeret Mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "0.43em"
    fontWeight: 300
    lineHeight: 1.12
    letterSpacing: "-0.03em"
  headline:
    fontFamily: "Azeret Mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "clamp(1.5rem, 3.4vw, 2.6rem)"
    fontWeight: 800
    lineHeight: 1.02
    letterSpacing: "-0.045em"
  title:
    fontFamily: "Azeret Mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "clamp(1.02rem, 1.5vw, 1.2rem)"
    fontWeight: 800
    lineHeight: 1.25
    letterSpacing: "-0.03em"
  lead:
    fontFamily: "Azeret Mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "clamp(1rem, 1.55vw, 1.2rem)"
    fontWeight: 300
    lineHeight: 1.55
    letterSpacing: "-0.02em"
  body:
    fontFamily: "Azeret Mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.62
    letterSpacing: "-0.01em"
  data:
    fontFamily: "Azeret Mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "0.82rem"
    fontWeight: 400
    lineHeight: 1.62
    letterSpacing: "-0.01em"
  figure:
    fontFamily: "Azeret Mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "1.5rem"
    fontWeight: 700
    lineHeight: 1.15
    letterSpacing: "-0.04em"
  label:
    fontFamily: "Azeret Mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "0.68rem"
    fontWeight: 500
    lineHeight: 1.62
    letterSpacing: "0.19em"
  body-compact:
    fontFamily: "Azeret Mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.62
    letterSpacing: "-0.01em"
  micro:
    fontFamily: "Azeret Mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "0.62rem"
    fontWeight: 400
    lineHeight: 1.9
    letterSpacing: "0.17em"
  action:
    fontFamily: "Azeret Mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "0.78rem"
    fontWeight: 700
    lineHeight: 1.62
    letterSpacing: "0.13em"
rounded:
  none: "0"
spacing:
  hairline: "1px"
  xs: "0.4rem"
  sm: "0.7rem"
  md: "0.9rem"
  lg: "1.5rem"
  xl: "1.75rem"
  gutter: "clamp(1.25rem, 4vw, 4rem)"
  section: "clamp(3.5rem, 8vw, 7rem)"
  rail: "13rem"
  measure: "68ch"
components:
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.ground}"
    typography: "{typography.action}"
    rounded: "{rounded.none}"
    padding: "0.85rem 1.15rem"
  button-primary-hover:
    backgroundColor: "{colors.ground}"
    textColor: "{colors.ink}"
  button-quiet:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    typography: "{typography.action}"
    rounded: "{rounded.none}"
    padding: "0.85rem 1.15rem"
  button-quiet-hover:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.ground}"
  node:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "1.1rem 0.9rem 1.3rem"
  node-header:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    typography: "{typography.label}"
    padding: "0.6rem 0.9rem"
  ledger-cell:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    typography: "{typography.data}"
    padding: "0.8rem 0.9rem 0.8rem 0"
  key-cap:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "0.1rem 0.4rem"
    width: "2.1rem"
  code-block:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "1rem 1.1rem"
  bar-rule:
    backgroundColor: "{colors.ground}"
    textColor: "{colors.ink}"
    height: "12px"
    width: "100%"
---

# Design System: rotector-selfbot

> **Scope: the web surface, and one borrowed palette.** This system governs `docs/index.html`,
> the project's public page, its `og.png` thumbnail, and any future web surface. It does **not**
> govern the application, which is a Python/Textual terminal UI with its own visual language
> (colour-coded verdict tiers, Textual widget conventions, terminal cell geometry). `PRODUCT.md`
> records the platform as `terminal` for exactly this reason. Do not carry the type ramp, the
> spacing scale or the breakpoints into the TUI, and do not read the TUI's conventions back into
> this file.
>
> The **two-value palette is the one exception**, and it travels in one direction only. Ink,
> ground, rule and the two ornament tones are mirrored in `rsb/tui/theme.py` as a Textual theme
> named `ten-thousand`, which a user picks from the command palette's *Theme* command. It is not
> the app's default and it does not replace the app's own language; it is an option that happens
> to be this page's. The page is the source and the theme is the copy — a change to the colours
> here is a change there, never the reverse.

## Overview

**Creative North Star: "The Field of Ten Thousand"**

The page is a wall of data made from the product's own facts. A seeded barcode canvas draws one
hairline bar for roughly every fourteen of a real 10,833-member scan, and fourteen findings are the
only marks that break it. Nothing here is a picture *of* scanning; scale is the argument, rendered
at scale. The world is Ikeda's Datamatics, chosen by the user over the assigned direction and then
fused with the product: the challenger supplied the form and the system grammar, and every number,
label and route on the page is a fact from the tool.

The palette is two values. Pure white ink on pure black ground, with no grey fills anywhere in the
page's own chrome. Tone is produced by *density* — bars per unit width on the canvas, mask stripe
frequency in the meters and chips — never by a flat mid-grey panel. When a passage needs to be read
at length rather than scanned, the entire frame inverts to black on white; there is no half-measure
surface between the two states. The one authored motion on the page is a single scan pass that
sweeps the field once on load and then stops.

The page deliberately refuses the default tool-marketing composition: no dark hero over a
gradient, no floating terminal mockup on a card, no glow, no colour accent doing the work of the
argument. The only colour that exists anywhere on the page lives inside the program's own captured
screenshots, and the figcaption says so out loud.

**Key Characteristics:**
- Two-value palette: pure `#ffffff` ink on pure `#000000` ground; no grey fills in page chrome.
- Tone comes from bar and stripe density, never from opacity-flattened surfaces.
- One typeface at all sizes: Azeret Mono, 300/400/500/700/800.
- Zero corner radius everywhere; every container is a hairline rectangle.
- Reading passages invert the whole frame rather than tinting a panel.
- Exactly one motion moment, on load, fully halted under reduced-motion.

## Colors

Two absolutes and a small set of hairline and ornament tones that exist only so the absolutes stay
absolute.

### Primary
- **Ink** (`#ffffff`): every content-bearing character on the page, every bar in the canvas field,
  every mask-driven meter and chip (they take `currentColor`), and the fill of the primary button.
  There is no second accent; the system's emphasis mechanism is weight and inversion, not hue.

### Neutral
- **Ground** (`#000000`): the page background and the fill the canvas clears to each frame. Set
  literally, not as a near-black.
- **Hairline Rule** (`#2a2a2a`): every structural border on the dark ground — section dividers,
  node frames, table row rules, code-block and key-cap strokes, the hero's rail separators. It is a
  line colour, never a fill.
- **Ornament Bright** (`#6f6f6f`) and **Ornament Dim** (`#3a3a3a`): the seeded numeral columns only
  — the hero's left id rail and its right readout body, and the `.index` edge column. These strings
  are generated digits, `aria-hidden`, and unselectable; they are texture with the shape of data.
- **Quiet Stroke** (`#9a9a9a`): the outline of the secondary button, which sits over the live canvas
  and needs a stroke that reads against both a dense and a sparse patch of field.
- **Comment Grey** (`#8d8d8d`): shell comments inside code blocks, which are commentary on the
  command rather than the command.

### Inverted Context
When a section carries `.invert`, ink and ground swap and the supporting tones swap with them:
borders become **Invert Rule** (`#c4c4c4`), ornament numerals become **Invert Texture** (`#b4b4b4`),
section labels become **Invert Label** (`#555555`), and code comments become **Invert Comment**
(`#6a6a6a`). Selection colours invert too. Nothing is left at a dark-mode value inside a light frame.

**Eight steps, and no more.** The build was consolidated to Display, Headline, Title, Lead, Body
(+ Body Compact), Data, Figure, Label, Action and Micro. An earlier draft carried thirteen literal
sizes, four of them inside 0.08rem of each other, which reads as noise rather than hierarchy. A new
literal font-size is drift until it earns a named step here.

### Named Rules

**The No Grey Fill Rule.** Grey is a line, a numeral, or a comment — never a surface. Tone in this
system is produced by density: bars per unit width on the canvas, stripe frequency in a mask
(`.meter`, `.chip`, `.tick`). If a design wants "a slightly lighter panel", it gets a hairline frame
or a bar-field instead.

**The Pure Ink Rule.** Every character a reader is meant to read is `{colors.ink}` at full opacity.
`--texture-1` and `--texture-2` are permitted only on `aria-hidden`, unselectable ornament columns
(`.ids`, `.index`) and on `pre .c` comments. A new element that wants a dimmed text colour is
either ornament — in which case hide it from assistive tech and make it unselectable — or it is
content, in which case it is pure ink.

**The Screenshot Colour Rule.** The page's own chrome is black and white with no exceptions. Colour
appears on this page only inside `docs/screenshots/*.svg`, which are genuine Textual exports of the
program, and the figcaption states this in-page. This is a deliberate adaptation of the source
world's no-colour rule: the program's colour is evidence, not decoration, and cropping it out would
have falsified the artifact. Never introduce colour into page chrome to "match" a screenshot.

The screenshots are now captured with the app in its `ten-thousand` theme, so the *program's* chrome
is two-value too and the pictures sit inside the page rather than beside it. This did not weaken the
rule, it sharpened it: what survives in colour is exactly the verdict column, the flag tiers and the
status line — the evidence — and nothing else. The route matters. The screenshots were not recoloured
or retouched; a theme was added to the program, and the program was photographed wearing it. If a
future change drains the verdict hues as well, the pictures stop being evidence and the rule is lost.
`tools/sitesvg.py` also replaces Rich's default export chrome (rounded window, macOS traffic lights,
grey ground) with the page's own Data Node frame, and refuses to run unless the theme is active.

## Typography

**Display Font:** Azeret Mono (with `ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`)
**Body Font:** Azeret Mono (same stack)
**Label/Mono Font:** Azeret Mono (same stack)

**Character:** One family carries everything, loaded at five weights (300/400/500/700/800) from
Google Fonts. Azeret is a wide, squared monospace, so display sizes are tracked to `-0.045em` —
past the usual `-0.04em` floor, deliberately, because at 4.9rem the family's default advance opens
gaps that read as spaced-out rather than set. Ligatures are disabled globally
(`font-variant-ligatures: none`) so ids, commands and flag names render character-exact.

### Hierarchy
- **Display** (800, `clamp(2.6rem, 7.6vw, 4.9rem)`, 0.9): the hero statement only, one per page,
  with a 300-weight subordinate clause nested inside at `0.43em` and capped at 22ch.
- **Headline** (800, `clamp(1.5rem, 3.4vw, 2.6rem)`, 1.02): the one sentence that opens each
  section. Sections have exactly one.
- **Title** (800, `clamp(1.02rem, 1.5vw, 1.2rem)`, 1.25): step headings inside a node.
- **Lead** (300, `clamp(1rem, 1.55vw, 1.2rem)`, 1.55): the hero paragraph, capped at 54ch. Light
  weight against the heavy display is the only tonal contrast the type ramp uses.
- **Body** (400, 15px, 1.62): all reading copy, capped at 68ch (`--measure`).
- **Body Compact** (400, 14px, 1.62): the same role below 620px. The only viewport-conditional
  step in the ramp.
- **Data** (400, 0.82rem): table cells and the two smaller supporting paragraphs.
- **Figure** (700, 1.5rem, tracked `-0.04em`): the standalone numerals in the hero readout rail —
  10,833, 14 — set as objects rather than as running text.
- **Label** (500, 0.68rem, `0.19em`, uppercase): section eyebrow-free column headings, table
  headers and captions, node headers. These are structural column labels attached to a data region,
  not standalone kickers over a headline.
- **Action** (700, 0.78rem, `0.13em`, uppercase): button faces and the wordmark.
- **Micro** (400/500, 0.62rem): the smallest step, and the floor. Two uses only, both structural:
  the aria-hidden ornament columns (`.ids`, `.index`), and the labels a data region generates for
  itself — node headers, and the `data-label` micro-headers the ledger emits when it stacks below
  620px. Never reading copy; anything a visitor must read starts at Label.

### Named Rules

**The One Family Rule.** Azeret Mono sets everything: display, body, tables, code, keycaps. Weight
(300 through 800) and tracking carry the entire hierarchy. A second family — including a serif for
"reading comfort" or a system UI face for chrome — is not available in this world.

**The Wide-Mono Tracking Rule.** Tracking tightens as size grows and loosens as size shrinks:
`-0.045em` at display, `-0.02em` at lead, `-0.01em` at body, `+0.13em` on actions, `+0.19em` on
labels. The negative display tracking is calibrated to Azeret's wide advance and is not a general
licence for tighter tracking on narrower faces.

## Layout

The page is a vertical stack of full-bleed sections separated by hairline rules
(`1px solid {colors.rule}`), each padded `clamp(3.5rem, 8vw, 7rem)` vertically by
`--gutter: clamp(1.25rem, 4vw, 4rem)` horizontally, with content constrained to a 1180px `.wrap`.
There is no card grid and no floating container; sections are bands.

**The hero** is a three-column grid — `13rem` id rail, fluid body, `13rem` readout rail — over a
full-bleed absolutely positioned canvas at `min-height: 100svh`. The rails sit *on* the canvas
rather than over a panel: legibility comes from a layered text-shadow halo (`0 0 7px/3px/1px #000`),
so the wall reads as one continuous surface instead of three stacked planes.

**Body sections** use a 5fr/7fr `.split` — argument left, evidence right — with
`clamp(1.75rem, 4vw, 4rem)` gutters and `align-items: start`. Vertical rhythm inside a column is a
flat `1.5rem` (`.stack > * + *`); paragraph spacing is `1.15em`.

**Breakpoints and their behaviour:**
- **1180px** — rails narrow to `10.5rem` and their type drops to `.62rem`.
- **980px** — the hero collapses to one column, the left id rail is removed entirely, and the right
  readout becomes a three-across horizontal strip below the type. The canvas responds too: bar
  weight drops to 34% and the finding marks relocate to the clear band between the actions and the
  readout, so nothing readable or clickable is struck through.
- **860px** — the `.index` edge column is removed and `.indexed` collapses to a single column.
- **620px** — body drops to 14px, the readout strip goes two-across, steps unstack their number
  gutter, and the ledger reflows (see Components).

**The Continuous Field Rule.** Nothing sits on an opaque panel over the hero canvas. Elements that
need to survive the live field use a text-shadow halo, not a background. The field is one wall.

## Elevation & Depth

There is no elevation. Zero `box-shadow` declarations exist in the build. Depth is entirely
optical: the hero canvas has a density envelope (dense at the edges, thinning through the middle
where the reading sits, `pow(|x-0.5|*2, 1.5)`), lit bars sit at full alpha while unlit bars sit at
22%, and the finding marks brighten toward the edges. That gradient of density *is* the depth model.

The only shadow-family declarations in the build are `text-shadow` halos, and they are legibility
devices, not elevation: `0 0 7px #000, 0 0 3px #000, 0 0 1px #000` on the hero rails and
`0 0 6px #000, 0 0 2px #000` on the quiet button, both removed the moment the element gets a solid
background (the quiet button drops its halo on hover).

**The Flat Field Rule.** Surfaces never lift. A container is separated from its neighbour by a
hairline rule or by a full-frame inversion, never by a shadow, a blur, or a tint.

### Motion

One authored moment: a seeded scan pass across the hero canvas on load, 2200ms, exponential
ease-out (`1 - (1-p)³`), starting from an already-visible field and leaving the findings standing.
It does not loop. There are no hover animations anywhere; the only transitions in the stylesheet are
the buttons' 180ms `cubic-bezier(.16, 1, .3, 1)` background/colour swap. Under
`prefers-reduced-motion: reduce` all animation and transition is disabled globally and the canvas
draws its final state directly — and a `change` listener on the media query honours a mid-session
switch in either direction.

## Shapes

Every corner is square. There is not a single `border-radius` in the build, including on buttons,
keycaps, code blocks and node frames; `rounded.none` (`0`) is the only radius token and it is the
whole scale.

The recurring silhouette is the **hairline rectangle**: a `1px` frame in `{colors.rule}` with no
fill, optionally split by a header rule. Above that sits the **bar-field**, a repeating 24×12px
barcode gradient used as the page's horizontal divider, and the **mask stripe**, a repeating linear
gradient used as a `mask-image` over a `currentColor` block to produce meters, node chips and the
button ticks. Both are made of the same material — parallel vertical lines at varying frequency —
so a density meter and a section divider are visibly the same family.

## Components

### Buttons
- **Shape:** square (`0` radius), `1px` outline, `0.85rem 1.15rem` padding, uppercase 700 at
  `0.13em`.
- **Primary:** solid ink on ground text, flanked by two `34×11px` barcode ticks that are masked
  `currentColor` and therefore invert with the button.
- **Hover / Focus:** the fill inverts to transparent ground with ink text over the retained ink
  border, 180ms `cubic-bezier(.16, 1, .3, 1)`. `:focus-visible` gets the same treatment plus the
  global `2px` ink outline at `3px` offset.
- **Quiet (secondary):** transparent with a `{colors.quiet-stroke}` border, 500 weight, and a text
  halo so it survives the live canvas. On hover/focus it fills ink, takes ground text, drops the
  halo, and its border promotes to full ink.

### Cards / Containers — the Data Node
- **Corner Style:** square (`0`).
- **Background:** none. The node is a frame, not a surface.
- **Border:** `1px solid {colors.rule}` all round, with the header separated by the same rule.
- **Header:** a `0.62rem`/`0.21em` uppercase label followed by a flex-filling barcode chip
  (`10px` tall, masked `currentColor` at 50% opacity) that consumes the remaining width — so the
  header reads as a labelled data channel rather than a card title.
- **Internal Padding:** `1.1rem 0.9rem 1.3rem` body, `0.6rem 0.9rem` header; both tighten below
  620px.
- In an inverted section, all node borders shift to `{colors.invert-rule}`.

### Tables — the Verdict Ledger
- Borderless except for `1px` top rules on each row; left-aligned; `0.82rem`; header row is
  uppercase 500 at `0.17em`; the row-header cell is 800 weight and `nowrap`.
- Each evidence cell carries a **meter**: a `74×9px` `currentColor` block masked by a repeating
  stripe whose duty cycle encodes strength — 3/5 for THREAT down to 1/24 for UNKNOWN. The bar is
  `aria-hidden`; the text beside it carries the meaning.
- **Below 620px** the table stops being a table: the header row is visually hidden (clip-path
  inset), each row becomes a block, and each cell prints its own column name from `data-label` via
  `::before` as a `0.62rem`/`0.17em` uppercase line. Four columns cannot survive a 390px viewport;
  labelled pairs can.
- Horizontal overflow above that breakpoint is handled by a scroll wrapper with a `34rem` minimum.

### Code Blocks
- `1px` rule frame, no fill, `1rem 1.1rem` padding, inherited family at `0.8rem`/1.75, horizontal
  scroll. Commands are set in ink; trailing shell comments take Comment Grey and are aligned by a
  `36ch` min-width command span that becomes a stacked block below 620px.
- Inline code takes a `1px` bottom rule rather than a filled chip — consistent with the no-grey-fill
  rule.

### Keys
- `kbd` is a square `1px`-framed cap, 700 weight at `0.78rem`, `2.1rem` minimum width, zero
  tracking, in an auto-filling `230px` minimum grid with `1px` top rules per row.

### Signature: the Bar-Field Rule (`.bars`)
A `12px`-tall block element whose background is a repeating 24px barcode gradient at 55% opacity —
five ink strokes of varying width per repeat. It is the page's divider wherever a divider needs to
carry weight rather than merely separate (top and bottom of the inverted warning, above the setup
close, above the footer). In an inverted section it re-declares its gradient in ground-black at full
opacity rather than inheriting a washed-out white.

*Note for future maintenance:* an automated check flags this element as a "decorative grid-line
background". It is not a defect. The bar-field is this world's own divider — the chosen form
language is literally made of parallel rules — and it was reviewed and kept deliberately. Do not
"fix" it into a plain `1px` hr.

### Signature: the Seeded Field (`#field`)
A full-bleed canvas behind the hero, drawn from a `mulberry32` PRNG at fixed seeds so it is the same
wall on every visit rather than noise. Bar count is `MEMBERS / 14` from the README's real 10,833
scan; fourteen brighter marks with cross-ticks are the findings. A single sine trace at 34% alpha
crosses the whole wall. On the white ground of the setup section the same sine returns as an inline
SVG stroke (`.wave`, 46px tall, 55% opacity) — the one motif that crosses the inversion.

### Signature: the Index Column (`.index`)
A `4.5rem` seeded numeric edge column with a right hairline rule, `0.6rem`/1.9, in Ornament Dim,
`aria-hidden` and unselectable — the hero's id rail carried down the page edge as an alignment mark
for a data node. Removed entirely below 860px.

## Do's and Don'ts

### Do:
- **Do** build tone from density — bar frequency, mask duty cycle — rather than from a fill value.
- **Do** invert the entire frame (`.invert`) when a passage is meant to be read at length; black on
  white is this system's reading mode and its emphasis device at once.
- **Do** swap every supporting tone when you invert: rules to `#c4c4c4`, ornament to `#b4b4b4`,
  labels to `#555555`, comments to `#6a6a6a`, and selection colours with them.
- **Do** keep every ornamental numeral column `aria-hidden`, `user-select: none`, and seeded — it
  must be reliably the same texture, and never mistakable for data.
- **Do** give an element over the hero canvas a text-shadow halo instead of an opaque panel.
- **Do** derive any number shown as texture from a real measured fact (10,833 members, 14 findings);
  where a figure is not measured — per-route coverage, for instance — show none.
- **Do** reach for weight (300 vs 800) and inversion for emphasis, since there is no accent hue.
- **Do** keep motion to the one load-time scan pass, and keep the `prefers-reduced-motion` `change`
  listener intact when touching the canvas script.

### Don't:
- **Don't** introduce a grey fill, tint, or translucent panel. Frames and bar-fields only.
- **Don't** apply a texture tone (`--texture-1` / `--texture-2`) to any text a reader is meant to
  read.
- **Don't** add an accent colour to page chrome. Colour on this page exists only inside the
  program's own screenshots, and the figcaption says so.
- **Don't** add a `border-radius`. The scale is `0` and that is the entire scale.
- **Don't** add a `box-shadow`. Depth here is density, not elevation; the only shadow-family
  declaration permitted is a `text-shadow` halo for legibility over the live canvas.
- **Don't** introduce a second typeface, including a system UI or display face for chrome.
- **Don't** add hover animation, parallax, scroll-triggered reveals, or a looping ambient
  animation. One authored moment, on load.
- **Don't** set a standalone uppercase kicker or eyebrow above a headline. The uppercase label style
  exists only as a structural column/table/node-region heading attached to a data element.
- **Don't** use glyph or icon-font characters as icons. The page's only marks are drawn geometry —
  masked bar strips and an SVG path.
- **Don't** let the ledger fracture below 620px; keep the `data-label` stacked-pair reflow.
- **Don't** "fix" the bar-field divider into a plain rule to satisfy a decorative-background check.
- **Don't** apply anything in this file to the Textual TUI under `rsb/`.
