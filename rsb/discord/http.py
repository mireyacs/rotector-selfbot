"""Minimal Discord REST client for a user token.

Only the read-only endpoints the scanner needs: identity, guild list and
channel list.  Nothing here sends messages or mutates state.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

API_BASE = "https://discord.com/api/v9"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

#: text-ish channel types whose member sidebar we can subscribe to
TEXT_CHANNEL_TYPES = {0, 5}

VIEW_CHANNEL = 1 << 10


class DiscordAuthError(RuntimeError):
    pass


class DiscordHTTPError(RuntimeError):
    pass


@dataclass
class Guild:
    id: str
    name: str
    owner: bool
    permissions: int
    member_count: int | None
    presence_count: int | None
    icon: str | None = None

    @classmethod
    def parse(cls, raw: dict) -> "Guild":
        return cls(
            id=str(raw["id"]),
            name=raw.get("name") or "(unknown)",
            owner=bool(raw.get("owner")),
            permissions=int(raw.get("permissions") or 0),
            member_count=raw.get("approximate_member_count"),
            presence_count=raw.get("approximate_presence_count"),
            icon=raw.get("icon"),
        )


@dataclass
class Channel:
    id: str
    name: str
    type: int
    position: int
    everyone_can_view: bool


class DiscordHTTP:
    def __init__(self, token: str, timeout: float = 30.0) -> None:
        self.token = token
        self._http = httpx.AsyncClient(
            base_url=API_BASE,
            timeout=timeout,
            headers={
                "authorization": token,
                "user-agent": BROWSER_UA,
                "accept": "*/*",
                "accept-language": "en-US,en;q=0.9",
                "x-discord-locale": "en-US",
                "referer": "https://discord.com/channels/@me",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, **params):
        for attempt in range(5):
            resp = await self._http.get(path, params=params or None)
            if resp.status_code == 429:
                body = _safe_json(resp)
                await asyncio.sleep(float(body.get("retry_after", 1.0)) + 0.25)
                continue
            if resp.status_code == 401:
                raise DiscordAuthError(
                    "Discord rejected the token (401). It may be expired, "
                    "or invalidated by a password change."
                )
            if resp.status_code == 403:
                raise DiscordHTTPError(f"{path}: forbidden (403)")
            if resp.status_code >= 500:
                await asyncio.sleep(min(2**attempt, 10))
                continue
            if resp.status_code >= 400:
                raise DiscordHTTPError(f"{path}: HTTP {resp.status_code} {resp.text[:200]}")
            return resp.json()
        raise DiscordHTTPError(f"{path}: gave up after repeated failures")

    async def me(self) -> dict:
        return await self._get("/users/@me")

    async def guilds(self) -> list[Guild]:
        raw = await self._get("/users/@me/guilds", with_counts="true")
        guilds = [Guild.parse(g) for g in raw]
        guilds.sort(key=lambda g: (-(g.member_count or 0), g.name.lower()))
        return guilds

    async def channels(self, guild_id: str) -> list[Channel]:
        """Text channels, most-likely-viewable first.

        The member sidebar is keyed by a channel's permission set, so any
        channel @everyone can read yields the full list.  Channels with an
        explicit @everyone VIEW_CHANNEL deny are tried last.
        """
        raw = await self._get(f"/guilds/{guild_id}/channels")
        out: list[Channel] = []
        for c in raw:
            if c.get("type") not in TEXT_CHANNEL_TYPES:
                continue
            out.append(
                Channel(
                    id=str(c["id"]),
                    name=c.get("name") or "?",
                    type=int(c.get("type", 0)),
                    position=int(c.get("position") or 0),
                    everyone_can_view=_everyone_can_view(c, guild_id),
                )
            )
        out.sort(key=lambda c: (not c.everyone_can_view, c.position))
        return out


def _everyone_can_view(channel: dict, guild_id: str) -> bool:
    for ow in channel.get("permission_overwrites") or []:
        # type 0 == role; the @everyone role id equals the guild id
        if str(ow.get("id")) == str(guild_id) and int(ow.get("type", 0)) == 0:
            if int(ow.get("deny") or 0) & VIEW_CHANNEL:
                return False
    return True


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {}
