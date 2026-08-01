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


@dataclass
class Config:
    token: str | None = None
    rotector: RotectorConfig = field(default_factory=RotectorConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
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

        rot = data.get("rotector") or {}
        for key in ("api_key", "rate_limit", "window", "reserve", "cache_ttl", "concurrency"):
            if key in rot and rot[key] not in (None, ""):
                setattr(self.rotector, key, rot[key])

        scan = data.get("scan") or {}
        for key in ("skip_bots", "max_members", "hide_no_detections",
                    "hide_unknown"):
            if key in scan and scan[key] is not None:
                setattr(self.scan, key, scan[key])

        proxy = data.get("proxy") or {}
        for key in ("enabled", "file", "list", "direct_as_fallback",
                    "max_failures", "timeout", "probe_concurrency"):
            if key in proxy and proxy[key] is not None:
                setattr(self.proxy, key, proxy[key])

    def validate(self) -> list[str]:
        problems = []
        if not self.token:
            problems.append(
                "No Discord token. Set DISCORD_TOKEN, or put one in "
                f"{CONFIG_NAME} under [discord] token = \"...\"."
            )
        if self.rotector.reserve >= self.rotector.rate_limit:
            problems.append("rotector.reserve must be smaller than rotector.rate_limit")
        return problems
