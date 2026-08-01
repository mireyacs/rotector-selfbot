"""Discord gateway client used to enumerate guild members with a user token.

A user account cannot call the bot-only ``GET /guilds/{id}/members`` endpoint,
so members are read the way the real client reads them: by subscribing to
ranges of the member sidebar.

* **OP 14** (guild subscriptions / "lazy request") asks for slices of the
  sidebar and the server answers with ``GUILD_MEMBER_LIST_UPDATE`` dispatches
  containing ``SYNC`` ops.  This is the primary path.  Caveat inherited from
  Discord: in large guilds the sidebar only lists non-offline members, so a
  scan covers who is *visible*, not necessarily everyone.
* **OP 8** (``REQUEST_GUILD_MEMBERS``) is the fallback for guilds where no
  channel yields a sidebar.  With the right permissions an empty query returns
  everyone; otherwise it is brute-forced over a prefix alphabet.

The gateway allows roughly 120 outbound events per 60 seconds, so every send
goes through its own limiter.
"""

from __future__ import annotations

import asyncio
import json
import random
import string
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from ..ratelimit import RateLimiter
from .http import BROWSER_UA, Channel

GATEWAY_URL = "wss://gateway.discord.gg/?v=9&encoding=json"

#: how long to keep draining events after an OP 14 before deciding it is done
SETTLE_TIMEOUT = 2.5
#: how long to wait for the very first response on a candidate channel
FIRST_RESPONSE_TIMEOUT = 6.0
#: members per sidebar range
RANGE_SIZE = 100
#: absolute cap on one request/drain round, however chatty the guild is
MAX_ROUND_SECONDS = 20.0
#: fraction of the guild's member count treated as full coverage
COVERAGE_TARGET = 0.995
#: pause between OP 8 prefix queries, to stay clear of the gateway event budget
PREFIX_QUERY_DELAY = 0.5
#: how long to wait for chunks after an OP 8 query (they arrive promptly)
CHUNK_TIMEOUT = 1.5

ScrapeProgress = Callable[[int, int | None, str], None]
#: receives only the members newly discovered by a round, as they arrive
MemberSink = Callable[[list["GuildMember"]], None]


class GatewayError(RuntimeError):
    pass


@dataclass
class GuildMember:
    id: str
    username: str
    global_name: str | None = None
    discriminator: str = "0"
    nick: str | None = None
    bot: bool = False
    joined_at: str | None = None
    roles: list[str] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.nick or self.global_name or self.username

    @property
    def tag(self) -> str:
        if self.discriminator in ("0", "0000", ""):
            return f"@{self.username}"
        return f"{self.username}#{self.discriminator}"

    @classmethod
    def parse(cls, raw: dict) -> "GuildMember | None":
        user = raw.get("user") or {}
        uid = user.get("id")
        if not uid:
            return None
        return cls(
            id=str(uid),
            username=user.get("username") or "unknown",
            global_name=user.get("global_name"),
            discriminator=str(user.get("discriminator") or "0"),
            nick=raw.get("nick"),
            bot=bool(user.get("bot")),
            joined_at=raw.get("joined_at"),
            roles=[str(r) for r in (raw.get("roles") or [])],
        )


class DiscordGateway:
    def __init__(self, token: str) -> None:
        self.token = token
        self.user: dict | None = None
        self._ws = None
        self._seq: int | None = None
        self._heartbeat_interval = 41.25
        self._reader: asyncio.Task | None = None
        self._beater: asyncio.Task | None = None
        self._listeners: dict[asyncio.Queue, frozenset[str] | None] = {}
        self._ready = asyncio.Event()
        self._closed = False
        self._fatal: Exception | None = None
        # gateway budget: ~120 events / 60s, kept well under
        self._send_limit = RateLimiter(limit=110, window=60.0, reserve=15)
        #: filled in by fetch_members, for the UI to report coverage
        self.last_coverage: float | None = None
        self.last_scrape_channels: list[str] = []

    # -- lifecycle ---------------------------------------------------------

    async def connect(self, timeout: float = 45.0) -> dict:
        self._ws = await ws_connect(
            GATEWAY_URL,
            max_size=None,
            user_agent_header=BROWSER_UA,
            open_timeout=timeout,
            ping_interval=None,
        )
        self._reader = asyncio.create_task(self._read_loop())
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except TimeoutError:
            await self.close()
            raise GatewayError("timed out waiting for READY") from None
        if self._fatal:
            raise self._fatal
        return self.user or {}

    async def close(self) -> None:
        self._closed = True
        for task in (self._beater, self._reader):
            if task and not task.done():
                task.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None

    async def __aenter__(self) -> "DiscordGateway":
        await self.connect()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    # -- plumbing ----------------------------------------------------------

    async def _send(self, payload: dict, *, metered: bool = True) -> None:
        if self._ws is None:
            raise GatewayError("gateway is not connected")
        if metered:
            await self._send_limit.acquire(1)
        await self._ws.send(json.dumps(payload))

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                op = msg.get("op")
                if msg.get("s") is not None:
                    self._seq = msg["s"]

                if op == 10:  # HELLO
                    self._heartbeat_interval = msg["d"]["heartbeat_interval"] / 1000
                    self._beater = asyncio.create_task(self._heartbeat_loop())
                    await self._identify()
                elif op == 0:  # DISPATCH
                    await self._dispatch(msg.get("t"), msg.get("d") or {})
                elif op == 1:  # heartbeat request
                    await self._send({"op": 1, "d": self._seq}, metered=False)
                elif op == 7:  # reconnect
                    self._fail(GatewayError("gateway asked us to reconnect"))
                elif op == 9:  # invalid session
                    self._fail(
                        GatewayError(
                            "invalid session - the token was rejected by the gateway"
                        )
                    )
        except ConnectionClosed as exc:
            self._fail(GatewayError(f"gateway connection closed: {exc}"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            self._fail(exc)

    def _fail(self, exc: Exception) -> None:
        if not self._closed:
            self._fatal = exc
        self._ready.set()
        # bypasses the per-queue filter: everyone must wake up and re-check
        for queue in list(self._listeners):
            queue.put_nowait(None)

    async def _dispatch(self, event: str | None, data: dict) -> None:
        if event == "READY":
            self.user = data.get("user")
            self._ready.set()
        for queue, wanted in list(self._listeners.items()):
            if wanted is None or event in wanted:
                queue.put_nowait((event, data))

    async def _heartbeat_loop(self) -> None:
        # first beat is jittered, as the real client does
        await asyncio.sleep(self._heartbeat_interval * random.random())
        while not self._closed:
            try:
                await self._send({"op": 1, "d": self._seq}, metered=False)
            except Exception:
                return
            await asyncio.sleep(self._heartbeat_interval)

    async def _identify(self) -> None:
        await self._send(
            {
                "op": 2,
                "d": {
                    "token": self.token,
                    "capabilities": 30717,
                    "properties": {
                        "os": "Windows",
                        "browser": "Chrome",
                        "device": "",
                        "system_locale": "en-US",
                        "browser_user_agent": BROWSER_UA,
                        "browser_version": "128.0.0.0",
                        "os_version": "10",
                        "referrer": "",
                        "referring_domain": "",
                        "referrer_current": "",
                        "referring_domain_current": "",
                        "release_channel": "stable",
                        "client_build_number": 335050,
                        "client_event_source": None,
                    },
                    "presence": {
                        "status": "unknown",
                        "since": 0,
                        "activities": [],
                        "afk": False,
                    },
                    "compress": False,
                    "client_state": {
                        "guild_versions": {},
                        "highest_last_message_id": "0",
                        "read_state_version": 0,
                        "user_guild_settings_version": -1,
                        "private_channels_version": "0",
                        "api_code_version": 0,
                    },
                },
            },
            metered=False,
        )

    def _subscribe(self, events: set[str] | None = None) -> asyncio.Queue:
        """Queue of dispatches, optionally narrowed to ``events``.

        Filtering here rather than at the consumer matters: a live account
        receives a constant stream of unrelated dispatches, and anything that
        reaches the queue is capable of disturbing the consumer's timing.
        """
        queue: asyncio.Queue = asyncio.Queue()
        self._listeners[queue] = frozenset(events) if events else None
        return queue

    def _unsubscribe(self, queue: asyncio.Queue) -> None:
        self._listeners.pop(queue, None)

    def _check_alive(self) -> None:
        if self._fatal:
            raise self._fatal

    # -- member enumeration ------------------------------------------------

    async def fetch_members(
        self,
        guild_id: str,
        channels: Sequence[Channel],
        expected: int | None = None,
        on_progress: ScrapeProgress | None = None,
        on_members: MemberSink | None = None,
        coverage_target: float = COVERAGE_TARGET,
        max_channels: int = 6,
    ) -> dict[str, GuildMember]:
        """Enumerate members of ``guild_id``, as completely as possible.

        Completeness is the priority, because a member the sidebar never shows
        is a member never checked. Three things serve that:

        * Channels **@everyone can view** are tried first. A sidebar only lists
          members who can see its channel, so a restricted channel silently
          yields a partial list -- exactly the failure that is invisible unless
          you look for it.
        * Results from several channels are **unioned**, not replaced, so
          members visible only through one channel are still picked up.
        * If the union still falls short of the guild's member count, the OP 8
          search sweeps for the rest and is unioned in too.

        ``on_members`` receives each newly-seen member once, as they arrive.
        """
        self._check_alive()

        members: dict[str, GuildMember] = {}
        known: set[str] = set()
        self.last_scrape_channels = []

        def emit() -> None:
            if not on_members:
                known.update(members)
                return
            fresh = [m for mid, m in members.items() if mid not in known]
            if fresh:
                on_members(fresh)
            known.update(members)

        def covered() -> bool:
            return bool(expected) and len(members) >= expected * coverage_target

        open_channels = [c for c in channels if c.everyone_can_view]
        restricted = [c for c in channels if not c.everyone_can_view]
        # everyone-visible first; restricted ones only as a last resort, since
        # each can only ever contribute a subset
        ordered = (open_channels + restricted)[:max_channels]

        if not open_channels and on_progress:
            on_progress(
                0,
                expected,
                "No channel is visible to @everyone - member list may be partial",
            )

        for index, channel in enumerate(ordered, start=1):
            if on_progress:
                on_progress(
                    len(members),
                    expected,
                    f"Opening member list via #{channel.name} "
                    f"({channel.visibility}, channel {index}/{len(ordered)})",
                )
            before = len(members)
            await self._scrape_sidebar(
                guild_id, channel, expected, on_progress, members, emit
            )
            gained = len(members) - before
            if gained:
                self.last_scrape_channels.append(channel.name)
            elif on_progress:
                on_progress(
                    len(members),
                    expected,
                    f"#{channel.name} added nothing, trying the next channel",
                )
            if covered():
                break

        if not covered():
            if on_progress:
                have = f"{len(members):,}"
                want = f" of {expected:,}" if expected else ""
                on_progress(
                    len(members),
                    expected,
                    f"Sidebar gave {have}{want} - searching for the rest (OP 8)",
                )
            await self._request_members(
                guild_id, expected, on_progress, members, emit
            )

        self.last_coverage = (
            min(1.0, len(members) / expected) if expected else None
        )
        return members

    async def _scrape_sidebar(
        self,
        guild_id: str,
        channel: Channel,
        expected: int | None,
        on_progress: ScrapeProgress | None,
        members: dict[str, GuildMember],
        emit: Callable[[], None],
    ) -> dict[str, GuildMember]:
        queue = self._subscribe({"GUILD_MEMBER_LIST_UPDATE"})
        started_with = len(members)
        offset = 0
        total: int | None = expected
        barren_rounds = 0
        first = True

        try:
            while not self._closed:
                self._check_alive()
                ranges = _sidebar_ranges(offset)
                await self._send(
                    {
                        "op": 14,
                        "d": {
                            "guild_id": guild_id,
                            "channels": {channel.id: ranges},
                            "members": [],
                            "activities": True,
                            "typing": True,
                            "threads": False,
                        },
                    }
                )

                before = len(members)
                invalidated = False
                got_event = False

                # Reported before draining, so the bar names the range being
                # waited on rather than the last one that came back.
                if on_progress:
                    if first:
                        note = f"Waiting for #{channel.name} member list"
                    else:
                        note = (
                            f"Reading #{channel.name} members "
                            f"{offset:,}-{offset + RANGE_SIZE * 2 - 1:,}"
                        )
                    on_progress(len(members), total, note)

                # Deadline-based, never per-item: an unrelated dispatch must
                # not be able to extend the wait. Only a member list update for
                # *this* guild pushes the deadline out, and never past the hard
                # cap, so a chatty guild cannot stall the round forever.
                now = asyncio.get_running_loop().time()
                deadline = now + (FIRST_RESPONSE_TIMEOUT if first else SETTLE_TIMEOUT)
                hard_deadline = now + MAX_ROUND_SECONDS

                while True:
                    now = asyncio.get_running_loop().time()
                    remaining = min(deadline, hard_deadline) - now
                    if remaining <= 0:
                        break
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=remaining)
                    except TimeoutError:
                        break
                    if item is None:
                        self._check_alive()
                        break
                    event, data = item
                    if event != "GUILD_MEMBER_LIST_UPDATE":
                        continue
                    if str(data.get("guild_id")) != str(guild_id):
                        continue
                    got_event = True
                    deadline = asyncio.get_running_loop().time() + SETTLE_TIMEOUT
                    count = data.get("member_count")
                    if isinstance(count, int) and count > 0:
                        total = count
                    if _absorb_ops(data.get("ops") or [], members):
                        invalidated = True

                if first and not got_event:
                    # this channel exposes no member list at all
                    return members
                first = False

                gained = len(members) - before
                if gained:
                    emit()
                if on_progress:
                    on_progress(len(members), total, f"Reading #{channel.name} member list")

                if gained == 0:
                    barren_rounds += 1
                else:
                    barren_rounds = 0

                if invalidated and gained == 0:
                    break
                if barren_rounds >= 2:
                    break

                offset += RANGE_SIZE * 2
                if total is not None and offset > total + RANGE_SIZE * 2:
                    break
                if offset > 100_000:  # hard stop, sanity only
                    break

                await asyncio.sleep(0.35)
        finally:
            self._unsubscribe(queue)

        return members

    async def _request_members(
        self,
        guild_id: str,
        expected: int | None,
        on_progress: ScrapeProgress | None,
        members: dict[str, GuildMember],
        emit: Callable[[], None],
    ) -> dict[str, GuildMember]:
        """OP 8 sweep: an open query first, then brute-force prefixes."""
        queue = self._subscribe({"GUILD_MEMBERS_CHUNK"})

        async def run_query(query: str, limit: int) -> int:
            await self._send(
                {
                    "op": 8,
                    "d": {
                        "guild_id": [guild_id],
                        "query": query,
                        "limit": limit,
                        "presences": False,
                    },
                }
            )
            before = len(members)
            # Same deadline discipline as the sidebar drain: only a chunk for
            # this guild may extend the wait, and never past the hard cap.
            now = asyncio.get_running_loop().time()
            deadline = now + CHUNK_TIMEOUT
            hard_deadline = now + MAX_ROUND_SECONDS
            while True:
                now = asyncio.get_running_loop().time()
                remaining = min(deadline, hard_deadline) - now
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=remaining)
                except TimeoutError:
                    break
                if item is None:
                    self._check_alive()
                    break
                event, data = item
                if event != "GUILD_MEMBERS_CHUNK":
                    continue
                if str(data.get("guild_id")) != str(guild_id):
                    continue
                deadline = asyncio.get_running_loop().time() + CHUNK_TIMEOUT
                for raw in data.get("members") or []:
                    member = GuildMember.parse(raw)
                    if member:
                        members[member.id] = member
                index = data.get("chunk_index")
                count = data.get("chunk_count")
                if index is None or count is None or index >= count - 1:
                    break
            gained = len(members) - before
            if gained:
                emit()
            return gained

        try:
            # With MANAGE_GUILD/elevated permissions this returns everyone.
            if on_progress:
                on_progress(0, expected, "Requesting all members (OP 8 open query)")
            await run_query("", 0)

            if expected is None or len(members) < expected * 0.9:
                alphabet = string.ascii_lowercase + string.digits + "_."
                for i, prefix in enumerate(alphabet):
                    self._check_alive()
                    if on_progress:
                        on_progress(
                            len(members),
                            expected,
                            f"Searching members by prefix '{prefix}' "
                            f"({i + 1}/{len(alphabet)})",
                        )
                    await run_query(prefix, 100)
                    if expected and len(members) >= expected:
                        break  # whole guild accounted for, no need to sweep on
                    await asyncio.sleep(PREFIX_QUERY_DELAY)
        finally:
            self._unsubscribe(queue)

        return members


def _sidebar_ranges(offset: int) -> list[list[int]]:
    """Ranges for one OP 14. ``[0,99]`` is always included, as the client does."""
    head = [0, RANGE_SIZE - 1]
    if offset == 0:
        return [head]
    return [
        head,
        [offset, offset + RANGE_SIZE - 1],
        [offset + RANGE_SIZE, offset + RANGE_SIZE * 2 - 1],
    ]


def _absorb_ops(ops: list[dict], members: dict[str, GuildMember]) -> bool:
    """Fold ``GUILD_MEMBER_LIST_UPDATE`` ops into ``members``.

    Returns True if any op was an INVALIDATE, which means we asked for a range
    beyond the end of the list.
    """
    invalidated = False
    for op in ops:
        kind = op.get("op")
        if kind == "INVALIDATE":
            invalidated = True
            continue
        if kind == "SYNC":
            items = op.get("items") or []
        elif kind in ("INSERT", "UPDATE"):
            items = [op.get("item")] if op.get("item") else []
        else:  # DELETE and anything unknown carry no member payload
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            raw = item.get("member")
            if not raw:  # {"group": {...}} role dividers
                continue
            member = GuildMember.parse(raw)
            if member:
                members[member.id] = member
    return invalidated
