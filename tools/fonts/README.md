# Azeret Mono

The typeface `docs/index.html` loads from Google Fonts, vendored here so
`tools/thumbnail.py` can draw `docs/og.png` in the same face the page is set in.
Pillow needs a font file on disk; it cannot use the page's webfont.

These are the static instances Google Fonts serves for the five weights the page
requests (300 / 400 / 500 / 700 / 800), renamed to something legible:

| File                      | Weight | Used for                          |
| ------------------------- | ------ | --------------------------------- |
| `AzeretMono-Light.ttf`    | 300    | the subordinate clause            |
| `AzeretMono-Regular.ttf`  | 400    | the ornament id rail              |
| `AzeretMono-Medium.ttf`   | 500    | labels                            |
| `AzeretMono-Bold.ttf`     | 700    | the wordmark and the readout figures |
| `AzeretMono-ExtraBold.ttf`| 800    | the display line                  |

Upstream: <https://github.com/displaay/azeret> — Copyright 2021 The Azeret
Project Authors. Licensed under the SIL Open Font License 1.1, included as
`OFL.txt`. The OFL permits redistribution bundled like this provided the licence
travels with the fonts, which is what that file is for; do not delete it.

They are build-time assets only. Nothing in `rsb/` reads them, and the shipped
application does not depend on them.
