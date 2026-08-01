"""Proxy routing, failover, halt-on-total-failure, and ETA estimation.

The failover tests use proxies pointed at a closed local port, so the failures
are real transport failures rather than mocks, and the successful fallback hop
goes to the real Rotector API.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from rsb.eta import (
    RateEstimator,
    estimate_scan_seconds,
    format_duration,
    units_for_ids,
)
from rsb.proxy import (
    AllRoutesFailed,
    DIRECT_NAME,
    ProbeResult,
    Route,
    RoutePool,
    parse_proxy,
    probe_proxy,
    proxy_label,
    summarise_pool,
)
from rsb.ratelimit import RateLimiter
from rsb.rotector import RotectorClient

ok = lambda m: print(f"[ok] {m}")

# nothing listens here, so connections are refused immediately
DEAD = ["http://127.0.0.1:1", "http://127.0.0.1:2"]


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def test_parsing():
    cases = {
        "1.2.3.4:8080": "http://1.2.3.4:8080",
        "http://u:p@1.2.3.4:8080": "http://u:p@1.2.3.4:8080",
        "socks5://host:1080": "socks5://host:1080",
        "1.2.3.4:8080:user:pw": "http://user:pw@1.2.3.4:8080",
        "u:p@1.2.3.4:8080": "http://u:p@1.2.3.4:8080",
        "": None, "  ": None, "# a comment": None,
        "garbage": None, "ftp://1.2.3.4:21": None,
    }
    for raw, expected in cases.items():
        assert parse_proxy(raw) == expected, f"{raw!r} -> {parse_proxy(raw)!r}"
    ok(f"{len(cases)} proxy spellings parsed (vendor host:port:user:pass form included)")

    assert proxy_label("http://user:secret@1.2.3.4:8080") == "1.2.3.4:8080"
    assert "secret" not in proxy_label("http://user:secret@1.2.3.4:8080")
    ok("labels strip credentials")


# --------------------------------------------------------------------------
# pool mechanics
# --------------------------------------------------------------------------

def _pool(n_proxies=2, direct_as_fallback=True):
    limiter = RateLimiter(limit=50, window=10.0, reserve=5)
    direct = httpx.AsyncClient(base_url="https://example.invalid")
    return RoutePool.build(
        direct_client=direct,
        direct_limiter=limiter,
        proxies=DEAD[:n_proxies],
        direct_as_fallback=direct_as_fallback,
        base_url="https://example.invalid",
    ), direct


def test_pool_selection():
    pool, _ = _pool()
    assert len(pool.routes) == 3 and len(pool.proxies) == 2
    assert pool.direct is not None and pool.direct.name == DIRECT_NAME

    # proxies preferred while healthy
    picks = {pool.pick().name for _ in range(10)}
    assert DIRECT_NAME not in picks, f"direct used while proxies healthy: {picks}"
    assert len(picks) == 2, f"load did not spread across proxies: {picks}"
    ok(f"healthy proxies preferred and rotated: {sorted(picks)}")

    # one proxy dies -> the other carries it
    pool.proxies[0].penalise("boom")
    assert not pool.proxies[0].available()
    assert pool.pick().name == pool.proxies[1].name
    ok("a parked proxy is skipped, traffic moves to its sibling")

    # both die -> falls back to direct
    pool.proxies[1].penalise("boom")
    assert pool.pick().name == DIRECT_NAME
    ok("all proxies parked -> falls back to the direct connection")

    # direct dies too -> nothing left
    pool.direct.penalise("offline")
    assert pool.pick() is None
    ok("direct parked too -> pick() returns None, which triggers the halt")

    summary = pool.failure_summary()
    assert len(summary) == 3 and any(n == DIRECT_NAME for n, _ in summary)
    ok(f"failure summary names every route: {[n for n, _ in summary]}")


def test_backoff_and_recovery():
    limiter = RateLimiter(limit=50, window=10.0, reserve=5)
    route = Route("p1", httpx.AsyncClient(), limiter)

    assert route.available()
    route.penalise("first")
    assert not route.available(), "must park on the FIRST failure, not the third"
    first_delay = route.disabled_until - time.time()
    ok(f"parked on first failure ({first_delay:.0f}s) so a hanging proxy is not retried")

    route.penalise("second")
    second_delay = route.disabled_until - time.time()
    assert second_delay > first_delay, (first_delay, second_delay)
    ok(f"backoff escalates: {first_delay:.0f}s -> {second_delay:.0f}s")

    route.recover()
    assert route.available() and route.failures == 0
    ok("a success clears the strikes and un-parks the route")


def test_direct_not_fallback_is_coequal():
    pool, _ = _pool(direct_as_fallback=False)
    picks = {pool.pick().name for _ in range(20)}
    assert DIRECT_NAME in picks, "direct_as_fallback=False should use direct too"
    ok("direct_as_fallback=False makes the direct connection a co-equal route")


def test_capacity_scales_with_routes():
    one, _ = _pool(n_proxies=0)
    two, _ = _pool(n_proxies=2)
    c1, c2 = one.capacity_units_per_sec(), two.capacity_units_per_sec()
    assert c2 > c1, (c1, c2)
    ok(f"capacity scales with routes: {c1:.1f} -> {c2:.1f} units/s")

    two.proxies[0].penalise("dead")
    c3 = two.capacity_units_per_sec()
    assert c3 < c2, (c2, c3)
    ok(f"capacity drops when a route parks: {c2:.1f} -> {c3:.1f} units/s")


# --------------------------------------------------------------------------
# failover against the real API
# --------------------------------------------------------------------------

async def test_failover_to_direct_live():
    """Dead proxies must not break a scan -- direct picks up the work."""
    client = RotectorClient(proxies=DEAD, cache_ttl=0, concurrency=2)
    try:
        reports = await client.scan_members(["1", "2", "3"])
    finally:
        pass

    assert len(reports) == 3
    assert reports["1"].accounts, "real lookup did not come back"
    for proxy in client.pool.proxies:
        assert not proxy.available(), f"{proxy.name} should be parked"
        assert proxy.last_error, "no error recorded for the dead proxy"
    assert client.pool.direct.available()
    ok(f"2 dead proxies parked ({client.pool.proxies[0].last_error[:40]}...), "
       f"direct served {len(reports)} lookups")
    await client.aclose()


async def test_halt_when_everything_fails():
    """Direct broken as well -> AllRoutesFailed carrying each route's error."""
    client = RotectorClient(proxies=DEAD, cache_ttl=0, concurrency=2)

    async def refuse(*a, **kw):
        raise httpx.ConnectError("simulated: direct connection is down")

    client.pool.direct.client.post = refuse

    try:
        await client.scan_members(["1"])
        raise AssertionError("expected AllRoutesFailed")
    except AllRoutesFailed as exc:
        names = [n for n, _ in exc.attempts]
        assert DIRECT_NAME in names, names
        assert len(names) >= 3, names
        assert exc.direct_error and "down" in exc.direct_error
        ok(f"halted with a per-route diagnosis: {names}")
        ok(f"direct error surfaced verbatim: {exc.direct_error[:60]}")
        assert "every route failed" in str(exc)
    finally:
        await client.aclose()


async def test_probe_direct_and_dead():
    result = await probe_proxy(DIRECT_NAME, timeout=20)
    assert result.label == DIRECT_NAME
    if result.ok:
        # every field comes from Rotector's own response, not an IP service
        assert result.status == 200, result.status
        assert result.rate_limit and result.rate_remaining is not None
        ok(f"probed direct against Rotector: HTTP {result.status}, "
           f"{result.latency_ms:.0f}ms, budget {result.rate_limit}/window, "
           f"used {result.used_in_window} -> verdict {result.verdict}")
    else:
        ok(f"probed direct: unreachable ({result.error}) - offline environment")

    dead = await probe_proxy("127.0.0.1:1", timeout=5)
    assert not dead.ok and dead.verdict == "FAIL" and dead.error
    ok(f"dead proxy probe reports FAIL: {dead.error[:50]}")

    bad = await probe_proxy("not a proxy", timeout=5)
    assert not bad.ok and "unrecognised" in (bad.error or "")
    ok("malformed proxy rejected before any connection attempt")


def test_shared_budget_detection():
    """Budget independence is read from Rotector's own rate-limit headers."""
    def probe(used, status=200, ok=True):
        return ProbeResult(
            raw="p", url="http://p", label="p", ok=ok, status=status,
            rate_limit=50, rate_remaining=50 - used,
        )

    fresh = probe(used=1)
    assert fresh.used_in_window == 1
    assert fresh.independent_budget is True and fresh.verdict == "OK"
    ok("an exit reporting 1 request used has its own budget -> OK")

    shared = probe(used=7)
    assert shared.independent_budget is False and shared.verdict == "SHARED"
    ok("an exit already 7 requests into its window is SHARED, not OK")

    blocked = probe(used=1, status=403)
    assert blocked.verdict == "NO API"
    ok("reaching the internet but not Rotector is NO API, not OK")

    dead = ProbeResult(raw="p", url="http://p", label="p", ok=False, error="refused")
    assert dead.verdict == "FAIL" and dead.independent_budget is None
    ok("an unreachable proxy is FAIL with no budget claim")

    stats = summarise_pool([fresh, shared, blocked, dead])
    assert stats["independent"] == 1 and stats["shared"] == 1
    assert stats["no_api"] == 1 and stats["failed"] == 1
    assert stats["combined_budget"] == 50, stats
    ok(f"pool summary counts only independent budgets: {stats['combined_budget']}/window")


# --------------------------------------------------------------------------
# ETA
# --------------------------------------------------------------------------

def test_eta_math():
    assert format_duration(0.4) == "<1s"
    assert format_duration(45) == "45s"
    assert format_duration(95) == "1m 35s"
    assert format_duration(3660) == "1h 01m"
    assert format_duration(7380) == "2h 03m"
    assert format_duration(None) == "?"
    ok("durations format across every magnitude")

    # 100 ids = 2 units, 150 = 2 + 1, 50 = 1
    assert units_for_ids(0) == 0
    assert units_for_ids(50) == 1
    assert units_for_ids(100) == 2
    assert units_for_ids(150) == 3
    assert units_for_ids(1000) == 20
    ok("request-unit maths matches the measured ceil(n/50) batch cost")

    # 50 req / 10s, reserve 5 -> 4.5 units/s
    capacity = 4.5
    small = estimate_scan_seconds(1_000, capacity)
    large = estimate_scan_seconds(50_000, capacity)
    assert small and large and large > small * 40
    ok(f"pre-scan estimate: 1k members ~{format_duration(small)}, "
       f"50k members ~{format_duration(large)}")

    doubled = estimate_scan_seconds(50_000, capacity * 2)
    assert abs(doubled - large / 2) < 1, (large, doubled)
    ok(f"doubling capacity halves the estimate: {format_duration(large)} -> "
       f"{format_duration(doubled)}")

    assert estimate_scan_seconds(0, capacity) is None
    assert estimate_scan_seconds(100, 0) is None
    ok("degenerate inputs return None rather than nonsense")


def test_rate_estimator():
    est = RateEstimator(min_elapsed=0.05, min_samples=3)
    assert est.rate is None and est.eta(0, 100) is None
    assert est.describe(0, 100) == "ETA --"
    ok("no estimate offered before there is evidence")

    now = time.monotonic()
    for i in range(6):                      # ~100 items/sec
        est._samples.append((now + i * 0.1, i * 10))
    est._started = now
    rate = est.rate
    assert rate and 90 < rate < 110, rate
    eta = est.eta(50, 1050)
    assert eta and 9 < eta < 11, eta
    ok(f"measured {rate:.0f}/s -> ETA {format_duration(eta)} for 1000 remaining")

    text = est.describe(50, 1050)
    assert "ETA" in text and "/s" in text
    ok(f"describe(): {text!r}")

    est.reset()
    assert est.rate is None
    ok("reset() clears the window between phases")


def test_estimator_reacts_to_a_stall():
    """A stall must move the ETA quickly, not be averaged away."""
    est = RateEstimator(window=10.0, min_elapsed=0.05, min_samples=3)
    now = time.monotonic()
    for i in range(11):                     # fast: 100/s for 1s
        est._samples.append((now + i * 0.1, i * 10))
    est._started = now
    fast = est.rate

    # now stall: time passes, progress does not
    for i in range(1, 6):
        est._samples.append((now + 1.0 + i, 100))
    stalled = est.rate
    assert stalled < fast / 4, (fast, stalled)
    ok(f"a stall drops the measured rate {fast:.0f}/s -> {stalled:.0f}/s "
       f"instead of hiding in an average")


async def main():
    test_parsing()
    test_pool_selection()
    test_backoff_and_recovery()
    test_direct_not_fallback_is_coequal()
    test_capacity_scales_with_routes()
    test_shared_budget_detection()
    print()
    test_eta_math()
    test_rate_estimator()
    test_estimator_reacts_to_a_stall()
    print()
    await test_failover_to_direct_live()
    await test_halt_when_everything_fails()
    await test_probe_direct_and_dead()
    print("\nALL ROUTING TESTS PASSED")


asyncio.run(main())
