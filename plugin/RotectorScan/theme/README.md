# Ten Thousand — a Discord theme

Two `.theme.css` files that restyle **Discord itself** into the design language
of [rotector-selfbot](https://github.com/mireyacs/rotector-selfbot): pure white
ink on pure black ground, hairline rules, square corners everywhere, one
monospace family, and no colour anywhere except where colour is evidence.

* `ten-thousand.theme.css` — the dark frame.
* `ten-thousand-light.theme.css` — the same frame inverted: black ink on white
  ground, `#c4c4c4` rules, `#b4b4b4` ornament, `#555555` labels, `#6a6a6a`
  commentary, and selection colours swapped with the rest.

Install **one of them**, not both. In this system a frame is a whole decision
and half of one is the failure mode, which is also why the light file is a
separate theme rather than a `.theme-light` branch inside the dark one.

## It is optional

Nothing depends on it in either direction.

* **The RotectorScan plugin looks right with no theme installed.** Its
  stylesheet declares the whole token block on its own `.rsb` root, precisely so
  that its surfaces are the design language whatever the client around them
  looks like.
* **The theme works with the plugin uninstalled.** It is an ordinary Discord
  theme and it styles the client, not the plugin.

What installing both gets you is the absence of a seam: the client, the
plugin's panels and the project's [web page](https://mireyacs.github.io/rotector-selfbot/)
are then built out of one set of values, and a scan modal stops looking like a
window cut into somebody else's application.

## Install

Equicord and Vencord both load themes the same way.

1. **Settings → Themes → Open Themes Folder.** (There is also a tray-menu entry
   for it on Equicord desktop.) That button is the reliable route, because the
   folder moves with your install:

   | Client   | Folder |
   | -------- | ------ |
   | Equicord | Linux `~/.config/Equicord/themes`, Windows `%APPDATA%\Equicord\themes`, macOS `~/Library/Application Support/Equicord/themes` |
   | Vencord  | the same paths with `Vencord` in place of `Equicord` |

2. Copy **one** of the two `.theme.css` files into that folder.
3. Back in **Settings → Themes**, tick it in the Local Themes list.

It applies immediately; there is no restart and no reload. Untick it to take it
off. The client's own Light/Dark setting stops mattering while a theme is on —
pick the theme file that matches the frame you want.

If you would rather not keep a file in sync by hand, the same folder accepts a
URL under **Online Themes**; point it at the raw file in this repository.

### The typeface

The design language is set in **Azeret Mono**. A theme file is a stylesheet, not
a bundle, and this one deliberately fetches nothing — a client whose entire
chrome waits on a webfont request is a worse client, and Discord's content
security policy may refuse the request anyway. The font stack is therefore
`"Azeret Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`, which
gives you your system's monospace face by default and picks up Azeret Mono for
free if you [install it locally](https://fonts.google.com/specimen/Azeret+Mono).
That is the only difference the font makes; nothing is laid out against it.

## What it changes

Almost all of it is a remap of Discord's own CSS custom properties rather than a
fight with Discord's selectors, so it follows the client through most redesigns.
Discord is mid-migration between two token families — the legacy
`--background-primary` / `--text-normal` names and the newer
`--background-base-lower` / `--text-default` ones — and both are declared,
because both are live in the client today.

* **Every surface is the ground.** The server rail, the channel list, the chat
  and the member list are one continuous wall separated by hairlines, not by
  steps in tone. `#101010` and `#161616` are the only raised states, and they are
  used for hover and selection so the client still answers the pointer.
* **No grey fills.** Discord's `--background-mod-subtle` family — the "slightly
  lighter panel" — is remapped onto those two near-ground steps instead of onto
  a grey.
* **Zero corner radius, everywhere**, including avatars, guild icons and
  buttons. Avatars and guild icons are circles cut by an SVG mask rather than by
  CSS, so the mask is dropped as well.
* **No box-shadow.** Depth in this world is density, not elevation. Popouts,
  menus and modals get a hairline instead, drawn as an inset outline so it costs
  no layout.
* **Blurple becomes ink.** Buttons, links, mentions, badges and the logo. Links
  say they are links with an underline rather than with a hue.
* **One family, ligatures off**, so ids, snowflakes and flag names render
  character-exact.

## What it deliberately does not change

Colour survives where it is evidence or identity, and this is the half of the
theme that is easiest to "finish" and hardest to get back:

* **Status dots keep their hue.** A person's presence is a fact about them. They
  do become squares — but their four shapes are redrawn as geometry (a filled
  square, a bitten corner for idle, a slotted square for do-not-disturb, a square
  ring for offline), because the hue must never be the only carrier for someone
  who cannot separate the four colours. The mobile, streaming and typing
  indicators keep Discord's own masks, since there the shape *is* the message.
* **Status, feedback and destructive-action colours are left at Discord's own
  values.** An error is a fact about the client's state, and a red Delete button
  is the affordance that stops somebody destroying a server they meant to leave.
* **Role colours are untouched.** Discord writes them as an inline style per
  user; nothing here can or should reach them.
* **User media is untouched** — avatars, embeds, attachments, custom emoji,
  stickers. No filters, no desaturation.
* **The plugin's five verdict hexes are untouched** and are identical in both
  frames. A verdict colour that followed your theme would stop being evidence and
  start being chrome.

## Known trade-offs

* **Two blocks reach for Discord class names**, and they are marked as such in
  both files: the seams between the client's columns, and the hairline that keeps
  a popout from reading as a hole punched in the page. Discord has no token for
  either, because in its own design those jobs are done by a tone step and a drop
  shadow, and this theme has removed both. When Discord renames a class those two
  blocks stop applying — the client stays entirely usable and simply reads as one
  continuous wall.
* **Removing the avatar mask removes the notch the status dot sits in**, so the
  dot now sits on top of the picture. It carries a ground-coloured hairline of
  its own to keep its edge.
* **Discord's raw neutral ramp (`--primary-100` … `--primary-900`) is remapped**,
  because a long tail of older components reaches straight past the semantic
  tokens into it. The mapping keeps the ramp's light-to-dark order so nothing
  inverts; the middle band (`460`–`560`) lands on the hairline tone, since
  Discord uses those steps as strokes and dividers far more often than as fills.
  Both files document the call where it is made.
* **Loading spinners and a few other shapes that were round are now square.**
  That is the radius scale doing what it says.

## Where the values come from

`DESIGN.md` in the repository root is the source of truth, `docs/index.html` is
where the language is actually built, and both theme files are copies of it —
the same relationship `rsb/tui/theme.py` has as the terminal app's copy. The rule
is the one `DESIGN.md` states for the TUI and it applies here unchanged:

> The page is the source and the theme is the copy — a change to the colours
> here is a change there, never the reverse.

The `--rsb-*` token block at the top of each file is the same block
`plugin/RotectorScan/style.css` declares on `.rsb`. Keep the three in step. The
light theme additionally carries a short block that flips the plugin's own tokens
with the frame, so a RotectorScan panel is not a black rectangle inside a white
client; it is a copy of the plugin's existing `.rsb--invert` decision and it says
so, so that it can be deleted the day the plugin grows a way to invert itself.

## A note on client mods

Installing a theme means modifying the Discord client, and Discord's terms do not
carve out an exception for that. The risk is the same one the plugin's own README
describes, it is real, and it is yours. A theme is the least invasive thing in
this repository — it adds no network traffic and reads nothing — but it is still
a modified client.
