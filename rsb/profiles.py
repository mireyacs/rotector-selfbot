"""Fetching the Discord profile assets a card render needs.

The avatar hash *is* in the member payload, so avatars cost nothing extra --
pass what you already have as ``seed_avatars``.  Banners and bios are not, and
the route that carries them is often refused outright for a user account, so
everything here fails soft: a card with an avatar and no banner beats one with
neither, and an export that dies because a CDN request timed out beats nothing
at all.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field

import httpx

CDN = "https://cdn.discordapp.com"
#: avatar/banner pixel size requested from the CDN
AVATAR_SIZE = 160
BANNER_SIZE = 600

#: Discord's default avatar palette, indexed the way the client indexes it
DEFAULT_AVATAR_COLOURS = [
    (88, 101, 242),
    (87, 242, 135),
    (254, 231, 92),
    (235, 69, 158),
    (237, 66, 69),
    (148, 156, 247),
]


@dataclass
class Profile:
    """What a card can show about one account."""

    user_id: str
    username: str = ""
    global_name: str | None = None
    avatar_url: str | None = None
    banner_url: str | None = None
    accent_colour: tuple[int, int, int] | None = None
    avatar_bytes: bytes | None = None
    banner_bytes: bytes | None = None
    bio: str = ""
    created_at: str = ""
    pronouns: str = ""
    #: (label, description) per badge, e.g. HypeSquad, Nitro tenure
    badges: list[tuple[str, str]] = field(default_factory=list)
    #: (platform, name, verified) for linked Steam/Spotify/etc accounts
    connections: list[tuple[str, str, bool]] = field(default_factory=list)
    mutual_guilds: int = 0
    premium_since: str = ""
    guild_joined_at: str = ""
    #: set when the member is currently timed out in the guild being scanned
    timed_out_until: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def has_nitro(self) -> bool:
        return bool(self.premium_since)

    @property
    def display_name(self) -> str:
        return self.global_name or self.username or self.user_id

    @property
    def fallback_colour(self) -> tuple[int, int, int]:
        """The colour Discord would use for a default avatar."""
        try:
            index = (int(self.user_id) >> 22) % len(DEFAULT_AVATAR_COLOURS)
        except (TypeError, ValueError):
            index = 0
        return DEFAULT_AVATAR_COLOURS[index]


def _snowflake_date(user_id: str) -> str:
    """Account creation date, which is encoded in the id itself."""
    try:
        millis = (int(user_id) >> 22) + 1_420_070_400_000
    except (TypeError, ValueError):
        return ""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(millis / 1000, timezone.utc).strftime("%Y-%m-%d")


def _date(value) -> str:
    """An ISO timestamp trimmed to a date, or empty."""
    text = str(value or "")
    return text[:10] if len(text) >= 10 and text[4] == "-" else ""


def _colour(value) -> tuple[int, int, int] | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return ((number >> 16) & 255, (number >> 8) & 255, number & 255)


async def fetch_profiles(
    http,
    user_ids: Sequence[str],
    *,
    with_images: bool = True,
    concurrency: int = 4,
    timeout: float = 15.0,
    on_progress=None,
    seed_avatars: dict[str, str] | None = None,
    guild_id: str | None = None,
) -> dict[str, Profile]:
    """Collect profiles for ``user_ids``, tolerating whatever fails.

    ``seed_avatars`` maps user id to an avatar hash already in hand. Member
    payloads carry the hash, so avatars need no request at all -- which matters
    because the profile route is frequently refused outright, and a card with
    an avatar and no banner is far better than one with neither.
    """
    wanted = list(dict.fromkeys(str(i) for i in user_ids))
    out: dict[str, Profile] = {}
    if not wanted:
        return out

    semaphore = asyncio.Semaphore(max(1, concurrency))
    done = 0
    cdn = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def grab(url: str) -> bytes | None:
        try:
            response = await cdn.get(url)
            if response.status_code == 200:
                return response.content
        except httpx.HTTPError:
            return None
        return None

    seeds = seed_avatars or {}

    def avatar_url(user_id: str, avatar_hash: str) -> str:
        ext = "gif" if str(avatar_hash).startswith("a_") else "png"
        return f"{CDN}/avatars/{user_id}/{avatar_hash}.{ext}?size={AVATAR_SIZE}"

    async def one(user_id: str) -> None:
        nonlocal done
        profile = Profile(user_id=user_id, created_at=_snowflake_date(user_id))
        # what we already know, before asking Discord anything
        if seeds.get(user_id):
            profile.avatar_url = avatar_url(user_id, seeds[user_id])
        async with semaphore:
            try:
                data = await http.user(user_id, guild_id=guild_id)
            except Exception as exc:  # noqa: BLE001 - recorded on the card
                data = None
                if not profile.avatar_url:
                    profile.errors.append(
                        f"profile unavailable ({type(exc).__name__})"
                    )
                else:
                    # the avatar came from the member list, so only the banner
                    # and bio are missing -- worth saying, not worth shouting
                    profile.errors.append("banner unavailable")

            if data:
                profile.username = data.get("username") or ""
                profile.global_name = data.get("global_name")
                profile.bio = (data.get("bio") or "").strip()
                profile.pronouns = (data.get("pronouns") or "").strip()
                accent = _colour(data.get("accent_color"))
                if accent:
                    profile.accent_colour = accent

                profile.badges = [
                    (str(b.get("id") or ""), str(b.get("description") or ""))
                    for b in (data.get("badges") or [])
                    if isinstance(b, dict)
                ]
                profile.connections = [
                    (
                        str(c.get("type") or ""),
                        str(c.get("name") or ""),
                        bool(c.get("verified")),
                    )
                    for c in (data.get("connected_accounts") or [])
                    if isinstance(c, dict)
                ]
                profile.mutual_guilds = len(data.get("mutual_guilds") or [])
                profile.premium_since = _date(data.get("premium_since"))
                profile.guild_joined_at = _date(data.get("guild_joined_at"))
                profile.timed_out_until = _date(data.get("timed_out_until"))
                if data.get("avatar"):
                    profile.avatar_url = avatar_url(user_id, data["avatar"])
                if data.get("banner"):
                    ext = "gif" if str(data["banner"]).startswith("a_") else "png"
                    profile.banner_url = (
                        f"{CDN}/banners/{user_id}/{data['banner']}.{ext}"
                        f"?size={BANNER_SIZE}"
                    )

            if with_images:
                if profile.avatar_url:
                    profile.avatar_bytes = await grab(profile.avatar_url)
                    if profile.avatar_bytes is None:
                        profile.errors.append("avatar download failed")
                if profile.banner_url:
                    profile.banner_bytes = await grab(profile.banner_url)

        out[user_id] = profile
        done += 1
        if on_progress:
            on_progress("Fetching profiles", done, len(wanted))

    try:
        await asyncio.gather(*(one(i) for i in wanted), return_exceptions=True)
    finally:
        await cdn.aclose()
    return out
