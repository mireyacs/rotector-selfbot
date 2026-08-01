"""Minimal Discord REST client for a user token.

Covers the read-only endpoints the scanner needs -- identity, guilds, channels,
relationships and private channels -- plus the few moderation actions the UI
offers. Nothing here sends messages.
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

#: private channel types
DM_CHANNEL = 1
GROUP_DM_CHANNEL = 3

#: relationship types, as used by /users/@me/relationships
FRIEND = 1
BLOCKED = 2
INCOMING_REQUEST = 3
OUTGOING_REQUEST = 4

VIEW_CHANNEL = 1 << 10
ADMINISTRATOR = 1 << 3
KICK_MEMBERS = 1 << 1
BAN_MEMBERS = 1 << 2
MANAGE_ROLES = 1 << 28

#: Holding any of these lets the gateway hand over the *entire* member list,
#: offline members included, in a single request. Without them Discord will
#: only ever expose the member sidebar, which hides offline members in a large
#: guild -- so this permission check is the difference between a complete scan
#: and a partial one.
CHUNK_PERMISSIONS = KICK_MEMBERS | BAN_MEMBERS | MANAGE_ROLES | ADMINISTRATOR


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

    @property
    def can_chunk(self) -> bool:
        """Whether this account may request every member at once."""
        return bool(self.permissions & CHUNK_PERMISSIONS)

    @property
    def offline_members_hidden(self) -> bool:
        """Whether Discord will withhold offline members from the sidebar.

        Mirrors the client's own heuristic: member count, plus hoisted role
        groups, plus the online/offline groups, at or above 1,000.
        """
        return (self.member_count or 0) + 2 >= 1000

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
class Relationship:
    """An entry from the user's relationship list."""

    user_id: str
    username: str
    global_name: str | None
    discriminator: str
    nickname: str | None
    type: int
    bot: bool = False

    @classmethod
    def parse(cls, raw: dict) -> "Relationship | None":
        user = raw.get("user") or {}
        if not user.get("id"):
            return None
        return cls(
            user_id=str(user["id"]),
            username=user.get("username") or "unknown",
            global_name=user.get("global_name"),
            discriminator=str(user.get("discriminator") or "0"),
            nickname=raw.get("nickname"),
            type=int(raw.get("type") or 0),
            bot=bool(user.get("bot")),
        )


@dataclass
class PrivateChannel:
    """A DM or group DM the account is a party to."""

    id: str
    type: int
    name: str | None
    owner_id: str | None
    recipients: list[dict]

    @property
    def is_group(self) -> bool:
        return self.type == GROUP_DM_CHANNEL

    def display_name(self) -> str:
        if self.name:
            return self.name
        names = [
            r.get("global_name") or r.get("username") or "?"
            for r in self.recipients[:3]
        ]
        label = ", ".join(names) if names else "empty group"
        if len(self.recipients) > 3:
            label += f" +{len(self.recipients) - 3}"
        return label

    @classmethod
    def parse(cls, raw: dict) -> "PrivateChannel | None":
        if not raw.get("id"):
            return None
        return cls(
            id=str(raw["id"]),
            type=int(raw.get("type") or 0),
            name=raw.get("name"),
            owner_id=str(raw["owner_id"]) if raw.get("owner_id") else None,
            recipients=list(raw.get("recipients") or []),
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

    async def relationships(self) -> list[Relationship]:
        """Friends, pending requests and blocks, in one call."""
        raw = await self._get("/users/@me/relationships")
        out = [Relationship.parse(entry) for entry in raw]
        return [r for r in out if r is not None]

    async def private_channels(self) -> list[PrivateChannel]:
        """Open DMs and group DMs."""
        raw = await self._get("/users/@me/channels")
        out = [PrivateChannel.parse(entry) for entry in raw]
        return [c for c in out if c is not None]

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


    async def remove_friend(self, user_id: str) -> None:
        """Drop a relationship: unfriend, or withdraw/decline a request."""
        await self._request("DELETE", f"/users/@me/relationships/{user_id}")

    async def block_user(self, user_id: str) -> None:
        """Block a user. Also removes any existing friendship."""
        await self._request(
            "PUT", f"/users/@me/relationships/{user_id}", json={"type": BLOCKED}
        )

    async def find_dm_channel(self, user_id: str) -> str | None:
        """The existing DM channel with ``user_id``, or None.

        Deliberately a lookup rather than ``POST /users/@me/channels``: that
        endpoint *opens* a DM as a side effect, and opening a conversation with
        someone in order to delete a conversation that never existed is not
        what anybody wants.
        """
        for channel in await self.private_channels():
            if channel.type != DM_CHANNEL:
                continue
            recipients = channel.recipients or []
            if len(recipients) == 1 and str(recipients[0].get("id")) == str(user_id):
                return channel.id
        return None

    async def channel_messages(
        self, channel_id: str, limit: int = 100, before: str | None = None
    ) -> list[dict]:
        """One page of messages, newest first."""
        params = {"limit": str(max(1, min(100, limit)))}
        if before:
            params["before"] = before
        resp = await self._request(
            "GET", f"/channels/{channel_id}/messages", params=params
        )
        body = resp.json()
        return body if isinstance(body, list) else []

    async def delete_message(self, channel_id: str, message_id: str) -> None:
        await self._request(
            "DELETE", f"/channels/{channel_id}/messages/{message_id}"
        )

    async def leave_group_dm(self, channel_id: str, silent: bool = False) -> None:
        """Leave a group DM.

        ``silent`` suppresses the "left the group" system message the others
        would otherwise see -- the same option the official client offers.
        """
        params = {"silent": "true"} if silent else None
        await self._request("DELETE", f"/channels/{channel_id}", params=params)

    async def remove_group_recipient(self, channel_id: str, user_id: str) -> None:
        """Remove someone from a group DM. Only the group owner may do this."""
        await self._request(
            "DELETE", f"/channels/{channel_id}/recipients/{user_id}"
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
