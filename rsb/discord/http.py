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

#: Build number the client identifies as. Discord rejects or degrades requests
#: from a client it cannot place, which is why several routes answer 403 for a
#: request that carries no super-properties at all.
CLIENT_BUILD = 586984
CLIENT_VERSION = "1.0.144"


def super_properties() -> dict:
    """The client descriptor Discord expects alongside a user token.

    Sent as ``x-super-properties`` on HTTP and inside IDENTIFY on the gateway --
    the same blob in both places, because a client that describes itself two
    different ways is exactly what looks automated.
    """
    return {
        "os": "Linux",
        "browser": "Discord Client",
        "release_channel": "stable",
        "client_version": CLIENT_VERSION,
        "os_version": "",
        "os_arch": "x64",
        "app_arch": "x64",
        "system_locale": "en-US",
        "has_client_mods": False,
        "browser_user_agent": BROWSER_UA,
        "browser_version": "37.6.0",
        "client_build_number": CLIENT_BUILD,
        "native_build_number": None,
        "client_event_source": None,
    }


def super_properties_header() -> str:
    import base64
    import json

    blob = json.dumps(super_properties(), separators=(",", ":"))
    return base64.b64encode(blob.encode()).decode()

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "discord/1.0.144 Chrome/138.0.7204.251 Electron/37.6.0 Safari/537.36"
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


#: Discord asks bots to identify themselves with a URL and a version rather
#: than to imitate a browser -- and a bot pretending to be Chrome is exactly
#: the sort of thing that gets an application flagged.
BOT_UA = "DiscordBot (https://github.com/rotector-selfbot, 1.0)"


class DiscordAuthError(RuntimeError):
    pass


class DiscordHTTPError(RuntimeError):
    pass


class DiscordForbidden(DiscordHTTPError):
    """The account lacks the permission for this action."""


class BotUnsupported(DiscordHTTPError):
    """Asked of a bot token something only a user account can do."""


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
    avatar: str | None = None

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
            avatar=user.get("avatar"),
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
    """Discord's HTTP API, as either a user account or a bot application.

    The two differ in more than a header. A user token has to look like the
    real client down to ``x-super-properties`` or several routes answer 403; a
    bot token must *not* look like that, and Discord asks bots to identify
    themselves honestly instead. Bots in exchange get routes a user account is
    refused outright -- ``GET /guilds/{id}/members`` chief among them.
    """

    #: class-level default so a subclass that replaces __init__ (test stubs
    #: do exactly this) still answers the user-vs-bot question sensibly
    is_bot = False

    def __init__(self, token: str, timeout: float = 30.0, bot: bool = False) -> None:
        self.token = token
        self.is_bot = bot
        if bot:
            headers = {
                "authorization": f"Bot {token}",
                "user-agent": BOT_UA,
                "accept": "*/*",
            }
        else:
            headers = {
                "authorization": token,
                "user-agent": BROWSER_UA,
                "accept": "*/*",
                "accept-language": "en-US,en;q=0.9",
                "x-discord-locale": "en-US",
                "x-discord-timezone": "UTC",
                "x-debug-options": "bugReporterEnabled",
                # without this several routes answer 403, the profile one
                # among them
                "x-super-properties": super_properties_header(),
                "referer": "https://discord.com/channels/@me",
            }
        self._http = httpx.AsyncClient(
            base_url=API_BASE, timeout=timeout, headers=headers
        )

    def _bot_only(self, what: str) -> None:
        if not self.is_bot:
            return
        raise BotUnsupported(
            f"{what} is not something a bot application can do -- it has no "
            f"friends, no DMs of its own and no profile to act from. Supply a "
            f"user token for this."
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

    async def list_guild_members(
        self, guild_id: str, limit: int = 1000, after: str = "0"
    ) -> list[dict]:
        """One page of ``GET /guilds/{id}/members``, bot tokens only.

        This is the route that makes a bot token worth having: it lists *every*
        member, offline included, with no dependency on what a member sidebar
        happens to show. It is paginated by ascending user id via ``after``,
        at most 1000 per call, and Discord requires the GUILD_MEMBERS
        privileged intent to be enabled for the application.
        """
        if not self.is_bot:
            raise BotUnsupported(
                "GET /guilds/{id}/members is refused for user accounts; "
                "members have to be read from the member sidebar instead."
            )
        return await self._get(
            f"/guilds/{guild_id}/members",
            limit=str(max(1, min(1000, limit))),
            after=str(after),
        )

    async def all_guild_members(
        self,
        guild_id: str,
        on_progress=None,
        on_members=None,
        expected: int | None = None,
        page_delay: float = 0.35,
    ) -> list[dict]:
        """Every member of a guild, page by page. Bot tokens only.

        A second, independent way to get the same list the gateway chunks
        give. It exists because the two fail differently: the gateway path is
        one long-lived socket that can be dropped mid-list, while this is a
        series of small stateless requests that can simply be resumed. When
        the socket falls short, this finishes the job.

        Pagination is by ascending user id, so ``after`` is the highest id
        seen -- there is no page number to lose track of.
        """
        out: list[dict] = []
        after = "0"
        while True:
            page = await self.list_guild_members(guild_id, 1000, after)
            if not page:
                break
            out.extend(page)
            if on_members:
                on_members(page)
            highest = after
            for entry in page:
                member_id = str((entry.get("user") or {}).get("id") or "")
                if member_id and (highest == "0" or int(member_id) > int(highest)):
                    highest = member_id
            if on_progress:
                on_progress(
                    len(out), expected,
                    f"Listing members directly ({len(out):,} so far)",
                )
            if highest == after or len(page) < 1000:
                break
            after = highest
            await asyncio.sleep(page_delay)
        return out

    async def guild_member(self, guild_id: str, user_id: str) -> dict:
        """One member of a guild, with their roles and join date."""
        return await self._get(f"/guilds/{guild_id}/members/{user_id}")

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
        self._bot_only("Reading the friend list")
        raw = await self._get("/users/@me/relationships")
        out = [Relationship.parse(entry) for entry in raw]
        return [r for r in out if r is not None]

    async def private_channels(self) -> list[PrivateChannel]:
        """Open DMs and group DMs."""
        self._bot_only("Reading direct messages")
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
        self._bot_only("Removing a friend")
        await self._request("DELETE", f"/users/@me/relationships/{user_id}")

    async def block_user(self, user_id: str) -> None:
        """Block a user. Also removes any existing friendship."""
        self._bot_only("Blocking someone")
        await self._request(
            "PUT", f"/users/@me/relationships/{user_id}", json={"type": BLOCKED}
        )

    async def user(self, user_id: str, guild_id: str | None = None) -> dict:
        """A user's profile, as the client fetches it for a popout.

        The plain ``/users/{id}`` route is a bot route and answers 403 for a
        user token, so the popout endpoint is used instead. Passing
        ``guild_id`` additionally returns that guild's member record -- roles,
        join date, and whether they are currently timed out.

        The response is flattened into one mapping: ``user``, then anything
        ``user_profile`` adds, plus badges, connections and mutual guilds.
        """
        if self.is_bot:
            return await self._bot_user(user_id, guild_id)

        params = {
            "type": "popout",
            "with_mutual_guilds": "true",
            "with_mutual_friends": "false",
            "with_mutual_friends_count": "false",
        }
        if guild_id:
            params["guild_id"] = str(guild_id)

        try:
            data = await self._get(f"/users/{user_id}/profile", **params)
        except DiscordHTTPError:
            return await self._get(f"/users/{user_id}")

        user = dict(data.get("user") or {})
        extra = data.get("user_profile") or {}
        for key in ("banner", "accent_color", "bio", "banner_color", "pronouns",
                    "theme_colors"):
            if extra.get(key) is not None:
                user[key] = extra[key]

        user["badges"] = data.get("badges") or []
        user["connected_accounts"] = data.get("connected_accounts") or []
        user["mutual_guilds"] = data.get("mutual_guilds") or []
        user["premium_type"] = data.get("premium_type")
        user["premium_since"] = data.get("premium_since")
        member = data.get("guild_member") or {}
        if member:
            user["guild_roles"] = member.get("roles") or []
            user["guild_joined_at"] = member.get("joined_at")
            user["guild_nick"] = member.get("nick")
            user["timed_out_until"] = member.get("communication_disabled_until")
        return user

    async def _bot_user(self, user_id: str, guild_id: str | None) -> dict:
        """The same shape as the popout profile, from the bot routes.

        A bot gets the plain user object -- which is where the banner and
        accent colour live for it -- and, in a guild, the member record. What
        it cannot see at all is the profile bio, pronouns, linked accounts and
        badges: those are behind the user-only popout route. Missing is
        reported as missing rather than faked.
        """
        user = dict(await self._get(f"/users/{user_id}"))
        user.setdefault("badges", [])
        user.setdefault("connected_accounts", [])
        user.setdefault("mutual_guilds", [])
        if guild_id:
            try:
                member = await self._get(f"/guilds/{guild_id}/members/{user_id}")
            except (DiscordHTTPError, DiscordForbidden, DiscordNotFound):
                member = {}
            if member:
                user["guild_roles"] = member.get("roles") or []
                user["guild_joined_at"] = member.get("joined_at")
                user["guild_nick"] = member.get("nick")
                user["timed_out_until"] = member.get(
                    "communication_disabled_until"
                )
                if member.get("banner"):
                    user.setdefault("banner", member["banner"])
        return user

    async def widget(self, guild_id: str) -> dict | None:
        """The guild's public widget, or None if it is not enabled.

        Needs no permissions and no membership -- but it returns at most 100
        members, only ones who are online, and it **anonymises their ids**
        (they come back as 0, 1, 2 ... rather than snowflakes). So it is a
        source of *names*, not of users: each still has to be resolved to a
        real account before it means anything.
        """
        try:
            return await self._get(f"/guilds/{guild_id}/widget.json")
        except (DiscordForbidden, DiscordNotFound):
            return None  # widget disabled, which is the common case
        except DiscordHTTPError:
            return None

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

    async def open_dm(self, user_id: str) -> str:
        """Open (or reuse) a DM channel with someone, returning its id.

        Unlike :meth:`find_dm_channel` this deliberately *creates* the channel
        when none exists -- which is the point when the intent is to tell
        somebody something before acting on them.
        """
        resp = await self._request(
            "POST", "/users/@me/channels", json={"recipient_id": str(user_id)}
        )
        return str(resp.json().get("id") or "")

    async def send_message(self, channel_id: str, content: str) -> dict:
        resp = await self._request(
            "POST", f"/channels/{channel_id}/messages",
            json={"content": content[:MAX_MESSAGE]},
        )
        return _safe_json(resp)

    async def send_dm(self, user_id: str, content: str) -> dict:
        """Send one direct message, opening the channel if needed.

        Raises like any other request -- a closed DM is a 403, and the caller
        is expected to carry on rather than treat it as a failure of the thing
        it was actually trying to do.
        """
        channel_id = await self.open_dm(user_id)
        if not channel_id:
            raise DiscordHTTPError("could not open a DM channel")
        return await self.send_message(channel_id, content)

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
        self._bot_only("Leaving a group DM")
        params = {"silent": "true"} if silent else None
        await self._request("DELETE", f"/channels/{channel_id}", params=params)

    async def remove_group_recipient(self, channel_id: str, user_id: str) -> None:
        """Remove someone from a group DM. Only the group owner may do this."""
        self._bot_only("Removing someone from a group DM")
        await self._request(
            "DELETE", f"/channels/{channel_id}/recipients/{user_id}"
        )


#: Discord truncates audit-log reasons past this
MAX_MESSAGE = 2000
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
