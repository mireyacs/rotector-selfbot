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
ADMINISTRATOR = 1 << 3


class DiscordAuthError(RuntimeError):
    pass


class DiscordHTTPError(RuntimeError):
    pass


class DiscordForbidden(DiscordHTTPError):
    """The account lacks the permission for this action."""


class DiscordNotFound(DiscordHTTPError):
    """The target no longer exists (already kicked, or left)."""


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
    parent_id: str | None = None

    @property
    def visibility(self) -> str:
        return "everyone" if self.everyone_can_view else "restricted"


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

    async def _request(self, method: str, path: str, **kwargs):
        for attempt in range(5):
            resp = await self._http.request(method, path, **kwargs)
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
                raise DiscordForbidden(f"{path}: missing permissions (403)")
            if resp.status_code == 404:
                raise DiscordNotFound(f"{path}: not found (404)")
            if resp.status_code >= 500:
                await asyncio.sleep(min(2**attempt, 10))
                continue
            if resp.status_code >= 400:
                detail = _safe_json(resp).get("message") or resp.text[:200]
                raise DiscordHTTPError(f"{path}: HTTP {resp.status_code} {detail}")
            return resp
        raise DiscordHTTPError(f"{path}: gave up after repeated failures")

    async def _get(self, path: str, **params):
        resp = await self._request("GET", path, params=params or None)
        return resp.json()

    async def me(self) -> dict:
        return await self._get("/users/@me")

    async def guilds(self) -> list[Guild]:
        raw = await self._get("/users/@me/guilds", with_counts="true")
        guilds = [Guild.parse(g) for g in raw]
        guilds.sort(key=lambda g: (-(g.member_count or 0), g.name.lower()))
        return guilds

    async def everyone_permissions(self, guild_id: str) -> int:
        """Base permissions of the @everyone role (its id equals the guild id)."""
        try:
            roles = await self._get(f"/guilds/{guild_id}/roles")
        except (DiscordHTTPError, DiscordForbidden):
            return VIEW_CHANNEL  # assume the common default rather than give up
        for role in roles:
            if str(role.get("id")) == str(guild_id):
                try:
                    return int(role.get("permissions") or 0)
                except (TypeError, ValueError):
                    return VIEW_CHANNEL
        return VIEW_CHANNEL

    async def channels(self, guild_id: str) -> list[Channel]:
        """Text channels, ordered so the most *widely visible* come first.

        This ordering is load-bearing for coverage. The member sidebar only
        lists members who can see the channel it belongs to, so scraping a
        staff-only channel silently returns a partial member list. Channels
        @everyone can view are therefore always tried first.

        Visibility is computed the way Discord computes it: the @everyone role's
        base permissions, then the category's overwrite for @everyone, then the
        channel's own -- not just a single overwrite lookup.
        """
        raw = await self._get(f"/guilds/{guild_id}/channels")
        base = await self.everyone_permissions(guild_id)
        by_id = {str(c["id"]): c for c in raw if c.get("id")}

        out: list[Channel] = []
        for channel in raw:
            if channel.get("type") not in TEXT_CHANNEL_TYPES:
                continue
            out.append(
                Channel(
                    id=str(channel["id"]),
                    name=channel.get("name") or "?",
                    type=int(channel.get("type", 0)),
                    position=int(channel.get("position") or 0),
                    parent_id=(
                        str(channel["parent_id"]) if channel.get("parent_id") else None
                    ),
                    everyone_can_view=_everyone_can_view(channel, guild_id, base, by_id),
                )
            )
        out.sort(key=lambda c: (not c.everyone_can_view, c.position))
        return out

    # -- moderation --------------------------------------------------------

    async def kick(self, guild_id: str, user_id: str, reason: str) -> None:
        """Remove a member. They can rejoin with a new invite."""
        await self._request(
            "DELETE",
            f"/guilds/{guild_id}/members/{user_id}",
            headers=_reason_header(reason),
        )

    async def ban(
        self,
        guild_id: str,
        user_id: str,
        reason: str,
        delete_message_seconds: int = 0,
    ) -> None:
        """Ban a member, optionally purging their recent messages."""
        await self._request(
            "PUT",
            f"/guilds/{guild_id}/bans/{user_id}",
            headers=_reason_header(reason),
            json={
                "delete_message_seconds": max(0, min(604800, delete_message_seconds))
            },
        )


#: Discord truncates audit-log reasons past this
MAX_REASON = 512


def _reason_header(reason: str) -> dict[str, str]:
    """Audit-log reason, trimmed and encoded as Discord requires."""
    from urllib.parse import quote

    text = " ".join((reason or "").split())[:MAX_REASON]
    return {"X-Audit-Log-Reason": quote(text, safe="")}


def _apply_overwrite(allowed: bool, overwrite: dict) -> bool:
    try:
        deny = int(overwrite.get("deny") or 0)
        allow = int(overwrite.get("allow") or 0)
    except (TypeError, ValueError):
        return allowed
    if deny & VIEW_CHANNEL:
        allowed = False
    if allow & VIEW_CHANNEL:
        allowed = True
    return allowed


def _everyone_overwrite(channel: dict, guild_id: str) -> dict | None:
    for overwrite in channel.get("permission_overwrites") or []:
        # type 0 == role; the @everyone role id equals the guild id
        if str(overwrite.get("id")) == str(guild_id) and int(overwrite.get("type", 0)) == 0:
            return overwrite
    return None


def _everyone_can_view(
    channel: dict, guild_id: str, base_permissions: int, by_id: dict[str, dict]
) -> bool:
    """Can the @everyone role view this channel?

    Base role permissions, then the category's @everyone overwrite, then the
    channel's own -- the same order Discord resolves them in.
    """
    if base_permissions & ADMINISTRATOR:
        return True
    allowed = bool(base_permissions & VIEW_CHANNEL)

    parent_id = channel.get("parent_id")
    if parent_id:
        parent = by_id.get(str(parent_id))
        if parent:
            overwrite = _everyone_overwrite(parent, guild_id)
            if overwrite:
                allowed = _apply_overwrite(allowed, overwrite)

    overwrite = _everyone_overwrite(channel, guild_id)
    if overwrite:
        allowed = _apply_overwrite(allowed, overwrite)
    return allowed


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {}
