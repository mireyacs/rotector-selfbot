"""Render the example export images the README shows.

    python tools/exports.py

``docs/screenshots/export-png.png`` and ``export-card.png`` were previously
made by hand, which meant they could not be brought back into line when the
renderer changed -- and could not be re-shot in a new theme at all. This draws
both from the real renderer, so they are the same pictures the program makes.

They are rendered in the "ten-thousand" theme, the same one the rest of
``docs/`` is shot in, via ``[export] follow_theme``. The verdict accents stay
coloured on purpose: the chrome follows the theme, the findings do not, and
these images are the argument for exactly that.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rsb.export import DEFAULT_COLUMNS, ExportRow  # noqa: E402
from rsb.imagerender import available, render_card, render_png  # noqa: E402
from rsb.palette import from_theme  # noqa: E402
from rsb.profiles import Profile  # noqa: E402
from rsb.rotector import MemberReport, RobloxAccount, TrackedServer  # noqa: E402
from rsb.tui.theme import NAME as THEME_NAME, TEN_THOUSAND  # noqa: E402

OUT = ROOT / "docs" / "screenshots"
STAMP = "20260803T120000Z"

#: the same cast as tools/screenshots.py, so the docs tell one story
MEMBERS = [
    ("kaidenz", "Kaiden", 2, 5, "Condo Activity",
     "[Trap3] Entered a condo game via unguessable entry code 8 time(s)"),
    ("mxrcy_", "mxrcy", 2, 1, "CSAM",
     "[ListData] Account present in known distribution list (2024-present)"),
    ("v0idkid", "v0id", 1, 2, "Sexual Content",
     "[Profile] Explicit solicitation in profile description"),
    ("sh4dowplay", "shadow", 5, 3, "Kink Content",
     "[Groups] Member of 3 fetish roleplay groups"),
    ("tinybunnie", "bunnie", 2, 5, "Condo Activity",
     "[Trap3] Entered a condo game via unguessable entry code 3 time(s)"),
    ("jayjay0417", "JayJay", 4, None, "", ""),
    ("rblxtrader99", "Trader", 6, None, "", ""),
    ("noodlearms", "noodle", 3, None, "", ""),
]


def rows() -> list[ExportRow]:
    out = []
    for index, (username, nick, flag, category, reason, message) in enumerate(MEMBERS):
        report = MemberReport(discord_id=str(543787937737867264 + index))
        report.accounts.append(
            RobloxAccount(
                user_id=8_423_569_713 + index,
                username=f"{username}_rblx",
                flag_type=flag,
                category=category,
                confidence=0.95,
                sources=[2],
                reasons={reason: {"message": message}} if reason else {},
            )
        )
        if index % 3 == 0:
            report.servers.append(
                TrackedServer(f"s{index}", "condo hub", None, None, True, False)
            )
        out.append(
            ExportRow(
                discord_id=report.discord_id,
                username=username,
                display_name=nick,
                report=report,
            )
        )
    return out


def profile(row: ExportRow) -> Profile:
    """A filled-in profile, so the card shows every section it can."""
    return Profile(
        user_id=row.discord_id,
        username=row.username,
        global_name=row.display_name,
        accent_colour=(212, 175, 55),
        bio="GeT OuT oF mE SwOmP!",
        created_at="2019-02-10",
        pronouns="He/Him",
        badges=[
            ("hypesquad_brilliance", "HypeSquad Brilliance"),
            ("premium_guild", "Server boosting since 7/26/26"),
        ],
        connections=[
            ("steam", row.display_name, True),
            ("spotify", "GucciFlipFlops", True),
            ("epicgames", row.display_name, False),
        ],
        mutual_guilds=4,
        premium_since="2025-10-07",
        guild_joined_at="2026-04-16",
        timed_out_until="2026-06-25",
    )


def main() -> int:
    if not available():
        print("Pillow is not installed; these images need it.", file=sys.stderr)
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    palette = from_theme(
        TEN_THOUSAND.to_color_system().generate(),
        name=THEME_NAME,
        dark=TEN_THOUSAND.dark,
    )
    data = rows()

    # the table, drawn straight into docs/screenshots under a fixed name
    written = render_png(
        data,
        OUT,
        "export",
        list(DEFAULT_COLUMNS),
        title="Roblox Trading Hub (server)",
        subtitle=f"filtered  -  generated {STAMP}",
        style="table",
        palette=palette,
    )
    table = OUT / "export-png.png"
    written[0].replace(table)
    for leftover in written[1:]:
        leftover.unlink(missing_ok=True)
    print(f"  wrote {table.relative_to(ROOT)}")

    # and one card, for the member the README talks about
    subject = data[0]
    card = render_card(subject, profile(subject), palette=palette)
    path = OUT / "export-card.png"
    card.save(path, "PNG", optimize=True)
    print(f"  wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
