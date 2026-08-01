"""Unit tests for the parts that can't be exercised without a live token."""
import asyncio, sys, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from rsb.discord.gateway import _absorb_ops, _sidebar_ranges, GuildMember
from rsb.discord.http import _everyone_can_view, VIEW_CHANNEL
from rsb.ratelimit import RateLimiter, batch_cost
from rsb.rotector import MemberReport, RobloxAccount, TrackedServer
from rsb.verdict import Verdict, flag_is_actionable, verdict_for_flag

ok = lambda m: print(f"[ok] {m}")

# --- sidebar ranges -------------------------------------------------------
assert _sidebar_ranges(0) == [[0, 99]]
assert _sidebar_ranges(200) == [[0, 99], [200, 299], [300, 399]]
ok(f"sidebar ranges: {_sidebar_ranges(0)} then {_sidebar_ranges(200)}")

# --- GUILD_MEMBER_LIST_UPDATE op folding ----------------------------------
members = {}
inv = _absorb_ops([
    {"op": "SYNC", "range": [0, 3], "items": [
        {"group": {"id": "online", "count": 2}},
        {"member": {"user": {"id": "10", "username": "alice", "global_name": "Alice"}, "nick": "Al"}},
        {"member": {"user": {"id": "11", "username": "bot", "bot": True}}},
    ]},
    {"op": "INSERT", "index": 1, "item": {"member": {"user": {"id": "12", "username": "carol"}}}},
    {"op": "UPDATE", "index": 0, "item": {"member": {"user": {"id": "10", "username": "alice2"}}}},
    {"op": "DELETE", "index": 5},
], members)
assert not inv and set(members) == {"10", "11", "12"}, members
assert members["10"].username == "alice2", "UPDATE should overwrite"
assert members["11"].bot is True
assert members["12"].display_name == "carol"
ok(f"op folding: {len(members)} members, groups skipped, UPDATE overwrote, DELETE ignored")

assert _absorb_ops([{"op": "INVALIDATE", "range": [900, 999]}], members) is True
ok("INVALIDATE detected")

# malformed payloads must not raise
_absorb_ops([{"op": "SYNC", "items": [None, {}, {"member": {}}, {"member": {"user": {}}}]}], members)
ok("malformed items ignored without raising")

# --- channel permission check --------------------------------------------
assert _everyone_can_view({"permission_overwrites": []}, "g1") is True
assert _everyone_can_view(
    {"permission_overwrites": [{"id": "g1", "type": 0, "deny": str(VIEW_CHANNEL)}]}, "g1") is False
assert _everyone_can_view(
    {"permission_overwrites": [{"id": "role9", "type": 0, "deny": str(VIEW_CHANNEL)}]}, "g1") is True
ok("channel @everyone VIEW_CHANNEL deny detection")

# --- verdict mapping ------------------------------------------------------
assert [verdict_for_flag(f).name for f in (0, 1, 2, 3, 4, 5, 6, 8)] == \
    ["NO_DETECTIONS", "THREAT", "THREAT", "INFO", "CAUTION", "CAUTION", "INFO", "INFO"]
assert verdict_for_flag(None) is Verdict.UNKNOWN
assert [f for f in (0,1,2,3,4,5,6,8) if flag_is_actionable(f)] == [1, 2], "only 1 and 2 actionable"
ok("flag -> verdict mapping matches the docs table; only Flagged/Confirmed actionable")

# worst-of across multiple linked accounts
rep = MemberReport("1", accounts=[
    RobloxAccount(1, "a", flag_type=0), RobloxAccount(2, "b", flag_type=2)])
assert rep.verdict is Verdict.THREAT and rep.worst_account.user_id == 2
ok("report takes the worst verdict across linked accounts")

# --- batch cost -----------------------------------------------------------
assert [batch_cost(n) for n in (1, 49, 50, 51, 99, 100)] == [1, 1, 1, 2, 2, 2]
ok("batch cost = ceil(n/50), matching measured API behaviour")


# --- limiter --------------------------------------------------------------
async def limiter_tests():
    # never exceeds limit-reserve inside one window
    lim = RateLimiter(limit=10, window=1.0, reserve=2)
    t0 = time.time()
    for _ in range(16):
        await lim.acquire(1)
    elapsed = time.time() - t0
    assert elapsed >= 1.0, f"16 units at 8/window should span >1 window, took {elapsed:.2f}s"
    ok(f"local window throttles: 16 units at 8/s took {elapsed:.2f}s")

    # server headers can hard-block us
    lim2 = RateLimiter(limit=50, window=10.0, reserve=5)
    lim2.observe({"x-ratelimit-limit": "50", "x-ratelimit-remaining": "1",
                  "x-ratelimit-reset": str(time.time() + 0.6)})
    t0 = time.time()
    await lim2.acquire(1)
    waited = time.time() - t0
    assert waited > 0.5, f"should have waited for reset, waited {waited:.2f}s"
    ok(f"server 'remaining <= reserve' hard-blocks until reset ({waited:.2f}s)")

    # 429 Retry-After is honoured
    lim3 = RateLimiter(limit=50, window=10.0, reserve=5)
    lim3.penalise(0.7)
    t0 = time.time()
    await lim3.acquire(1)
    waited = time.time() - t0
    assert 0.6 < waited < 1.4, waited
    ok(f"429 Retry-After honoured ({waited:.2f}s)")

    # concurrent callers share the budget
    lim4 = RateLimiter(limit=6, window=1.0, reserve=0)
    t0 = time.time()
    await asyncio.gather(*(lim4.acquire(1) for _ in range(12)))
    elapsed = time.time() - t0
    assert elapsed >= 1.0, elapsed
    ok(f"12 concurrent callers share one budget ({elapsed:.2f}s)")

    # a raised limit from the server is adopted
    lim5 = RateLimiter(limit=50, window=10.0, reserve=5)
    lim5.observe({"x-ratelimit-limit": "500", "x-ratelimit-remaining": "499",
                  "x-ratelimit-reset": str(time.time() + 10)})
    assert lim5.limit == 500
    ok("adopts an elevated API-key limit from X-RateLimit-Limit")

asyncio.run(limiter_tests())
print("\nALL UNIT TESTS PASSED")
