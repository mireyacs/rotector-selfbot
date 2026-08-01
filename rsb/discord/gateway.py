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
from collections import deque
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
#: Extra ranges requested alongside [0,99] in one subscription.
#: Discord accepts at most three ranges per channel -- [0,99] plus two more.
#: Sending a fourth is rejected outright and closes the socket with 4002.
RANGES_PER_REQUEST = 2
#: channels subscribed to at once; each carries its own slice of the list.
#: This, not more ranges, is how a large list is read quickly.
CHANNELS_PER_REQUEST = 5
#: absolute cap on one request/drain round, however chatty the guild is
MAX_ROUND_SECONDS = 20.0
#: fraction of the guild's member count treated as full coverage
COVERAGE_TARGET = 0.995
#: pause between OP 8 prefix queries, to stay clear of the gateway event budget
PREFIX_QUERY_DELAY = 0.5
#: Discord's per-query result cap. A query returning exactly this is truncated.
QUERY_LIMIT = 100
#: characters prefixes are extended with when a query saturates
PREFIX_ALPHABET = string.ascii_lowercase + string.digits + "_."
#: how many characters deep the prefix search may go
PREFIX_MAX_DEPTH = 2
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

    async def watch_messages(
        self,
        on_author: Callable[[GuildMember], None],
        should_stop: Callable[[], bool],
        own_id: str | None = None,
        include_bots: bool = False,
        poll: float = 0.5,
    ) -> int:
        """Report the author of every incoming message, once each.

        Runs until ``should_stop`` says otherwise. Only the *sender* is
        reported -- message content is never read, stored or forwarded
        anywhere; the point is to know who is talking to you, not what was
        said.
        """
        queue = self._subscribe({"MESSAGE_CREATE"})
        seen: set[str] = set()
        count = 0
        try:
            while not should_stop() and not self._closed:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=poll)
                except TimeoutError:
                    continue
                if item is None:
                    self._check_alive()
                    break
                _event, data = item
                author = data.get("author") or {}
                uid = author.get("id")
                if not uid or str(uid) in seen:
                    continue
                if own_id and str(uid) == str(own_id):
                    continue
                if author.get("bot") and not include_bots:
                    continue
                seen.add(str(uid))
                member = GuildMember(
                    id=str(uid),
                    username=author.get("username") or "unknown",
                    global_name=author.get("global_name"),
                    discriminator=str(author.get("discriminator") or "0"),
                    bot=bool(author.get("bot")),
                )
                count += 1
                on_author(member)
        finally:
            self._unsubscribe(queue)
        return count

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
        can_chunk: bool = False,
    ) -> dict[str, GuildMember]:
        """Enumerate members of ``guild_id``, as completely as possible.

        Completeness is the priority, because a member the sidebar never shows
        is a member never checked. Three things serve that:

        * If the account holds **kick, ban, manage-roles or administrator** in
          this guild, Discord will hand over the whole member list -- offline
          members included -- for a single request. That is tried first, and
          when it works nothing else is needed.
        * Channels **@everyone can view** are tried first otherwise. A sidebar
          only lists members who can see its channel, so a restricted channel
          silently yields a partial list -- exactly the failure that is
          invisible unless you look for it.
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

        # The privileged path: one request, everyone, offline included.
        if can_chunk:
            if on_progress:
                on_progress(
                    0,
                    expected,
                    "Requesting the full member list (permitted for this account)",
                )
            await self._request_members(
                guild_id, expected, on_progress, members, emit,
                coverage_target=coverage_target, open_query_only=True,
            )
            if covered() or members:
                self.last_coverage = (
                    min(1.0, len(members) / expected) if expected else None
                )
                self.last_scrape_channels = ["full member list"]
                if members and on_progress:
                    on_progress(
                        len(members), expected,
                        f"Received the full member list ({len(members):,})",
                    )
                if covered():
                    return members

        open_channels = [c for c in channels if c.everyone_can_view]
        restricted = [c for c in channels if not c.everyone_can_view]
        # everyone-visible first; restricted ones only as a last resort, since
        # each can only ever contribute a subset


        if not open_channels and on_progress:
            on_progress(
                0,
                expected,
                "No channel is visible to @everyone - member list may be partial",
            )

        # Try the everyone-visible channels together first; if they yield
        # nothing, fall back to trying restricted ones one at a time.
        attempts: list[list[Channel]] = []
        if open_channels:
            attempts.append(open_channels[:CHANNELS_PER_REQUEST])
        attempts += [[c] for c in restricted[:max_channels]]

        for index, group in enumerate(attempts, start=1):
            if not group:
                continue
            label = "/".join(f"#{c.name}" for c in group[:2])
            if len(group) > 2:
                label += f" +{len(group) - 2}"
            if on_progress:
                on_progress(
                    len(members),
                    expected,
                    f"Opening member list via {label} "
                    f"({group[0].visibility}, attempt {index}/{len(attempts)})",
                )
            before = len(members)
            await self._scrape_sidebar(
                guild_id, group, expected, on_progress, members, emit
            )
            if len(members) > before:
                self.last_scrape_channels += [c.name for c in group]
            elif on_progress:
                on_progress(
                    len(members), expected, f"{label} added nothing, trying elsewhere"
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
        channels: Sequence[Channel],
        expected: int | None,
        on_progress: ScrapeProgress | None,
        members: dict[str, GuildMember],
        emit: Callable[[], None],
    ) -> dict[str, GuildMember]:
        """Read the member sidebar, several channels at a time.

        Discord allows at most three ranges per channel in one OP 14 -- the
        mandatory [0,99] plus two more. Speed therefore comes from subscribing
        to several channels in the *same* request, each covering a different
        window, which is what the real client does. Every everyone-visible
        channel exposes the same list, so their results simply merge.
        """
        queue = self._subscribe({"GUILD_MEMBER_LIST_UPDATE"})
        targets = list(channels)[:CHANNELS_PER_REQUEST]
        names = "/".join(f"#{c.name}" for c in targets[:2])
        if len(targets) > 2:
            names += f" +{len(targets) - 2}"

        windows: deque[tuple[int, int]] = deque()
        next_start = 0
        list_size: int | None = None
        total = expected
        barren = 0
        first = True

        def refill() -> None:
            nonlocal next_start
            if list_size is None:
                return
            while next_start < list_size:
                windows.append((next_start, next_start + RANGE_SIZE - 1))
                next_start += RANGE_SIZE

        try:
            while not self._closed:
                self._check_alive()

                requests: dict[str, list[list[int]]] = {}
                head = [0, RANGE_SIZE - 1]
                if first:
                    # one probe, to learn how long the list actually is
                    requests[targets[0].id] = [head]
                else:
                    for channel in targets:
                        picked = []
                        for _ in range(RANGES_PER_REQUEST):
                            if not windows:
                                break
                            lo, hi = windows.popleft()
                            picked.append([lo, hi])
                        if picked:
                            requests[channel.id] = [head, *picked]
                    if not requests:
                        break

                await self._send(
                    {
                        "op": 14,
                        "d": {
                            "guild_id": guild_id,
                            "channels": requests,
                            "members": [],
                            "activities": True,
                            "typing": True,
                            "threads": False,
                        },
                    }
                )

                before = len(members)
                got_event = False
                timeout = FIRST_RESPONSE_TIMEOUT if first else SETTLE_TIMEOUT

                if on_progress:
                    if first:
                        note = f"Waiting for {names} member list"
                    else:
                        spans = [r for rs in requests.values() for r in rs[1:]]
                        lo = min(r[0] for r in spans)
                        hi = max(r[1] for r in spans)
                        of = f" of {list_size:,}" if list_size else ""
                        note = (
                            f"Reading {names} members {lo:,}-{hi:,}{of} "
                            f"({len(requests)} channels)"
                        )
                    on_progress(len(members), total, note)

                now = asyncio.get_running_loop().time()
                deadline = now + timeout
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
                    groups = data.get("groups")
                    if isinstance(groups, list) and groups:
                        rows = sum(
                            g.get("count", 0) for g in groups if isinstance(g, dict)
                        )
                        if rows:
                            list_size = rows + len(groups)
                    _absorb_ops(data.get("ops") or [], members)

                if first and not got_event:
                    return members  # this channel exposes no member list
                if first:
                    first = False
                    refill()
                    if not windows:
                        break
                    continue

                refill()
                gained = len(members) - before
                if gained:
                    emit()
                    barren = 0
                else:
                    barren += 1
                if on_progress:
                    on_progress(len(members), total, f"Reading {names} member list")
                if barren >= 2:
                    break
                await asyncio.sleep(0.2)
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
        coverage_target: float = COVERAGE_TARGET,
        max_depth: int = PREFIX_MAX_DEPTH,
        open_query_only: bool = False,
    ) -> dict[str, GuildMember]:
        """OP 8 sweep: an open query, then adaptively deepened prefix search.

        A flat pass over single-character prefixes cannot fill a large guild:
        Discord returns at most :data:`QUERY_LIMIT` matches per query, so 38
        prefixes can surface 3,800 members at the very most however many the
        guild actually has.

        So a prefix that comes back *saturated* -- exactly the limit -- is
        understood to be hiding more, and is re-queried one character deeper
        ("a" -> "aa", "ab", ...). Prefixes that are not saturated are left
        alone, so the extra queries are spent only where members are actually
        being missed.
        """
        queue = self._subscribe({"GUILD_MEMBERS_CHUNK"})
        queries = [0]

        def covered() -> bool:
            return bool(expected) and len(members) >= expected * coverage_target

        async def run_query(query: str, limit: int = QUERY_LIMIT) -> int:
            """Send one query; return how many members it *returned*.

            Deliberately the returned count rather than the newly-added count:
            saturation is a property of the query, and a prefix can return a
            full hundred that we have all seen before while still hiding more.
            """
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
            queries[0] += 1
            before = len(members)
            returned = 0
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
                    returned += 1
                    member = GuildMember.parse(raw)
                    if member:
                        members[member.id] = member
                index = data.get("chunk_index")
                count = data.get("chunk_count")
                if index is None or count is None or index >= count - 1:
                    break
            if len(members) > before:
                emit()
            return returned

        async def sweep(prefix: str, depth: int) -> None:
            if covered():
                return
            self._check_alive()
            if on_progress:
                have = f"{len(members):,}"
                want = f" of {expected:,}" if expected else ""
                on_progress(
                    len(members),
                    expected,
                    f"Searching members by name '{prefix}' "
                    f"(query {queries[0] + 1}, {have}{want} found)",
                )
            returned = await run_query(prefix)
            await asyncio.sleep(PREFIX_QUERY_DELAY)
            # a full page back means the query was truncated, not exhausted
            if returned >= QUERY_LIMIT and depth < max_depth:
                for char in PREFIX_ALPHABET:
                    if covered():
                        return
                    await sweep(prefix + char, depth + 1)

        try:
            # With elevated permissions an empty query returns everyone at once.
            if on_progress:
                on_progress(
                    len(members), expected, "Requesting all members (OP 8 open query)"
                )
            await run_query("", 0)

            if not covered() and not open_query_only:
                for char in PREFIX_ALPHABET:
                    if covered():
                        break
                    await sweep(char, 1)
        finally:
            self._unsubscribe(queue)

        if on_progress:
            on_progress(
                len(members),
                expected,
                f"Name search finished after {queries[0]:,} queries",
            )
        return members


def _sidebar_ranges(offset: int, count: int = RANGES_PER_REQUEST) -> list[list[int]]:
    """Ranges for one OP 14 subscription.

    ``[0,99]`` is always included, as the real client does. Beyond that the
    client accepts several ranges per request, so asking for a few consecutive
    windows at once is what makes a large list finish in minutes rather than
    tens of minutes.
    """
    head = [0, RANGE_SIZE - 1]
    if offset == 0:
        return [head]
    ranges = [head]
    for index in range(count):
        start = offset + index * RANGE_SIZE
        ranges.append([start, start + RANGE_SIZE - 1])
    return ranges


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
