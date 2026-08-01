"""Purging your own messages: planning, execution, and the confirmation gate."""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rsb.purge as purge_mod
from rsb.purge import (
    KIND_DM,
    KIND_GROUP,
    PurgeTarget,
    execute_purge,
    plan_purge,
)

ok = lambda m: print(f"[ok] {m}")

ME = "9"
THEM = "1"


def message(index, author, days_ago=0, content="hello"):
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {
        "id": str(100000 - index),          # descending, like Discord returns
        "author": {"id": author},
        "timestamp": when.isoformat(),
        "content": content,
    }


class FakeHTTP:
    """A DM history, paginated newest-first the way Discord does."""

    def __init__(self, messages, fail_ids=()):
        self.messages = messages
        self.deleted = []
        self.fail_ids = set(fail_ids)
        self.pages_served = 0

    async def channel_messages(self, channel_id, limit=100, before=None):
        self.pages_served += 1
        pool = [m for m in self.messages if m["id"] not in self.deleted]
        if before:
            start = next(
                (i for i, m in enumerate(pool) if m["id"] == before), len(pool)
            )
            pool = pool[start + 1:]
        return pool[:limit]

    async def delete_message(self, channel_id, message_id):
        if message_id in self.fail_ids:
            raise RuntimeError("cannot delete")
        self.deleted.append(message_id)


TARGET = PurgeTarget(channel_id="c1", label="DM with Flagged", kind=KIND_DM)


async def test_plan_only_collects_own_messages():
    history = [message(i, ME if i % 2 == 0 else THEM) for i in range(40)]
    http = FakeHTTP(history)

    plan = await plan_purge(http, TARGET, own_id=ME)

    assert plan.scanned == 40, plan.scanned
    assert plan.count == 20, plan.count
    assert all(m.id in {h["id"] for h in history if h["author"]["id"] == ME}
               for m in plan.messages)
    ok(f"scanned {plan.scanned}, planned {plan.count} - only our own messages")

    assert not http.deleted, "planning deleted something"
    ok("planning deletes nothing")
    ok(f"describe(): {plan.describe()}")


async def test_plan_paginates():
    purge_mod.PAGE_DELAY = 0.0
    history = [message(i, ME) for i in range(250)]
    http = FakeHTTP(history)

    plan = await plan_purge(http, TARGET, own_id=ME)
    assert plan.count == 250, plan.count
    assert plan.pages == 3, plan.pages
    assert plan.reached_end
    ok(f"walked {plan.pages} pages to collect all {plan.count} messages")


async def test_plan_limits():
    purge_mod.PAGE_DELAY = 0.0
    history = [message(i, ME) for i in range(250)]

    plan = await plan_purge(FakeHTTP(history), TARGET, own_id=ME, max_messages=40)
    assert plan.count == 40 and not plan.reached_end
    ok(f"max_messages caps the plan at {plan.count} and flags it as truncated")

    aged = [message(i, ME, days_ago=i) for i in range(60)]
    plan = await plan_purge(FakeHTTP(aged), TARGET, own_id=ME, max_age_days=10)
    assert 0 < plan.count <= 11, plan.count
    ok(f"max_age_days=10 stops the walk at {plan.count} recent messages")


async def test_execute_deletes_oldest_first():
    purge_mod.PAGE_DELAY = 0.0
    history = [message(i, ME, days_ago=i) for i in range(5)]
    http = FakeHTTP(history)
    plan = await plan_purge(http, TARGET, own_id=ME)

    result = await execute_purge(http, plan, delete_delay=0.0)
    assert result.deleted == 5 and result.failed == 0
    ok(f"deleted all {result.deleted} planned messages")

    order = [m.timestamp for m in sorted(plan.messages, key=lambda m: m.timestamp)]
    expected = [
        next(m.id for m in plan.messages if m.timestamp == stamp) for stamp in order
    ]
    assert http.deleted == expected, (http.deleted, expected)
    ok("deleted oldest first, so an interrupted purge leaves the recent tail")


async def test_execute_survives_failures():
    purge_mod.PAGE_DELAY = 0.0
    history = [message(i, ME) for i in range(6)]
    doomed = {history[2]["id"], history[4]["id"]}
    http = FakeHTTP(history, fail_ids=doomed)
    plan = await plan_purge(http, TARGET, own_id=ME)

    result = await execute_purge(http, plan, delete_delay=0.0)
    assert result.deleted == 4 and result.failed == 2, result
    assert len(http.deleted) == 4
    assert result.errors and all(":" in e for e in result.errors)
    ok(f"one failure does not abort the rest: {result.describe()}")


async def test_execute_can_be_stopped():
    purge_mod.PAGE_DELAY = 0.0
    history = [message(i, ME) for i in range(20)]
    http = FakeHTTP(history)
    plan = await plan_purge(http, TARGET, own_id=ME)

    seen = 0

    def should_stop():
        return seen >= 5

    def progress(stage, done, total):
        nonlocal seen
        seen = done

    result = await execute_purge(
        http, plan, delete_delay=0.0, on_progress=progress, should_stop=should_stop
    )
    assert result.stopped and result.deleted == 5, result
    assert len(http.deleted) == 5
    ok(f"stopping mid-purge halts after {result.deleted} of {plan.count}")


async def test_empty_history():
    http = FakeHTTP([message(i, THEM) for i in range(10)])
    plan = await plan_purge(http, TARGET, own_id=ME)
    assert plan.count == 0 and plan.scanned == 10
    assert "No messages of yours" in plan.describe()
    ok(f"a conversation with none of our messages: {plan.describe()}")

    result = await execute_purge(http, plan, delete_delay=0.0)
    assert result.deleted == 0 and not http.deleted
    ok("executing an empty plan is a no-op")


async def test_group_target_is_labelled():
    group = PurgeTarget(channel_id="g1", label="group Squad", kind=KIND_GROUP)
    assert group.is_group and not TARGET.is_group
    ok("group targets are distinguishable, so the dialog can warn about scope")


async def test_dm_lookup_never_opens_a_dm():
    """find_dm_channel must not create a conversation that did not exist."""
    from rsb.discord.http import DM_CHANNEL, GROUP_DM_CHANNEL, DiscordHTTP, PrivateChannel

    class Probe(DiscordHTTP):
        def __init__(self):
            self.requests = []
            self._channels = [
                PrivateChannel(id="dm1", type=DM_CHANNEL, name=None, owner_id=None,
                               recipients=[{"id": "1"}]),
                PrivateChannel(id="g1", type=GROUP_DM_CHANNEL, name="Squad",
                               owner_id="9", recipients=[{"id": "1"}, {"id": "2"}]),
            ]

        async def private_channels(self):
            return self._channels

        async def _request(self, method, path, **kw):
            self.requests.append((method, path))
            raise AssertionError(f"unexpected call: {method} {path}")

    probe = Probe()
    assert await probe.find_dm_channel("1") == "dm1"
    ok("finds the existing 1:1 DM, and does not mistake the group for one")

    assert await probe.find_dm_channel("404") is None
    assert not probe.requests, probe.requests
    ok("a user with no DM returns None without opening one")


async def main():
    await test_plan_only_collects_own_messages()
    print()
    await test_plan_paginates()
    await test_plan_limits()
    print()
    await test_execute_deletes_oldest_first()
    await test_execute_survives_failures()
    await test_execute_can_be_stopped()
    print()
    await test_empty_history()
    await test_group_target_is_labelled()
    await test_dm_lookup_never_opens_a_dm()
    print("\nALL PURGE TESTS PASSED")


asyncio.run(main())
