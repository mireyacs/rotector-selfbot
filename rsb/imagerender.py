"""Rendering the results table to PNG.

The CSV is for machines and the TXT is for reading; this is for showing --
pasting a finding into a ticket, a report, or a moderator channel where a
screenshot is what people actually look at.

Long lists are segmented the same way the CSV is, because a single image of
ten thousand rows is both unopenable and unreadable. Verdict colouring matches
the terminal UI, so a row means the same thing wherever you see it.

Pillow is an optional dependency: without it PNG export is simply offered as
unavailable rather than breaking the other formats.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .export import COLUMNS, ExportRow, RETENTION_HOURS, count_stale, valid_columns
from .palette import DEFAULT as DEFAULT_PALETTE, ExportPalette
from .rotector import is_stale, report_age, short_age, stale_note
from .verdict import ATTRIBUTION, Verdict, category_name, flag_name, verdict_label

try:  # optional
    from PIL import Image, ImageDraw, ImageFont

    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the availability test
    PIL_AVAILABLE = False

#: rows per image; a page that stays readable and openable
DEFAULT_ROWS_PER_IMAGE = 60

#: Candidate monospace faces, in preference order, covering all three
#: platforms. The table is drawn on a fixed character grid, so a proportional
#: fallback would put every column out of line -- worth carrying a long list
#: for. Bare filenames come last: Pillow searches the system font directories
#: for those, which is what finds a face on a distribution nobody listed.
FONT_CANDIDATES = [
    # Linux
    "/usr/share/fonts/adwaita-mono-fonts/AdwaitaMono-Regular.ttf",
    "/usr/share/fonts/google-noto/NotoSansMono-Regular.ttf",
    "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "/usr/share/fonts/liberation-mono/LiberationMono-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    # macOS
    "/System/Library/Fonts/SFNSMono.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/Library/Fonts/Courier New.ttf",
    # Windows
    "C:/Windows/Fonts/consola.ttf",
    "C:/Windows/Fonts/lucon.ttf",
    "C:/Windows/Fonts/cour.ttf",
    # whatever the platform can find by name
    "DejaVuSansMono.ttf",
    "LiberationMono-Regular.ttf",
    "Consolas",
    "Menlo",
]
BOLD_CANDIDATES = [
    # Linux
    "/usr/share/fonts/adwaita-mono-fonts/AdwaitaMono-Bold.ttf",
    "/usr/share/fonts/google-noto/NotoSansMono-Bold.ttf",
    "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/liberation-mono/LiberationMono-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
    # macOS -- Menlo's bold lives inside the same collection, at index 1
    "/System/Library/Fonts/SFNSMonoBold.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    # Windows
    "C:/Windows/Fonts/consolab.ttf",
    "C:/Windows/Fonts/courbd.ttf",
    # by name
    "DejaVuSansMono-Bold.ttf",
    "LiberationMono-Bold.ttf",
    "Consolas Bold",
]


@dataclass(frozen=True)
class Theme:
    """RGB tuples for Pillow, taken from an :class:`ExportPalette`.

    Kept as its own type because everything below draws with tuples and a
    per-pixel hex parse would be wasteful. ``Theme()`` with no arguments is the
    palette's default, which is the fixed dark look exports have always had.
    """

    background: tuple = (18, 20, 24)
    panel: tuple = (28, 32, 38)
    stripe: tuple = (23, 26, 31)
    grid: tuple = (52, 58, 66)
    text: tuple = (222, 226, 230)
    muted: tuple = (138, 146, 156)
    heading: tuple = (255, 166, 43)

    @classmethod
    def from_palette(cls, palette: ExportPalette | None) -> "Theme":
        if palette is None:
            return cls()
        return cls(
            background=palette.background_rgb,
            panel=palette.panel_rgb,
            stripe=palette.stripe_rgb,
            grid=palette.grid_rgb,
            text=palette.text_rgb,
            muted=palette.muted_rgb,
            heading=palette.heading_rgb,
        )


def verdict_colours(palette: ExportPalette | None = None) -> dict[Verdict, tuple]:
    """Verdict -> row accent, mirroring the UI's styles.

    These do not follow the theme even when the chrome does: the accent is the
    finding, and a monochrome theme would drain the one distinction the reader
    is looking for.
    """
    source = palette or DEFAULT_PALETTE
    return {verdict: source.verdict_rgb(verdict) for verdict in Verdict}


#: the default accents, for callers that draw without a palette
VERDICT_COLOURS: dict[Verdict, tuple] = verdict_colours()


def _load_font(size: int, bold: bool = False):
    """The best monospace face this machine has, at ``size``.

    Falls back through the candidate list, then to whatever Pillow bundles.
    ``load_default(size)`` matters more than it looks: without a size argument
    Pillow hands back a fixed ~11px bitmap face, so an export on a machine with
    no listed font came out with headings the same size as body text and
    columns that no longer lined up. With one, the fallback is at least drawn
    at the size the layout was measured for.
    """
    candidates = BOLD_CANDIDATES if bold else FONT_CANDIDATES
    for path in candidates:
        try:
            font = ImageFont.truetype(path, size)
        except (OSError, ValueError):
            continue
        # a .ttc collection holds several faces; Menlo keeps bold at index 1
        if bold and str(path).endswith(".ttc"):
            try:
                return ImageFont.truetype(path, size, index=1)
            except (OSError, ValueError):
                pass
        return font

    # Pillow's own, sized where the version allows it
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 takes no size
        return ImageFont.load_default()


def available() -> bool:
    return PIL_AVAILABLE


def unavailable_reason() -> str:
    return (
        "PNG export needs Pillow. Install it with:  pip install pillow"
        if not PIL_AVAILABLE
        else ""
    )


def _fit(text: str, width: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= width:
        return text
    return text[: max(1, width - 1)] + "\u2026"


def render_png(
    rows: Sequence[ExportRow],
    directory: Path,
    stem: str,
    columns: Sequence[str],
    *,
    title: str = "",
    subtitle: str = "",
    rows_per_image: int = DEFAULT_ROWS_PER_IMAGE,
    font_size: int = 15,
    max_column_chars: int = 44,
    style: str = "table",
    profiles: dict | None = None,
    palette: ExportPalette | None = None,
    retain_beyond_terms: bool = False,
    retention_hours: int = RETENTION_HOURS,
) -> list[Path]:
    """Draw ``rows`` as images.

    ``style`` is ``"table"`` for the results grid, ``"cards"`` for one
    profile-shaped card per member -- avatar, banner and the findings laid out
    the way Discord lays out a profile -- or ``"both"``, which is often what is
    wanted: the table to see the shape of it, and a card to attach to whatever
    comes next.
    """
    if not PIL_AVAILABLE:
        raise RuntimeError(unavailable_reason())
    if style in ("cards", "both"):
        cards = render_cards(
            rows, directory, stem, profiles or {},
            title=title, subtitle=subtitle, palette=palette,
        )
        if style == "cards":
            return cards
        # "both" falls through to draw the table as well

    extra = cards if style == "both" else []
    columns = valid_columns(columns)

    # A screenshot travels further than the folder it came out of, so the strip
    # under the table has to say what is true of this image rather than repeat
    # the 24-hour rule over data the run was told to keep longer.
    retention_strip = (
        f"kept {retention_hours}h by choice; Rotector's terms allow "
        f"{RETENTION_HOURS}h"
        if retain_beyond_terms
        else f"delete within {RETENTION_HOURS}h"
    )
    stale_rows = count_stale(rows)
    stale_strip = (
        f"{stale_rows} finding(s) here are older than {RETENTION_HOURS}h and "
        "describe the day they were answered - re-check before acting"
        if stale_rows
        else ""
    )
    # The Stale column is on by default and can be deselected; when it is, the
    # age rides with the verdict rather than disappearing off the image.
    mark_verdict = "stale" not in columns

    def cell_value(row: ExportRow, column: str) -> str:
        """What actually gets drawn in one cell, measured and rendered alike."""
        value = COLUMNS[column](row)
        if column == "verdict" and mark_verdict and is_stale(row.report):
            value += f"  stale {short_age(report_age(row.report))}"
        return value

    font = _load_font(font_size)
    bold = _load_font(font_size, bold=True)
    theme = Theme.from_palette(palette)
    accents = verdict_colours(palette)

    cell_w = max(1.0, font.getlength("0"))
    line_h = font_size + 8
    pad = 14

    # width each column needs, capped so one verbose field cannot dominate
    widths: list[int] = []
    for column in columns:
        longest = len(column)
        for row in rows:
            longest = max(longest, len(" ".join(cell_value(row, column).split())))
        widths.append(min(max(longest, len(column)), max_column_chars))

    table_chars = sum(widths) + 3 * (len(columns) - 1)
    image_w = int(pad * 2 + table_chars * cell_w) + 2
    header_h = pad + line_h * (3 if subtitle else 2)
    footer_h = line_h * (2 if stale_strip else 1) + pad

    chunks = (
        [list(rows)]
        if not rows_per_image or len(rows) <= rows_per_image
        else [
            rows[i : i + rows_per_image]
            for i in range(0, len(rows), rows_per_image)
        ]
    )

    written: list[Path] = []
    width_digits = len(str(len(chunks)))

    for index, chunk in enumerate(chunks, start=1):
        image_h = header_h + line_h * (len(chunk) + 2) + footer_h
        image = Image.new("RGB", (image_w, int(image_h)), theme.background)
        draw = ImageDraw.Draw(image)

        y = pad
        if title:
            label = title
            if len(chunks) > 1:
                label += f"   (part {index} of {len(chunks)})"
            draw.text((pad, y), label, font=bold, fill=theme.heading)
            y += line_h
        if subtitle:
            draw.text((pad, y), subtitle, font=font, fill=theme.muted)
            y += line_h
        y += 4

        # header row
        draw.rectangle(
            [0, y - 3, image_w, y + line_h - 3], fill=theme.panel
        )
        x = pad
        for column, width in zip(columns, widths):
            draw.text(
                (x, y), column.replace("_", " ").upper()[:width], font=bold,
                fill=theme.text,
            )
            x += (width + 3) * cell_w
        y += line_h
        draw.line([(0, y - 2), (image_w, y - 2)], fill=theme.grid)

        for row_index, row in enumerate(chunk):
            if row_index % 2:
                draw.rectangle([0, y - 2, image_w, y + line_h - 2], fill=theme.stripe)
            accent = accents.get(row.report.verdict, theme.text)
            # a bar in the margin, so severity reads before any text does
            draw.rectangle([0, y - 2, 3, y + line_h - 2], fill=accent)

            x = pad
            for column, width in zip(columns, widths):
                value = _fit(cell_value(row, column), width)
                colour = accent if column == "verdict" else theme.text
                draw.text((x, y), value, font=font, fill=colour)
                x += (width + 3) * cell_w
            y += line_h

        y += 6
        draw.line([(0, y), (image_w, y)], fill=theme.grid)
        y += 6
        draw.text(
            (pad, y),
            f"{len(rows):,} members  -  {ATTRIBUTION}  -  {retention_strip}",
            font=font,
            fill=theme.muted,
        )
        if stale_strip:
            y += line_h
            draw.text(
                (pad, y), stale_strip, font=font,
                fill=verdict_colours(palette).get(Verdict.CAUTION, theme.text),
            )

        name = (
            f"{stem}.png"
            if len(chunks) == 1
            else f"{stem}.part{index:0{width_digits}d}-of-{len(chunks)}.png"
        )
        path = directory / name
        image.save(path, "PNG", optimize=True)
        written.append(path)

    return written


# --------------------------------------------------------------------------
# cards
# --------------------------------------------------------------------------

#: card geometry, shaped like a Discord profile popout
CARD_W = 640
BANNER_H = 120
AVATAR_D = 96
CARD_PAD = 18


def _sans(size: int, bold: bool = False):
    """A proportional face for card text; the table uses monospace."""
    candidates = (
        [
            "/usr/share/fonts/google-noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/adwaita-fonts/AdwaitaSans-Bold.ttf",
            "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/google-noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/adwaita-fonts/AdwaitaSans-Regular.ttf",
            "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, ValueError):
            continue
    return _load_font(size, bold)


def _wrap(text: str, font, width: int, max_lines: int = 3) -> list[str]:
    words = " ".join(str(text).split()).split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if font.getlength(candidate) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
            if len(lines) == max_lines:
                break
    if current and len(lines) < max_lines:
        lines.append(current)
    if lines and len(words) > sum(len(l.split()) for l in lines):
        lines[-1] = lines[-1].rstrip() + "…"
    return lines


def _circle(image, diameter: int):
    """Crop to a circle, the way Discord shows an avatar."""
    source = image.convert("RGBA").resize((diameter, diameter), Image.LANCZOS)
    mask = Image.new("L", (diameter * 4, diameter * 4), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, diameter * 4 - 1, diameter * 4 - 1], fill=255)
    source.putalpha(mask.resize((diameter, diameter), Image.LANCZOS))
    return source


def _initials(name: str) -> str:
    parts = [p for p in "".join(c if c.isalnum() else " " for c in name).split() if p]
    if not parts:
        return "?"
    return (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")).upper()


def render_card(
    row: ExportRow,
    profile,
    theme: Theme | None = None,
    palette: ExportPalette | None = None,
):
    """One member as a Discord-profile-shaped card."""
    theme = theme or Theme.from_palette(palette)
    verdict = row.report.verdict
    accent = verdict_colours(palette).get(verdict, theme.muted)

    name_font = _sans(22, bold=True)
    tag_font = _sans(14)
    label_font = _sans(12, bold=True)
    body_font = _sans(14)
    small_font = _sans(12)

    # measure before drawing, so the card is exactly as tall as its content
    blocks: list[tuple[str, list[str]]] = []
    if profile is not None and getattr(profile, "bio", ""):
        blocks.append(("ABOUT ME", _wrap(profile.bio, body_font, CARD_W - CARD_PAD * 2, 3)))

    if profile is not None:
        # account context that says something about who this is: how old the
        # account is, what is linked to it, how long they have been here
        facts: list[str] = []
        if profile.connections:
            facts.append(
                "Linked: "
                + ", ".join(
                    f"{platform} ({name})" + ("" if verified else " unverified")
                    for platform, name, verified in profile.connections[:4]
                )
            )
        if profile.badges:
            facts.append(
                "Badges: " + ", ".join(label for _id, label in profile.badges[:4])
            )
        tail = []
        if profile.guild_joined_at:
            tail.append(f"joined this server {profile.guild_joined_at}")
        if profile.mutual_guilds:
            tail.append(f"{profile.mutual_guilds} mutual server(s)")
        if profile.has_nitro:
            tail.append(f"Nitro since {profile.premium_since}")
        if tail:
            facts.append(" - ".join(tail))
        if profile.timed_out_until:
            facts.append(f"Currently timed out until {profile.timed_out_until}")
        if facts:
            wrapped: list[str] = []
            for fact in facts:
                wrapped += _wrap(fact, body_font, CARD_W - CARD_PAD * 2, 2)
            blocks.append(("ACCOUNT", wrapped))

    accounts = sorted(row.report.accounts, key=lambda a: -int(a.verdict))
    if accounts:
        lines = []
        for account in accounts[:4]:
            lines.append(
                f"{flag_name(account.flag_type)}  -  {account.username} "
                f"({account.user_id})"
                + (f"  -  {category_name(account.category)}" if account.category else "")
            )
        blocks.append(("LINKED ROBLOX ACCOUNTS", lines))

    reasons: list[str] = []
    for account in accounts:
        for name, detail in (account.reasons or {}).items():
            message = " ".join(str(detail.get("message", "")).split())
            reasons += _wrap(f"{name}: {message}", body_font, CARD_W - CARD_PAD * 2, 2)
    if reasons:
        blocks.append(("WHY", reasons[:6]))

    if row.report.servers:
        blocks.append((
            "TRACKED SERVERS",
            _wrap(
                ", ".join(s.server_name for s in row.report.servers),
                body_font, CARD_W - CARD_PAD * 2, 2,
            ),
        ))

    # A card is the piece that gets pasted into a ticket on its own, so if the
    # finding is older than the window the terms allow, the card has to carry
    # that with it rather than leave it behind in the folder's README.
    stale_lines = (
        _wrap(stale_note(row.report), small_font, CARD_W - CARD_PAD * 2, 2)
        if is_stale(row.report)
        else []
    )

    line_h = 19
    body_h = sum(24 + line_h * len(lines) for _title, lines in blocks)
    header_h = BANNER_H + AVATAR_D // 2 + 60 + 17 * len(stale_lines)
    # room for the footer strip plus breathing space, so the last block
    # does not run into it
    height = header_h + body_h + 60

    card = Image.new("RGB", (CARD_W, height), theme.panel)
    draw = ImageDraw.Draw(card)

    # --- banner
    banner_colour = (
        getattr(profile, "accent_colour", None)
        or (profile.fallback_colour if profile is not None else accent)
    )
    draw.rectangle([0, 0, CARD_W, BANNER_H], fill=banner_colour)
    banner_bytes = getattr(profile, "banner_bytes", None)
    if banner_bytes:
        try:
            import io

            banner = Image.open(io.BytesIO(banner_bytes)).convert("RGB")
            ratio = max(CARD_W / banner.width, BANNER_H / banner.height)
            banner = banner.resize(
                (int(banner.width * ratio) + 1, int(banner.height * ratio) + 1),
                Image.LANCZOS,
            )
            card.paste(banner.crop((0, 0, CARD_W, BANNER_H)), (0, 0))
        except Exception:
            pass

    # --- verdict ribbon, so severity is unmissable
    ribbon = f"  {verdict_label(verdict)}  "
    ribbon_w = int(label_font.getlength(ribbon)) + 10
    draw.rectangle([CARD_W - ribbon_w - 12, 12, CARD_W - 12, 34], fill=accent)
    draw.text((CARD_W - ribbon_w - 7, 17), ribbon.strip(), font=label_font,
              fill=(12, 14, 17))

    # --- avatar, overlapping the banner as Discord does
    ax, ay = CARD_PAD, BANNER_H - AVATAR_D // 2
    draw.ellipse(
        [ax - 6, ay - 6, ax + AVATAR_D + 6, ay + AVATAR_D + 6], fill=theme.panel
    )
    avatar_bytes = getattr(profile, "avatar_bytes", None)
    placed = False
    if avatar_bytes:
        try:
            import io

            avatar = Image.open(io.BytesIO(avatar_bytes))
            card.paste(_circle(avatar, AVATAR_D), (ax, ay), _circle(avatar, AVATAR_D))
            placed = True
        except Exception:
            placed = False
    if not placed:
        fallback = profile.fallback_colour if profile is not None else accent
        draw.ellipse([ax, ay, ax + AVATAR_D, ay + AVATAR_D], fill=fallback)
        initials = _initials(row.display_name or row.username)
        big = _sans(34, bold=True)
        w = big.getlength(initials)
        draw.text(
            (ax + (AVATAR_D - w) / 2, ay + AVATAR_D / 2 - 24), initials,
            font=big, fill=(255, 255, 255),
        )

    # --- identity
    y = ay + AVATAR_D + 10
    draw.text((CARD_PAD, y), row.display_name, font=name_font, fill=theme.text)
    y += 28
    handle = f"@{row.username}"
    if profile is not None and profile.pronouns:
        handle += f"   ({profile.pronouns})"
    if profile is not None and profile.created_at:
        handle += f"   -   joined Discord {profile.created_at}"
    draw.text((CARD_PAD, y), handle, font=tag_font, fill=theme.muted)
    y += 20
    draw.text((CARD_PAD, y), f"ID {row.discord_id}", font=small_font, fill=theme.muted)
    y += 20 if stale_lines else 24
    for line in stale_lines:
        draw.text(
            (CARD_PAD, y), line, font=small_font,
            fill=verdict_colours(palette).get(Verdict.CAUTION, theme.text),
        )
        y += 17
    if stale_lines:
        y += 4

    # --- findings
    for label, lines in blocks:
        draw.line([(CARD_PAD, y), (CARD_W - CARD_PAD, y)], fill=theme.grid)
        y += 8
        draw.text((CARD_PAD, y), label, font=label_font, fill=theme.muted)
        y += 16
        for line in lines:
            draw.text((CARD_PAD, y), line, font=body_font, fill=theme.text)
            y += line_h

    footer = ATTRIBUTION
    if profile is not None and profile.errors:
        footer = f"{'; '.join(profile.errors)}  -  {footer}"
    draw.rectangle([0, height - 26, CARD_W, height], fill=theme.background)
    draw.text((CARD_PAD, height - 20), _fit(footer, 92), font=small_font,
              fill=theme.muted)
    return card


def render_cards(
    rows: Sequence[ExportRow],
    directory: Path,
    stem: str,
    profiles: dict,
    *,
    title: str = "",
    subtitle: str = "",
    palette: ExportPalette | None = None,
) -> list[Path]:
    """One card per member, each its own file."""
    if not PIL_AVAILABLE:
        raise RuntimeError(unavailable_reason())
    written: list[Path] = []
    width = len(str(max(1, len(rows))))
    for index, row in enumerate(rows, start=1):
        card = render_card(
            row,
            profiles.get(row.discord_id),
            theme=Theme.from_palette(palette),
            palette=palette,
        )
        safe = "".join(
            c if c.isalnum() or c in "-_" else "-" for c in row.username
        )[:24] or row.discord_id
        path = directory / f"{stem}.{index:0{width}d}-{safe}.png"
        card.save(path, "PNG", optimize=True)
        written.append(path)
    return written
