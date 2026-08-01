"""How members are fetched, and why a large guild can come back short.

Three paths, in the order the gateway tries them:

1. With kick/ban/manage-roles, one OP 8 request returns the whole guild,
   offline members included.
2. Otherwise the member sidebar, which in a large guild contains only
   non-offline members -- a Discord limit, not one of ours.
3. Then a name search to recover what the sidebar could not show.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rsb.discord.gateway as gw
from rsb.discord.http import (
    ADMINISTRATOR,
    BAN_MEMBERS,
    KICK_MEMBERS,
    MANAGE_ROLES,
    VIEW_CHANNEL,
    Channel,
    Guild,
)

ok = lambda m: print(f"[ok] {m}")

GUILD = "111"
CHANNELS = [
    Channel(id=f"c{i}", name=f"chan{i}", type=0, position=i, everyone_can_view=True)
    for i in range(3)
]


def member(uid, name):
    return {"user": {"id": str(uid), "username": name}, "roles": [],
            "joined_at": "2024-01-01T00:00:00+00:00"}


class FakeWS:
    """A guild of `total` members, `online` of whom appear in the sidebar."""

    def __init__(self, gateway, total, online, allow_chunk):
        self.g = gateway
        self.total = total
        self.online = online
        self.allow_chunk = allow_chunk
        self.op14 = 0
        self.op8 = 0
        self.queries = []
        self.max_ranges = 0
        self.max_channels = 0

    async def send(self, raw):
        payload = json.loads(raw)
        op = payload.get("op")
        if op == 14:
            self.op14 += 1
            # Discord closes the socket with 4002 if a channel is given more
            # than three ranges. Enforce it here so a regression is a test
            # failure rather than a dead connection mid-scan.
            for channel_id, ranges in payload["d"]["channels"].items():
                if len(ranges) > 3:
                    raise AssertionError(
                        f"{len(ranges)} ranges for channel {channel_id}; "
                        f"Discord accepts at most 3 and closes with 4002"
                    )
                self.max_ranges = max(self.max_ranges, len(ranges))
            self.max_channels = max(self.max_channels, len(payload["d"]["channels"]))
            self._sidebar(payload["d"])
        elif op == 8:
            self.op8 += 1
            self._search(payload["d"])

    def _dispatch(self, event, data):
        asyncio.get_running_loop().call_later(
            0.01, lambda: asyncio.ensure_future(self.g._dispatch(event, data))
        )

    def _sidebar(self, d):
        # the list only ever contains the online members, plus one group header
        ranges = [r for rs in d["channels"].values() for r in rs]
        ops = []
        for start, end in ranges:
            if start >= self.online:
                ops.append({"op": "INVALIDATE", "range": [start, end]})
                continue
            items = [{"group": {"id": "online", "count": self.online}}] if start == 0 else []
            for i in range(start, min(end + 1, self.online)):
                # sidebar items are wrapped; OP 8 chunks are not
                items.append({"member": member(i, f"online{i:04d}")})
            ops.append({"op": "SYNC", "range": [start, end], "items": items})
        self._dispatch("GUILD_MEMBER_LIST_UPDATE", {
            "guild_id": GUILD, "id": "everyone",
            "member_count": self.total, "online_count": self.online,
            "groups": [{"id": "online", "count": self.online}], "ops": ops,
        })

    def _search(self, d):
        query = d.get("query", "")
        self.queries.append(query)
        if query == "" and d.get("limit") == 0:
            if not self.allow_chunk:
                return  # Discord simply does not answer without permission
            everyone = [member(i, f"online{i:04d}") for i in range(self.online)]
            everyone += [
                member(10_000 + i, f"offline{i:04d}")
                for i in range(self.total - self.online)
            ]
            for index in range(0, len(everyone), 1000):
                self._dispatch("GUILD_MEMBERS_CHUNK", {
                    "guild_id": GUILD,
                    "members": everyone[index : index + 1000],
                    "chunk_index": index // 1000,
                    "chunk_count": (len(everyone) + 999) // 1000,
                })
            return
        # a name query returns at most QUERY_LIMIT matches
        pool = [f"offline{i:04d}" for i in range(self.total - self.online)]
        hits = [n for n in pool if n.startswith(query)][: gw.QUERY_LIMIT]
        self._dispatch("GUILD_MEMBERS_CHUNK", {
            "guild_id": GUILD,
            "members": [member(10_000 + int(n[7:]), n) for n in hits],
            "chunk_index": 0, "chunk_count": 1,
        })

    async def close(self):
        pass


async def run(total, online, allow_chunk, can_chunk):
    gw.FIRST_RESPONSE_TIMEOUT = 0.6
    gw.SETTLE_TIMEOUT = 0.2
    gw.CHUNK_TIMEOUT = 0.25
    gw.PREFIX_QUERY_DELAY = 0.0
    gw.MAX_ROUND_SECONDS = 3.0

    g = gw.DiscordGateway("fake")
    ws = FakeWS(g, total, online, allow_chunk)
    g._ws = ws
    started = time.monotonic()
    members = await g.fetch_members(
        GUILD, CHANNELS, expected=total, can_chunk=can_chunk
    )
    elapsed = time.monotonic() - started
    await g.close()
    return members, ws, elapsed


def test_permissions():
    def guild(perms):
        return Guild(id="1", name="g", owner=False, permissions=perms,
                     member_count=10833, presence_count=2140)

    assert not guild(0).can_chunk
    assert not guild(VIEW_CHANNEL).can_chunk
    for perm, label in [(KICK_MEMBERS, "kick"), (BAN_MEMBERS, "ban"),
                        (MANAGE_ROLES, "manage roles"), (ADMINISTRATOR, "admin")]:
        assert guild(perm).can_chunk, label
    ok("kick / ban / manage-roles / administrator each unlock the full list")

    assert guild(0).offline_members_hidden
    small = Guild(id="1", name="g", owner=False, permissions=0,
                  member_count=400, presence_count=90)
    assert not small.offline_members_hidden
    ok("offline members are only hidden once the guild is large")


async def test_privileged_path_gets_everyone():
    members, ws, _ = await run(total=5000, online=800,
                               allow_chunk=True, can_chunk=True)
    assert len(members) == 5000, len(members)
    ok(f"with permission: all {len(members):,} members, offline included")
    assert ws.op14 == 0, f"{ws.op14} sidebar requests despite the full list"
    ok("the sidebar is not touched at all -- one request does it")
    assert any(m.username.startswith("offline") for m in members.values())
    ok("offline members really are present")


async def test_unprivileged_is_short_and_honest():
    members, ws, _ = await run(total=5000, online=800,
                               allow_chunk=False, can_chunk=False)
    assert ws.op14 > 0, "never tried the sidebar"
    online = [m for m in members.values() if m.username.startswith("online")]
    assert len(online) == 800, len(online)
    ok(f"without permission: the sidebar yields all {len(online)} online members")

    recovered = [m for m in members.values() if m.username.startswith("offline")]
    assert recovered, "the name search recovered nothing"
    ok(f"the name search then recovers {len(recovered):,} offline members")

    assert len(members) < 5000
    ok(f"still short of the guild ({len(members):,} of 5,000) - a Discord limit, "
       f"reported rather than hidden")

    deepened = [q for q in ws.queries if len(q) > 1]
    assert deepened, "never deepened a saturated prefix"
    ok(f"saturated prefixes were deepened ({len(deepened)} two-character queries)")


async def test_sidebar_stops_at_the_list_end():
    """The scroll must stop at the list length, not the member count."""
    members, ws, _ = await run(total=10833, online=600,
                               allow_chunk=False, can_chunk=False)
    online = [m for m in members.values() if m.username.startswith("online")]
    assert len(online) == 600, len(online)

    # 600 online at 300/round is a handful of rounds. Scrolling to the member
    # count instead would be ~36 -- which is what made a large scan crawl.
    naive = 10833 // (gw.RANGE_SIZE * gw.RANGES_PER_REQUEST)
    assert ws.op14 < naive / 2, (ws.op14, naive)
    ok(f"stopped after {ws.op14} sidebar requests, not the ~{naive} a "
       f"member-count ceiling would have cost")


async def test_protocol_limits_respected():
    """Never more than three ranges per channel, and several channels at once."""
    _members, ws, _ = await run(total=5000, online=1200,
                                allow_chunk=False, can_chunk=False)
    assert ws.max_ranges <= 3, ws.max_ranges
    ok(f"never sent more than {ws.max_ranges} ranges to one channel (limit 3)")

    assert ws.max_channels > 1, ws.max_channels
    ok(f"subscribed to {ws.max_channels} channels in a single request, which is "
       f"where the speed comes from")

    per_round = ws.max_channels * gw.RANGES_PER_REQUEST * gw.RANGE_SIZE
    ok(f"up to {per_round:,} members per round within the protocol limits")


async def main():
    test_permissions()
    print()
    await test_privileged_path_gets_everyone()
    print()
    await test_unprivileged_is_short_and_honest()
    print()
    await test_sidebar_stops_at_the_list_end()
    await test_protocol_limits_respected()
    print("\nALL MEMBER COVERAGE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
