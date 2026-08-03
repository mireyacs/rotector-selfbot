"""Draw the link thumbnail (og:image) for the project page.

    python tools/thumbnail.py

The page's hero is a seeded barcode canvas: one hairline bar for roughly every
fourteen members of a real 10,833-member scan, thinned through the middle where
the reading sits, with fourteen findings as the only marks that break it. This
draws the same field with the same algorithm and the same two seeds, so the
thumbnail is not a picture *like* the page -- it is the page's own wall, cropped
to 1200x630 and set in the same type.

That means the JavaScript in ``docs/index.html`` and the Python here have to
agree bit for bit. ``mulberry32`` below is a port of the generator there, and
``tests/test_thumbnail.py`` pulls the original out of the page, runs it under
node and compares the draws, so an edit to one and not the other is caught
rather than quietly leaving this drawing last month's wall.

Everything is drawn at 1200x630 and never downsampled: a 1px bar survives that
and does not survive a resize.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rsb.tui.theme import GROUND, INK  # noqa: E402

OUT = ROOT / "docs" / "og.png"
FONTS = Path(__file__).resolve().parent / "fonts"

#: the weights the page loads, vendored beside this script under the OFL
WEIGHTS = ("Light", "Regular", "Medium", "Bold", "ExtraBold")

#: the standard link-preview frame; Discord, Slack and X all crop toward it
W, H = 1200, 630

#: the two facts the page is built out of
MEMBERS = 10833
FINDINGS = 14

#: the page's seeds, and the reason the wall is the same wall every visit
SEED_BARS = 0x5EED
SEED_MARKS = 0xF1A6
SEED_IDS = 0xC0DE

#: the page's ornament tone -- texture with the shape of data, and the one grey
#: allowed to carry glyphs, because these are `aria-hidden` there and are not
#: content here either
TEXTURE = "#6f6f6f"

GUTTER = 72


# -- the page's generator, ported ------------------------------------------

def _imul(a: int, b: int) -> int:
    """``Math.imul``: a 32-bit multiply that keeps only the low word."""
    return (a * b) & 0xFFFFFFFF


def mulberry32(seed: int):
    """The generator from ``docs/index.html``, bit for bit.

    Kept unsigned throughout. JavaScript's ``|0`` makes the state signed and
    ``>>>`` reads it back unsigned; on an unsigned Python int both are just the
    same 32 bits, so only the masking has to be explicit.
    """
    state = seed & 0xFFFFFFFF

    def rand() -> float:
        nonlocal state
        state = (state + 0x6D2B79F5) & 0xFFFFFFFF
        t = state
        t = _imul(t ^ (t >> 15), 1 | t)
        t = ((t + _imul(t ^ (t >> 7), 61 | t)) & 0xFFFFFFFF) ^ t
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296

    return rand


def build_bars() -> list[tuple[float, int, float]]:
    """(x, width, alpha) per bar -- one bar per ~14 members."""
    rand = mulberry32(SEED_BARS)
    count = max(120, round(MEMBERS / 14))
    bars = []
    for i in range(count):
        x = i / count
        # dense at the edges, thinning through the middle where the type sits
        env = (abs(x - 0.5) * 2) ** 1.5
        # the page reads width before alpha; the generator is a stream, so the
        # order these two calls happen in is part of the picture
        width = 2 if rand() < 0.14 else 1
        alpha = (0.03 + rand() ** 2.4 * 0.26) * (0.16 + env * 0.84)
        bars.append((x, width, alpha))
    return bars


def build_marks() -> list[tuple[float, float]]:
    """(x, phase) for each finding, sorted across the wall."""
    rand = mulberry32(SEED_MARKS)
    marks = [(0.04 + rand() * 0.92, rand()) for _ in range(FINDINGS)]
    return sorted(marks, key=lambda m: m[0])


# -- the field --------------------------------------------------------------

def draw_field(image: Image.Image) -> None:
    draw = ImageDraw.Draw(image, "RGBA")
    top, bottom = H * 0.06, H * 0.94
    span = bottom - top

    for x, width, alpha in build_bars():
        px = round(x * W)
        draw.rectangle(
            [px, top, px + width - 1, bottom],
            fill=(255, 255, 255, round(alpha * 255)),
        )

    # the sine trace: one continuous curve across the wall
    mid, amp = H * 0.66, min(H * 0.17, 130)
    draw.line(
        [
            (sx, mid + math.sin((sx / W) * math.pi * 1.6 + 0.4) * amp)
            for sx in range(0, W + 1, 2)
        ],
        fill=(255, 255, 255, round(0.34 * 255)),
        width=1,
    )

    # findings: the only marks that break the field
    for x, phase in build_marks():
        px = round(x * W)
        alpha = 0.5 + 0.5 * (abs(x - 0.5) * 2) ** 1.2
        draw.rectangle(
            [px - 1, top, px, bottom], fill=(255, 255, 255, round(alpha * 255))
        )
        my = round(top + span * (0.18 + phase * 0.64))
        draw.rectangle([px - 4, my, px + 4, my + 8], fill=(255, 255, 255, 255))
        draw.rectangle(
            [px - 14, my + 4, px + 14, my + 4], fill=(255, 255, 255, round(0.28 * 255))
        )


# -- type -------------------------------------------------------------------

def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / f"AzeretMono-{weight}.ttf"), size)


class Setting:
    """One tracked line, remembered so it can be drawn twice.

    Once into a mask that becomes the halo, once in ink. The page keeps its
    hero rails legible over the live canvas with a stacked text-shadow rather
    than a panel behind them -- nothing sits on an opaque surface over the
    field -- and this is that halo.
    """

    def __init__(self, xy, text, fnt, tracking=0.0):
        self.x, self.y = xy
        self.text = text
        self.font = fnt
        self.tracking = tracking

    def width(self) -> float:
        if not self.text:
            return 0.0
        return sum(self.font.getlength(c) for c in self.text) + self.tracking * (
            len(self.text) - 1
        )

    def draw(self, draw: ImageDraw.ImageDraw, fill) -> None:
        x = self.x
        for char in self.text:
            draw.text((x, self.y), char, font=self.font, fill=fill)
            x += self.font.getlength(char) + self.tracking


def layout() -> list[Setting]:
    """The hero, as a poster: wordmark, statement, and the readout figures."""
    mark = font("Bold", 17)
    display = font("ExtraBold", 88)
    thin = font("Light", 34)
    label = font("Medium", 13)
    figure = font("Bold", 34)

    out = [
        # the wordmark, tracked to the page's .22em
        Setting((GUTTER, 46), "ROTECTOR-SELFBOT", mark, 17 * 0.22),
        # Display, tracked to -0.045em: Azeret is wide enough at this size that
        # the default advance reads as spaced-out rather than set
        Setting((GUTTER, 208), "Check everyone.", display, -88 * 0.045),
        Setting((GUTTER, 330), "Then read the number", thin, -34 * 0.02),
        Setting((GUTTER, 374), "that says who you missed.", thin, -34 * 0.02),
    ]

    # the readout: a label over a figure, set as objects rather than running
    # text, the way the hero's right rail sets them
    readout = [
        ("MEMBERS READ", f"{MEMBERS:,}"),
        ("FINDINGS", str(FINDINGS)),
        ("COVERAGE", "stated"),
    ]
    x = GUTTER
    for caption, value in readout:
        out.append(Setting((x, 486), caption, label, 13 * 0.19))
        out.append(Setting((x, 512), value, figure, -34 * 0.04))
        x += max(
            Setting((0, 0), caption, label, 13 * 0.19).width(),
            Setting((0, 0), value, figure, -34 * 0.04).width(),
        ) + 56
    return out


def ornament() -> list[Setting]:
    """The id rail: eighteen-digit rows, seeded, at the page's own 0xC0DE.

    Ornament, not information -- the page marks the same column `aria-hidden`
    and unselectable. It is here because the right of the frame is where the
    hero puts its rail, and texture is what belongs there.
    """
    rand = mulberry32(SEED_IDS)
    micro = font("Regular", 11)
    rows = ["".join(str(int(rand() * 10)) for _ in range(18)) for _ in range(34)]

    line_height = 11 * 1.7
    x = W - GUTTER - Setting((0, 0), rows[0], micro).width()
    return [
        Setting((x, round(28 + index * line_height)), row, micro)
        for index, row in enumerate(rows)
        if 28 + index * line_height < H - 34
    ]


def draw_type(image: Image.Image, settings: list[Setting]) -> None:
    # The halo first: the type's own shape, blurred, punched back into the
    # field as ground. The page keeps its rails legible over the live canvas
    # with a stacked text-shadow and no background, because nothing there is
    # allowed to sit on an opaque panel over the field -- so this stays tight
    # enough to read as a halo rather than as a plate.
    halo = Image.new("L", (W, H), 0)
    halo_draw = ImageDraw.Draw(halo)
    for setting in settings:
        setting.draw(halo_draw, 255)
    halo = halo.filter(ImageFilter.GaussianBlur(5))
    halo = halo.point(lambda v: min(255, int(v * 2.8)))
    image.paste(Image.new("RGB", (W, H), GROUND), (0, 0), halo)

    draw = ImageDraw.Draw(image)
    for setting in settings:
        setting.draw(draw, INK)


def draw_bar_rule(image: Image.Image, y: int, height: int = 12) -> None:
    """The page's divider: a 24px barcode period, repeated across the width."""
    draw = ImageDraw.Draw(image, "RGBA")
    ink = (255, 255, 255, round(0.55 * 255))
    for start in range(0, W, 24):
        for offset, width in ((0, 1), (3, 1), (9, 2), (14, 1), (21, 1)):
            x = start + offset
            if x < W:
                draw.rectangle([x, y, x + width - 1, y + height - 1], fill=ink)


def render() -> Image.Image:
    image = Image.new("RGB", (W, H), GROUND)
    draw_field(image)

    # the rail takes no halo: it is texture at a texture tone, and punching
    # ground out behind a whole column of it would put a plate on the field
    rail = ImageDraw.Draw(image)
    for setting in ornament():
        setting.draw(rail, TEXTURE)

    draw_type(image, layout())
    draw_bar_rule(image, H - 12)
    return image


def main() -> int:
    missing = [w for w in WEIGHTS if not (FONTS / f"AzeretMono-{w}.ttf").is_file()]
    if missing:
        print(f"missing font weights in {FONTS}: {', '.join(missing)}", file=sys.stderr)
        return 2

    OUT.parent.mkdir(parents=True, exist_ok=True)
    render().save(OUT, "PNG", optimize=True)
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
