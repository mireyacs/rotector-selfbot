"""Draw the site's favicon from its own bar-field motif.

    python tools/favicon.py

The page's divider is a barcode: a 24px period with bars at 0-1, 3-4, 9-11,
14-15 and 21-22. Scaled to a sixteen-unit grid that rhythm survives almost
intact, which is the whole reason the mark works small -- it is not a picture
of the page reduced until it disappears, it is one period of the page's own
rule, and it stays legible at the 16px a browser tab actually renders.

One bar is heavier than the others. That is the finding: the same thing the
hero canvas does when fourteen marks break a field of ten thousand hairlines.

Writes three files, because one format does not cover a browser tab:

* ``favicon.svg``  -- what modern browsers prefer, and the only one that stays
  crisp on a high-density display or in a dark/light UI at any size.
* ``favicon.ico``  -- 16/32/48, for everything that ignores the SVG.
* ``apple-touch-icon.png`` -- 180x180, which iOS uses for a home-screen tile.

Bars are placed on integer pixels at every size, so they never land on a half
pixel and blur into grey -- the one failure that would turn a two-value mark
into a smudge.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rsb.tui.theme import GROUND, INK  # noqa: E402

OUT = ROOT / "docs"

#: the mark, on a 16-unit grid: (x, width, is_the_finding)
#: derived from the page's own 24px period, scaled by 2/3
BARS = [
    (0, 1, False),
    (2, 1, False),
    (5, 2, True),
    (9, 1, False),
    (11, 1, False),
    (14, 1, False),
]
GRID = 16

#: .ico sizes a browser or a pinned shortcut will ask for
ICO_SIZES = (16, 32, 48)
TOUCH_SIZE = 180


def svg() -> str:
    """The scalable mark. viewBox is the grid, so it never needs rounding."""
    rects = "\n".join(
        f'  <rect x="{x}" y="0" width="{w}" height="{GRID}" fill="{INK}"'
        f'{"" if finding else ' opacity=".62"'}/>'
        for x, w, finding in BARS
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {GRID} {GRID}">\n'
        f"  <title>rotector-selfbot</title>\n"
        f'  <rect width="{GRID}" height="{GRID}" fill="{GROUND}"/>\n'
        f"{rects}\n"
        f"</svg>\n"
    )


def raster(size: int):
    """The mark at ``size``, with every bar snapped to a whole pixel."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (size, size), GROUND)
    draw = ImageDraw.Draw(image, "RGBA")
    scale = size / GRID
    for x, width, finding in BARS:
        left = round(x * scale)
        right = max(left, round((x + width) * scale) - 1)
        draw.rectangle(
            [left, 0, right, size - 1],
            fill=(255, 255, 255, 255 if finding else round(0.62 * 255)),
        )
    return image


def main() -> int:
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        print("Pillow is needed for the .ico and .png; install it first.",
              file=sys.stderr)
        return 2

    OUT.mkdir(parents=True, exist_ok=True)

    path = OUT / "favicon.svg"
    path.write_text(svg(), encoding="utf-8")
    print(f"  wrote {path.relative_to(ROOT)}")

    # Pillow writes every requested size into the one .ico
    icon = raster(max(ICO_SIZES))
    path = OUT / "favicon.ico"
    icon.save(path, "ICO", sizes=[(s, s) for s in ICO_SIZES])
    print(f"  wrote {path.relative_to(ROOT)} ({', '.join(str(s) for s in ICO_SIZES)})")

    path = OUT / "apple-touch-icon.png"
    raster(TOUCH_SIZE).save(path, "PNG", optimize=True)
    print(f"  wrote {path.relative_to(ROOT)} ({TOUCH_SIZE}x{TOUCH_SIZE})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
