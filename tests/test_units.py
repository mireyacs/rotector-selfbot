"""Unit tests for the parts that can't be exercised without a live token."""
import asyncio, sys, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from rsb.discord.gateway import (
    RANGES_PER_REQUEST,
    _absorb_ops,
    _sidebar_ranges,
    GuildMember,
)
from rsb.discord.http import _everyone_can_view, ADMINISTRATOR, VIEW_CHANNEL
from rsb.ratelimit import RateLimiter, batch_cost
from rsb.rotector import MemberReport, RobloxAccount, TrackedServer
from rsb.verdict import Verdict, flag_is_actionable, verdict_for_flag

ok = lambda m: print(f"[ok] {m}")

# --- sidebar ranges -------------------------------------------------------
# [0,99] is always included, as the real client does; the rest are the window
# being scrolled to. Several per request is what keeps a large list quick.
assert _sidebar_ranges(0) == [[0, 99]]
assert _sidebar_ranges(300, count=3) == [[0, 99], [300, 399], [400, 499], [500, 599]]
assert _sidebar_ranges(300, count=1) == [[0, 99], [300, 399]]
ok(f"sidebar ranges batch {RANGES_PER_REQUEST} windows per request: "
   f"{_sidebar_ranges(300)}")

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

# --- channel visibility ---------------------------------------------------
# Load-bearing for coverage: the member sidebar only lists members who can see
# the channel, so scraping a restricted channel silently truncates the scan.
G = "g1"
DENY = {"id": G, "type": 0, "deny": str(VIEW_CHANNEL)}
ALLOW = {"id": G, "type": 0, "allow": str(VIEW_CHANNEL)}

def visible(channel, base=VIEW_CHANNEL, parents=None):
    return _everyone_can_view(channel, G, base, parents or {})

assert visible({"permission_overwrites": []}) is True
assert visible({"permission_overwrites": [DENY]}) is False
assert visible({"permission_overwrites": []}, base=0) is False
assert visible({"permission_overwrites": [ALLOW]}, base=0) is True
assert visible({"permission_overwrites": [{"id": "r9", "type": 0,
                                           "deny": str(VIEW_CHANNEL)}]}) is True
ok("@everyone base permissions and channel overwrites resolved")

# a category deny is inherited unless the channel re-allows it
assert visible({"parent_id": "cat", "permission_overwrites": []},
               parents={"cat": {"permission_overwrites": [DENY]}}) is False
assert visible({"parent_id": "cat", "permission_overwrites": [ALLOW]},
               parents={"cat": {"permission_overwrites": [DENY]}}) is True
ok("category overwrites are inherited, and channel overwrites win over them")

assert visible({"permission_overwrites": [DENY]}, base=ADMINISTRATOR) is True
ok("ADMINISTRATOR on @everyone overrides every deny")

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


# --- no credential-shaped literals anywhere in the tree -------------------
# This has bitten twice: once a real token in a config backup, once fixtures
# fabricated to *look* real so they would pass a shape check. GitHub's secret
# scanner cannot tell a plausible fake from the genuine article, and neither
# can a person skimming a diff, so neither belongs in the repo.
import re as _re
import subprocess as _sp
from pathlib import Path as _Path

_TOKEN_SHAPES = [
    # Discord user/bot token: base64ish id . timestamp . hmac
    ("Discord token", _re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}")),
    # long opaque keys assigned to a suggestive name
    ("API key", _re.compile(r"(?i)(api_?key|secret|password)\s*=\s*[\"'][A-Za-z0-9_\-]{24,}[\"']")),
]

_ROOT = _Path(__file__).resolve().parent.parent
try:
    _tracked = _sp.run(
        ["git", "ls-files"], cwd=_ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
except Exception:
    _tracked = []

if _tracked:
    _offenders = []
    for _name in _tracked:
        _path = _ROOT / _name
        if _path.suffix in (".svg", ".png", ".jpg") or not _path.is_file():
            continue
        try:
            _body = _path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for _label, _pattern in _TOKEN_SHAPES:
            for _line_no, _line in enumerate(_body.splitlines(), 1):
                if _pattern.search(_line):
                    _offenders.append(f"{_name}:{_line_no} ({_label})")

    assert not _offenders, (
        "credential-shaped literals in tracked files -- a push will be blocked:\n  "
        + "\n  ".join(_offenders)
    )
    ok(f"no credential-shaped literals in any of {len(_tracked)} tracked files")

    _ignored = (_ROOT / ".gitignore").read_text(encoding="utf-8")
    for _pattern in ("config.toml", "*.bak", "proxies.txt"):
        assert _pattern in _ignored, f"{_pattern} is not gitignored"
    ok("config, proxy list and every .bak stay gitignored")
else:
    ok("not a git checkout; skipped the tracked-file secret scan")
