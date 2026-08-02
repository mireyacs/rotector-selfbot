"""Configuration loading.

Order of precedence, lowest to highest:

  1. built-in defaults
  2. ``config.toml`` (cwd, then ``$XDG_CONFIG_HOME/rotector-selfbot/``)
  3. environment variables (``DISCORD_TOKEN``, ``ROTECTOR_API_KEY``)
  4. command line flags
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = "config.toml"


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "rotector-selfbot"


def candidate_paths() -> list[Path]:
    return [Path.cwd() / CONFIG_NAME, _config_dir() / CONFIG_NAME]


@dataclass
class RotectorConfig:
    api_key: str | None = None
    #: documented standard tier: 50 requests / 10 seconds, per IP
    rate_limit: int = 50
    window: float = 10.0
    #: request units held back as headroom against clock skew / shared IPs
    reserve: int = 5
    #: in-memory cache lifetime; clamped below the 24h Terms of Use ceiling
    cache_ttl: int = 3600
    concurrency: int = 3


@dataclass
class ProxyConfig:
    """Routing Rotector traffic over proxies.

    Off by default and deliberately so: Rotector's Terms of Use prohibit
    circumventing rate limits by rotating IPs, and the sanctioned way to raise
    throughput is an API key. Enabling this is the operator's decision.
    """

    enabled: bool = False
    #: newline-delimited proxy list; blanks and #comments ignored
    file: str = "proxies.txt"
    #: inline proxies, merged with anything read from `file`
    list: list[str] = field(default_factory=list)
    #: use the direct connection only once no proxy is usable
    direct_as_fallback: bool = False
    #: consecutive failures before a proxy is parked with backoff
    max_failures: int = 3
    timeout: float = 20.0
    #: concurrent probes while testing
    probe_concurrency: int = 10


@dataclass
class ScanConfig:
    #: skip bot accounts, which have no Roblox connections
    skip_bots: bool = True
    #: cap members pulled per guild; 0 means no cap
    max_members: int = 0
    #: hide members Rotector has not flagged -- almost everyone, in practice
    hide_no_detections: bool = True
    #: hide members with no Rotector-known Roblox link -- the larger group still
    hide_unknown: bool = True
    #: what a new scan does with results already on screen:
    #: "ask", "replace", "merge_skip" or "merge_recheck"
    on_rescan: str = "ask"


@dataclass
class ExportConfig:
    #: any of "csv", "txt", "json"
    formats: list[str] = field(default_factory=lambda: ["csv", "txt"])
    #: "filtered" honours the table's current filter; "all" exports everything
    scope: str = "filtered"
    #: rows per CSV segment; 0 writes a single file however long the list is
    segment_size: int = 1000
    columns: list[str] = field(default_factory=list)
    directory: str = "exports"
    #: "table" for the results grid, "cards" for per-member profile images
    png_style: str = "table"
    #: keep exports past the 24h retention window instead of clearing them.
    #: Rotector's terms forbid retaining their responses that long, so leaving
    #: this off means old export folders are swept up automatically.
    preserve: bool = False


@dataclass
class ModerationConfig:
    """Kick/ban from the results table.

    ``require_threat`` reflects Rotector's own guidance: only flag types
    Flagged and Confirmed are documented as safe to action automatically.
    Leaving it on means anything else needs an explicit extra confirmation.
    """

    require_threat: bool = True
    #: {reason} is replaced by the Rotector finding; attribution is always appended
    default_reason: str = "Flagged by Rotector: {reason}"
    #: message history to purge on ban, in seconds (0-604800)
    delete_message_seconds: int = 0
    #: leave group DMs without posting a "left the group" message
    silent_leave: bool = False
    #: whether CAUTION findings may be actioned at all. They are never routine:
    #: Rotector asks that Provisional/Mixed be reviewed by a person, so the UI
    #: requires a second, separate confirmation every time.
    allow_caution: bool = True
    #: try to DM someone before kicking or banning them. Sent first because a
    #: banned account shares no server with you and can no longer be reached.
    #: **Bot tokens only.** A bot messaging members is ordinary; a user
    #: account messaging strangers in bulk is what Discord's spam heuristics
    #: are built to catch, and the penalty lands on the operator's own
    #: account. With a user token the option is not offered at all.
    notify_before_action: bool = True
    #: the notice itself. {action}, {place} and {reason} are filled in; the
    #: appeal link is appended if the text does not already carry one.
    notify_message: str = ""
    #: seconds between members in a bulk action. Bulk kicks and bans are the
    #: fastest way to trip Discord's abuse heuristics on a user account, and
    #: the pause costs nothing next to having the account flagged.
    bulk_delay: float = 1.0


@dataclass
class PurgeConfig:
    """Deleting your own messages from a conversation.

    Only ever your own -- Discord provides no way to remove someone else's
    messages from a DM.
    """

    #: seconds between deletions; Discord's per-channel delete bucket is strict
    delete_delay: float = 1.0
    #: cap on how many of your messages to collect. 0 = no cap
    max_messages: int = 0
    #: only consider history this recent, in days. 0 = all of it
    max_age_days: int = 0


@dataclass
class Config:
    token: str | None = None
    #: a bot application's token, used only when no user token is set.
    #: A bot can reach servers it has been invited to and nothing else -- no
    #: friends, no DMs, no group DMs -- but within those servers it can list
    #: every member outright, which a user account cannot.
    bot_token: str | None = None
    rotector: RotectorConfig = field(default_factory=RotectorConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    moderation: ModerationConfig = field(default_factory=ModerationConfig)
    purge: PurgeConfig = field(default_factory=PurgeConfig)
    source: Path | None = None

    def proxy_urls(self) -> list[str]:
        """Every configured proxy, from the inline list and the list file."""
        entries = list(self.proxy.list or [])
        path = Path(self.proxy.file) if self.proxy.file else None
        if path and not path.is_absolute():
            base = self.source.parent if self.source else Path.cwd()
            path = base / path
        if path and path.is_file():
            entries += path.read_text(encoding="utf-8").splitlines()
        seen, out = set(), []
        for entry in entries:
            entry = (entry or "").strip()
            if not entry or entry.startswith("#") or entry in seen:
                continue
            seen.add(entry)
            out.append(entry)
        return out

    def active_proxies(self) -> list[str]:
        """Proxies to actually route through -- empty unless explicitly on."""
        return self.proxy_urls() if self.proxy.enabled else []

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        cfg = cls()

        paths = [path] if path else candidate_paths()
        for candidate in paths:
            if candidate and candidate.is_file():
                cfg._apply_file(candidate)
                cfg.source = candidate
                break

        env_token = os.environ.get("DISCORD_TOKEN")
        if env_token:
            cfg.token = env_token.strip()
        env_bot = os.environ.get("DISCORD_BOT_TOKEN")
        if env_bot:
            cfg.bot_token = env_bot.strip()
        env_key = os.environ.get("ROTECTOR_API_KEY")
        if env_key:
            cfg.rotector.api_key = env_key.strip()

        return cfg

    def _apply_file(self, path: Path) -> None:
        with path.open("rb") as handle:
            data = tomllib.load(handle)

        discord = data.get("discord") or {}
        token = (discord.get("token") or "").strip()
        if token:
            self.token = token
        bot_token = (discord.get("bot_token") or "").strip()
        if bot_token:
            self.bot_token = bot_token

        rot = data.get("rotector") or {}
        for key in ("api_key", "rate_limit", "window", "reserve", "cache_ttl", "concurrency"):
            if key in rot and rot[key] not in (None, ""):
                setattr(self.rotector, key, rot[key])

        scan = data.get("scan") or {}
        for key in ("skip_bots", "max_members", "hide_no_detections",
                    "hide_unknown", "on_rescan"):
            if key in scan and scan[key] is not None:
                setattr(self.scan, key, scan[key])

        export = data.get("export") or {}
        for key in ("formats", "scope", "segment_size", "columns", "directory",
                    "png_style"):
            if key in export and export[key] is not None:
                setattr(self.export, key, export[key])

        moderation = data.get("moderation") or {}
        for key in ("require_threat", "default_reason", "delete_message_seconds",
                    "silent_leave"):
            if key in moderation and moderation[key] is not None:
                setattr(self.moderation, key, moderation[key])

        purge = data.get("purge") or {}
        for key in ("delete_delay", "max_messages", "max_age_days"):
            if key in purge and purge[key] is not None:
                setattr(self.purge, key, purge[key])

        proxy = data.get("proxy") or {}
        for key in ("enabled", "file", "list", "direct_as_fallback",
                    "max_failures", "timeout", "probe_concurrency"):
            if key in proxy and proxy[key] is not None:
                setattr(self.proxy, key, proxy[key])

    def config_path(self) -> Path:
        """Where settings changed in the UI get written back."""
        if self.source:
            return self.source
        local = Path.cwd() / CONFIG_NAME
        return local if local.is_file() else _config_dir() / CONFIG_NAME

    def save_export_settings(self) -> Path:
        """Persist the [export] section, leaving the rest of the file intact."""
        path = self.config_path()
        body = {
            "formats": self.export.formats,
            "scope": self.export.scope,
            "segment_size": self.export.segment_size,
            "columns": self.export.columns,
            "directory": self.export.directory,
            "preserve": self.export.preserve,
            "png_style": self.export.png_style,
        }
        write_section(path, "export", body)
        self.source = path
        return path

    def save_moderation_settings(self) -> Path:
        """Persist the [moderation] section, leaving the rest of the file intact."""
        path = self.config_path()
        write_section(
            path,
            "moderation",
            {
                "require_threat": self.moderation.require_threat,
                "default_reason": self.moderation.default_reason,
                "delete_message_seconds": self.moderation.delete_message_seconds,
                "silent_leave": self.moderation.silent_leave,
                "allow_caution": self.moderation.allow_caution,
                "notify_before_action": self.moderation.notify_before_action,
                "notify_message": self.moderation.notify_message,
                "bulk_delay": self.moderation.bulk_delay,
            },
        )
        self.source = path
        return path

    def save_scan_settings(self) -> Path:
        """Persist the [scan] section."""
        path = self.config_path()
        write_section(
            path,
            "scan",
            {
                "skip_bots": self.scan.skip_bots,
                "max_members": self.scan.max_members,
                "hide_no_detections": self.scan.hide_no_detections,
                "hide_unknown": self.scan.hide_unknown,
                "on_rescan": self.scan.on_rescan,
            },
        )
        self.source = path
        return path

    def save_purge_settings(self) -> Path:
        """Persist the [purge] section, leaving the rest of the file intact."""
        path = self.config_path()
        write_section(
            path,
            "purge",
            {
                "delete_delay": self.purge.delete_delay,
                "max_messages": self.purge.max_messages,
                "max_age_days": self.purge.max_age_days,
            },
        )
        self.source = path
        return path

    @property
    def is_bot(self) -> bool:
        """Whether this run will authenticate as a bot application.

        A user token always wins when both are present. It can do strictly
        more -- friends, DMs and group DMs are invisible to a bot -- so
        silently dropping to the narrower one because it happens to be
        configured too would be a downgrade nobody asked for.
        """
        return not (self.token or "").strip() and bool(
            (self.bot_token or "").strip()
        )

    @property
    def active_token(self) -> str:
        """The token this run will use, user first."""
        return (
            (self.token or "").strip() or (self.bot_token or "").strip()
        )

    def validate(self) -> list[str]:
        problems = []
        if not self.active_token:
            problems.append(
                "No Discord token. Set DISCORD_TOKEN or DISCORD_BOT_TOKEN, or "
                f"put one in {CONFIG_NAME} under [discord] token = \"...\" "
                f"(or bot_token for a bot application)."
            )
        if self.rotector.reserve >= self.rotector.rate_limit:
            problems.append("rotector.reserve must be smaller than rotector.rate_limit")
        return problems


# --------------------------------------------------------------------------
# writing settings back
# --------------------------------------------------------------------------


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_section(path: Path, section: str, values: dict) -> None:
    """Replace one ``[section]`` in a TOML file, preserving everything else.

    A full re-serialise would strip the comments the example config leans on,
    so only the target section's lines are swapped out; the rest of the file is
    passed through untouched.
    """
    rendered = [f"[{section}]"]
    rendered += [f"{key} = {_toml_value(value)}" for key, value in values.items()]

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(rendered) + "\n", encoding="utf-8")
        return

    lines = path.read_text(encoding="utf-8").splitlines()
    start = end = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if start is None:
            if stripped == f"[{section}]":
                start = index
            continue
        # the section runs until the next table header
        if stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break
    if start is None:
        out = lines + ([""] if lines and lines[-1].strip() else []) + rendered
    else:
        if end is None:
            end = len(lines)
        # keep any trailing blank lines that belonged to the old section
        tail = []
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
            tail.append("")
        out = lines[:start] + rendered + tail + lines[end:]

    path.write_text("\n".join(out) + "\n", encoding="utf-8")
