"""Bring an older ``config.toml`` up to date with newly added settings.

Settings get added between versions, and a config written against an earlier
one silently falls back to defaults for anything it does not mention.  This
walks a declared schema, appends whatever is missing -- with its explanatory
comment -- and leaves every existing value, comment and blank line exactly
where it was.  Nothing is ever overwritten or reordered.

    python -m rsb migrate            # show what is missing, then apply
    python -m rsb migrate --dry-run  # show only
"""

from __future__ import annotations

import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .config import _toml_value


@dataclass
class Setting:
    name: str
    default: object
    comment: str = ""


@dataclass
class Section:
    name: str
    comment: str = ""
    settings: list[Setting] = field(default_factory=list)


#: every section and key a current config understands, with its default
SCHEMA: list[Section] = [
    Section(
        "discord",
        "Your Discord credentials. DISCORD_TOKEN / DISCORD_BOT_TOKEN "
        "override these.",
        [
            Setting("token", "", "User token. Treat this like a password."),
            Setting(
                "bot_token",
                "",
                (
                    "Optional bot application token, used only when no user\n"
                    "token is set. A bot reaches servers it was invited to and\n"
                    "nothing else -- no friends, no DMs -- but can list every\n"
                    "member of them, which a user account cannot. Needs the\n"
                    "SERVER MEMBERS INTENT enabled in the Developer Portal."
                ),
            ),
        ],
    ),
    Section(
        "rotector",
        "Rotector API access. ROTECTOR_API_KEY overrides api_key.",
        [
            Setting("api_key", "", "Optional. Raises your limit on one connection."),
            Setting("rate_limit", 50, "Documented standard tier: 50 per window."),
            Setting("window", 10.0, "Rate limit window, in seconds."),
            Setting("reserve", 5, "Request units held back as safety headroom."),
            Setting("cache_ttl", 3600, "In-memory cache lifetime, clamped under 24h."),
            Setting("concurrency", 3, "Concurrent in-flight requests."),
        ],
    ),
    Section(
        "scan",
        "What a scan collects and what the results table lists.",
        [
            Setting("skip_bots", True, "Bots have no Roblox connections."),
            Setting("max_members", 0, "Cap members per source. 0 = no cap."),
            Setting(
                "hide_no_detections",
                True,
                "Hide members Rotector has not flagged.",
            ),
            Setting(
                "hide_unknown",
                True,
                "Hide members with no Rotector-known Roblox account.",
            ),
            Setting(
                "on_rescan",
                "ask",
                "What a new scan does with results already on screen:\n"
                '"ask", "replace", "merge_skip" or "merge_recheck".\n'
                "Merging accumulates coverage across runs, because an\n"
                "unprivileged scan only ever sees who was online.",
            ),
        ],
    ),
    Section(
        "proxy",
        (
            "Routing Rotector lookups over proxies.\n"
            "Rotector's Terms of Use prohibit circumventing rate limits by\n"
            "rotating IPs. An API key is the sanctioned way to go faster."
        ),
        [
            Setting("enabled", False, "Off by default. Enabling it is your call."),
            Setting("file", "proxies.txt", "Newline-delimited proxy list."),
            Setting("list", [], "Extra proxies inline."),
            Setting(
                "direct_as_fallback",
                False,
                "false = your connection is a co-equal route and carries work.",
            ),
            Setting("max_failures", 3, "Failures before a route is marked unhealthy."),
            Setting("timeout", 20.0, "Per-request timeout for proxied calls."),
            Setting("probe_concurrency", 10, "Concurrent probes while testing."),
        ],
    ),
    Section(
        "export",
        "Defaults for the export dialog. Saved here when you tick 'Remember'.",
        [
            Setting(
                "formats",
                ["csv", "txt"],
                'Any of "csv", "txt", "json", "png", "html".\n'
                '"png" draws the table as an image and needs Pillow;\n'
                '"html" writes one searchable page with both views.',
            ),
            Setting(
                "scope",
                "filtered",
                '"filtered" honours the results filter; "all" exports everything.',
            ),
            Setting("segment_size", 1000, "Rows per CSV segment. 0 = one file."),
            Setting("columns", [], "Columns to write. Empty = the default set."),
            Setting("directory", "exports", "Parent folder for exports."),
            Setting(
                "png_style",
                "table",
                '"table" draws the results grid, "cards" draws a\n'
                "Discord-style profile per member with avatar and banner,\n"
                '"both" writes the table and every card.',
            ),
            Setting(
                "preserve",
                False,
                (
                    "Keep exports past the 24h retention window.\n"
                    "Off means old export folders are cleared automatically --\n"
                    "Rotector's terms forbid retaining their data that long."
                ),
            ),
        ],
    ),
    Section(
        "purge",
        (
            "Deleting your own messages from a conversation with a flagged\n"
            "account. Only ever your own -- Discord gives no way to remove\n"
            "someone else's messages from a DM."
        ),
        [
            Setting(
                "delete_delay",
                1.0,
                "Seconds between deletions. Discord's delete limit is strict;\n"
                "lowering this mostly buys you 429s and a slower purge.",
            ),
            Setting("max_messages", 0, "Cap on messages collected. 0 = no cap."),
            Setting("max_age_days", 0, "Only history this recent. 0 = all of it."),
        ],
    ),
    Section(
        "moderation",
        "Acting on members from the results table.",
        [
            Setting(
                "require_threat",
                True,
                (
                    "Only Flagged/Confirmed may be kicked or banned.\n"
                    "Does not apply to unfriending, blocking or leaving a group."
                ),
            ),
            Setting(
                "default_reason",
                "Flagged by Rotector: {reason}",
                "{reason} is the finding. The appeal link is always appended.",
            ),
            Setting("delete_message_seconds", 0, "Messages to purge on ban (0-604800)."),
            Setting(
                "silent_leave",
                False,
                'Leave group DMs without posting "left the group".',
            ),
            Setting(
                "allow_caution",
                True,
                (
                    "Allow acting on CAUTION findings.\n"
                    "Never routine: Rotector asks that Provisional/Mixed be\n"
                    "reviewed by a person, so a second separate confirmation\n"
                    "is always required."
                ),
            ),
            Setting(
                "notify_before_action",
                True,
                (
                    "BOT TOKENS ONLY. Try to DM someone before kicking or\n"
                    "banning them, so they know why and can appeal.\n"
                    "Sent first because a banned account shares no server\n"
                    "with you and can no longer be reached. A closed DM is\n"
                    "not a failure -- the action still proceeds.\n"
                    "Ignored for user tokens: messaging strangers in bulk is\n"
                    "what gets a user account flagged for spam."
                ),
            ),
            Setting(
                "notify_message",
                "",
                (
                    "The notice text. Blank uses the built-in wording.\n"
                    "{action}, {place} and {reason} are filled in; the\n"
                    "appeal link is appended if you leave it out."
                ),
            ),
            Setting(
                "bulk_delay",
                1.0,
                (
                    "Seconds between members in a bulk action.\n"
                    "Acting fast on many people is what trips Discord's\n"
                    "abuse heuristics on a user account."
                ),
            ),
        ],
    ),
]


@dataclass
class MigrationReport:
    path: Path
    added_sections: list[str] = field(default_factory=list)
    added_keys: list[str] = field(default_factory=list)
    backup: Path | None = None
    created: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.added_sections or self.added_keys or self.created)

    def describe(self) -> str:
        if self.created:
            return f"Created {self.path} with every current setting."
        if not self.changed:
            return f"{self.path} is already up to date."
        bits = []
        if self.added_sections:
            bits.append(f"{len(self.added_sections)} new section(s): "
                        + ", ".join(self.added_sections))
        if self.added_keys:
            bits.append(f"{len(self.added_keys)} new setting(s): "
                        + ", ".join(self.added_keys))
        return "; ".join(bits)


def _comment_lines(text: str) -> list[str]:
    return [f"# {line}" if line else "#" for line in text.split("\n")]


def _render_setting(setting: Setting) -> list[str]:
    lines = _comment_lines(setting.comment) if setting.comment else []
    lines.append(f"{setting.name} = {_toml_value(setting.default)}")
    return lines


def _render_section(section: Section) -> list[str]:
    lines: list[str] = []
    if section.comment:
        lines += _comment_lines(section.comment)
    lines.append(f"[{section.name}]")
    for index, setting in enumerate(section.settings):
        if index:
            lines.append("")
        lines += _render_setting(setting)
    return lines


def _section_bounds(lines: list[str], name: str) -> tuple[int, int] | None:
    """(header index, index just past the section's last non-blank line)."""
    start = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if start is None:
            if stripped == f"[{name}]":
                start = index
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            end = index
            while end > start + 1 and not lines[end - 1].strip():
                end -= 1
            return start, end
    if start is None:
        return None
    end = len(lines)
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return start, end


def missing_settings(path: Path) -> tuple[list[str], list[str]]:
    """What a config lacks: (whole sections, individual ``section.key`` names)."""
    if not path.is_file():
        return [s.name for s in SCHEMA], []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return [], []

    sections, keys = [], []
    for section in SCHEMA:
        if section.name not in data:
            sections.append(section.name)
            continue
        present = data[section.name] or {}
        for setting in section.settings:
            if setting.name not in present:
                keys.append(f"{section.name}.{setting.name}")
    return sections, keys


def migrate_config(path: Path, dry_run: bool = False) -> MigrationReport:
    """Add every missing section and key, preserving what is already there."""
    path = Path(path)
    report = MigrationReport(path=path)

    if not path.is_file():
        report.created = True
        report.added_sections = [s.name for s in SCHEMA]
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            blocks = [_render_section(section) for section in SCHEMA]
            body = "\n\n".join("\n".join(block) for block in blocks)
            path.write_text(body + "\n", encoding="utf-8")
        return report

    original = path.read_text(encoding="utf-8")
    data = tomllib.loads(original)
    lines = original.splitlines()

    # Two passes, and the order matters. Inserting keys into existing sections
    # first -- back to front, so earlier insertions do not shift later indices
    # -- then appending whole new sections. Interleaving the two lets a new
    # section's leading comment land above another section's inserted keys.
    for section in reversed(SCHEMA):
        if section.name not in data:
            continue
        present = data[section.name] or {}
        missing = [s for s in section.settings if s.name not in present]
        if not missing:
            continue
        bounds = _section_bounds(lines, section.name)
        if bounds is None:
            continue
        _, end = bounds
        block: list[str] = []
        for setting in missing:
            block.append("")
            block += _render_setting(setting)
            report.added_keys.insert(0, f"{section.name}.{setting.name}")
        lines[end:end] = block

    for section in SCHEMA:
        if section.name in data:
            continue
        report.added_sections.append(section.name)
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", "", *_render_section(section)]

    if report.changed and not dry_run:
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
        report.backup = backup
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return report
