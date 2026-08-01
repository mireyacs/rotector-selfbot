"""Regression: unrelated gateway traffic must not stall a member scrape.

A live account receives a constant stream of dispatches (presence updates,
messages, typing) from *every* server it is in.  The drain loop originally used
a per-item timeout, so each of those unrelated events reset the clock and the
scrape hung forever on "Waiting for #channel member list" -- regardless of how
small the target server was.

These tests drive the real scrape logic against a gateway that is deliberately
noisy.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rsb.discord.gateway as gw
from rsb.discord.http import Channel

ok = lambda m: print(f"[ok] {m}")

GUILD = "111"
CHANNELS = [Channel(id="c1", name="general", type=0, position=0, everyone_can_view=True)]


def member(i):
    return {"member": {"user": {"id": str(1000 + i), "username": f"user{i}"},
                       "roles": [], "joined_at": "2024-01-01T00:00:00+00:00"}}


class FakeWS:
    """Answers OP 14 with a member list; ignores everything else."""

    def __init__(self, gateway, member_count, respond=True):
        self.g = gateway
        self.member_count = member_count
        self.respond = respond
        self.op14_sent = 0

    async def send(self, raw):
        payload = json.loads(raw)
        if payload.get("op") != 14 or not self.respond:
            return
        self.op14_sent += 1
        ranges = next(iter(payload["d"]["channels"].values()))
        ops = []
        for start, end in ranges:
            if start >= self.member_count:
                ops.append({"op": "INVALIDATE", "range": [start, end]})
                continue
            items = [{"group": {"id": "online", "count": self.member_count}}]
            items += [member(i) for i in range(start, min(end + 1, self.member_count))]
            ops.append({"op": "SYNC", "range": [start, end], "items": items})
        asyncio.get_running_loop().call_later(
            0.05,
            lambda: asyncio.ensure_future(
                self.g._dispatch("GUILD_MEMBER_LIST_UPDATE", {
                    "guild_id": GUILD, "id": "everyone",
                    "member_count": self.member_count,
                    "online_count": self.member_count, "groups": [], "ops": ops,
                })
            ),
        )

    async def close(self):
        pass


async def flood(gateway, stop, period=0.05):
    """Unrelated traffic, far faster than any of the scrape's timeouts."""
    n = 0
    while not stop.is_set():
        await gateway._dispatch("PRESENCE_UPDATE",
                                {"guild_id": "999", "user": {"id": "42"}})
        await gateway._dispatch("MESSAGE_CREATE",
                                {"guild_id": "999", "id": str(n)})
        await gateway._dispatch("TYPING_START", {"guild_id": "888"})
        n += 3
        await asyncio.sleep(period)
    return n


async def run_scrape(member_count, respond, budget):
    gw.FIRST_RESPONSE_TIMEOUT = 1.0
    gw.SETTLE_TIMEOUT = 0.3
    gw.MAX_ROUND_SECONDS = 3.0
    gw.CHUNK_TIMEOUT = 0.2
    gw.PREFIX_QUERY_DELAY = 0.05

    g = gw.DiscordGateway("fake")
    g._ws = FakeWS(g, member_count, respond=respond)
    stop = asyncio.Event()
    noise = asyncio.create_task(flood(g, stop))

    t0 = time.monotonic()
    try:
        members = await asyncio.wait_for(
            g.fetch_members(GUILD, CHANNELS, expected=member_count),
            timeout=budget,
        )
    except TimeoutError:
        raise AssertionError(
            f"scrape did not finish within {budget}s under load -- this is the hang"
        ) from None
    finally:
        stop.set()
        sent = await noise
        op14 = g._ws.op14_sent          # close() clears _ws, so read it first
        await g.close()
    return members, time.monotonic() - t0, sent, op14


async def test_small_guild_under_noise():
    """The reported case: a 9-member server, a chatty account."""
    members, elapsed, noise_events, op14 = await run_scrape(9, True, budget=20)

    assert len(members) == 9, f"got {len(members)} of 9 members"
    ok(f"9-member guild scraped in {elapsed:.2f}s while {noise_events} unrelated "
       f"events streamed in ({op14} OP 14 rounds)")
    assert elapsed < 8, f"took {elapsed:.1f}s, far longer than the work required"
    ok(f"finished promptly ({elapsed:.2f}s) instead of hanging")
    assert sorted(m.username for m in members.values())[:3] == ["user0", "user1", "user2"]
    ok("member payloads parsed correctly")


async def test_silent_channel_under_noise_still_gives_up():
    """A channel that never answers must still time out, noise or not."""
    members, elapsed, noise_events, _ = await run_scrape(9, False, budget=45)

    assert members == {}, members
    # one channel at FIRST_RESPONSE_TIMEOUT, then the full OP 8 sweep
    assert elapsed < 25, f"took {elapsed:.1f}s"
    ok(f"silent channel gave up after {elapsed:.2f}s despite {noise_events} "
       f"unrelated events (never-ending before the fix)")


async def test_filtered_subscription():
    """Irrelevant dispatches must not even reach a scraper's queue."""
    g = gw.DiscordGateway("fake")
    q = g._subscribe({"GUILD_MEMBER_LIST_UPDATE"})
    await g._dispatch("PRESENCE_UPDATE", {"guild_id": "1"})
    await g._dispatch("MESSAGE_CREATE", {"guild_id": "1"})
    assert q.qsize() == 0, f"{q.qsize()} irrelevant events leaked into the queue"
    await g._dispatch("GUILD_MEMBER_LIST_UPDATE", {"guild_id": "1"})
    assert q.qsize() == 1
    ok("filtered subscription drops unrelated dispatches at the source")

    # an unfiltered listener still sees everything
    q2 = g._subscribe()
    await g._dispatch("PRESENCE_UPDATE", {"guild_id": "1"})
    assert q2.qsize() == 1
    ok("unfiltered listeners are unaffected")

    # failures wake every listener regardless of filter
    g._fail(gw.GatewayError("boom"))
    drained = []
    while not q.empty():
        drained.append(q.get_nowait())
    assert None in drained, "filtered queue never received the failure sentinel"
    ok("failure sentinel bypasses the filter and wakes filtered listeners")


async def main():
    await test_small_guild_under_noise()
    print()
    await test_silent_channel_under_noise_still_gives_up()
    print()
    await test_filtered_subscription()
    print("\nALL GATEWAY SCRAPE TESTS PASSED")


asyncio.run(main())
