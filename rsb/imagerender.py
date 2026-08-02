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

from .export import COLUMNS, ExportRow, valid_columns
from .verdict import ATTRIBUTION, Verdict, category_name, flag_name, verdict_label

try:  # optional
    from PIL import Image, ImageDraw, ImageFont

    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the availability test
    PIL_AVAILABLE = False

#: rows per image; a page that stays readable and openable
DEFAULT_ROWS_PER_IMAGE = 60

#: candidate monospace faces, in preference order
FONT_CANDIDATES = [
    "/usr/share/fonts/adwaita-mono-fonts/AdwaitaMono-Regular.ttf",
    "/usr/share/fonts/google-noto/NotoSansMono-Regular.ttf",
    "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/liberation-mono/LiberationMono-Regular.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "C:/Windows/Fonts/consola.ttf",
]
BOLD_CANDIDATES = [
    "/usr/share/fonts/adwaita-mono-fonts/AdwaitaMono-Bold.ttf",
    "/usr/share/fonts/google-noto/NotoSansMono-Bold.ttf",
    "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/liberation-mono/LiberationMono-Bold.ttf",
]


@dataclass(frozen=True)
class Theme:
    """Dark, to match the terminal it mirrors."""

    background: tuple = (18, 20, 24)
    panel: tuple = (28, 32, 38)
    stripe: tuple = (23, 26, 31)
    grid: tuple = (52, 58, 66)
    text: tuple = (222, 226, 230)
    muted: tuple = (138, 146, 156)
    heading: tuple = (255, 166, 43)


#: verdict -> row accent, mirroring the UI's styles
VERDICT_COLOURS: dict[Verdict, tuple] = {
    Verdict.THREAT: (244, 63, 94),
    Verdict.CAUTION: (250, 204, 21),
    Verdict.INFO: (56, 189, 248),
    Verdict.NO_DETECTIONS: (74, 222, 128),
    Verdict.UNKNOWN: (138, 146, 156),
}


def _load_font(size: int, bold: bool = False):
    for path in BOLD_CANDIDATES if bold else FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, ValueError):
            continue
    # last resort: Pillow's built-in bitmap face
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
            rows, directory, stem, profiles or {}, title=title, subtitle=subtitle
        )
        if style == "cards":
            return cards
        # "both" falls through to draw the table as well

    extra = cards if style == "both" else []
    columns = valid_columns(columns)
    font = _load_font(font_size)
    bold = _load_font(font_size, bold=True)
    theme = Theme()

    cell_w = max(1.0, font.getlength("0"))
    line_h = font_size + 8
    pad = 14

    # width each column needs, capped so one verbose field cannot dominate
    widths: list[int] = []
    for column in columns:
        longest = len(column)
        for row in rows:
            longest = max(longest, len(" ".join(COLUMNS[column](row).split())))
        widths.append(min(max(longest, len(column)), max_column_chars))

    table_chars = sum(widths) + 3 * (len(columns) - 1)
    image_w = int(pad * 2 + table_chars * cell_w) + 2
    header_h = pad + line_h * (3 if subtitle else 2)
    footer_h = line_h + pad

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
            accent = VERDICT_COLOURS.get(row.report.verdict, theme.text)
            # a bar in the margin, so severity reads before any text does
            draw.rectangle([0, y - 2, 3, y + line_h - 2], fill=accent)

            x = pad
            for column, width in zip(columns, widths):
                value = _fit(COLUMNS[column](row), width)
                colour = accent if column == "verdict" else theme.text
                draw.text((x, y), value, font=font, fill=colour)
                x += (width + 3) * cell_w
            y += line_h

        y += 6
        draw.line([(0, y), (image_w, y)], fill=theme.grid)
        y += 6
        draw.text(
            (pad, y),
            f"{len(rows):,} members  -  {ATTRIBUTION}  -  delete within 24h",
            font=font,
            fill=theme.muted,
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


def render_card(row: ExportRow, profile, theme: Theme | None = None):
    """One member as a Discord-profile-shaped card."""
    theme = theme or Theme()
    verdict = row.report.verdict
    accent = VERDICT_COLOURS.get(verdict, theme.muted)

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

    line_h = 19
    body_h = sum(24 + line_h * len(lines) for _title, lines in blocks)
    header_h = BANNER_H + AVATAR_D // 2 + 60
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
    y += 24

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
) -> list[Path]:
    """One card per member, each its own file."""
    if not PIL_AVAILABLE:
        raise RuntimeError(unavailable_reason())
    written: list[Path] = []
    width = len(str(max(1, len(rows))))
    for index, row in enumerate(rows, start=1):
        card = render_card(row, profiles.get(row.discord_id))
        safe = "".join(
            c if c.isalnum() or c in "-_" else "-" for c in row.username
        )[:24] or row.discord_id
        path = directory / f"{stem}.{index:0{width}d}-{safe}.png"
        card.save(path, "PNG", optimize=True)
        written.append(path)
    return written
