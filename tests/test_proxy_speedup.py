"""Does the proxy system actually deliver, and does it lose anything?

Three claims are checked against real sockets -- a real client, real local
forward proxies, and a mock Rotector that enforces a genuine per-bucket rate
limit (each proxy = one bucket, exactly like one exit IP = one budget):

1. Every id gets answered. No lookup is silently dropped, whatever fails.
2. More routes really is faster -- and roughly in proportion.
3. Each route spends its own budget, and the main connection carries work too.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _mockapi import DIRECT_BUCKET, ForwardProxy, MockRotector

from rsb.proxy import AllRoutesFailed
from rsb.ratelimit import RateLimiter
from rsb.rotector import RotectorClient

ok = lambda m: print(f"[ok] {m}")

LIMIT, WINDOW, RESERVE = 50, 2.0, 5
IDS = [str(900_000_000_000_000_000 + i) for i in range(6000)]


def _client(base_url, proxies, **kw):
    return RotectorClient(
        base_url=base_url,
        proxies=proxies,
        limiter=RateLimiter(limit=LIMIT, window=WINDOW, reserve=RESERVE),
        cache_ttl=0,
        **kw,
    )


async def _run(base_url, proxies, ids=IDS, **kw):
    client = _client(base_url, proxies, **kw)
    started = time.monotonic()
    try:
        reports = await client.scan_members(ids)
    finally:
        elapsed = time.monotonic() - started
    return client, reports, elapsed


async def test_no_query_left_unanswered():
    api = MockRotector(limit=LIMIT, window=WINDOW)
    base = await api.start()
    proxies = [ForwardProxy(f"p{i}") for i in range(3)]
    urls = [await p.start() for p in proxies]

    try:
        client, reports, elapsed = await _run(base, urls)

        assert len(reports) == len(IDS), f"{len(reports)} reports for {len(IDS)} ids"
        assert set(reports) == set(IDS), "report keys do not match the input ids"
        ok(f"every one of {len(IDS):,} ids came back with a report")

        errored = [r for r in reports.values() if r.error]
        assert not errored, f"{len(errored)} unanswered: {errored[:2]}"
        ok("no report carries an error -- nothing went unanswered")

        assert api.ids_served == set(IDS), (
            f"server saw {len(api.ids_served)} distinct ids, expected {len(IDS)}"
        )
        ok(f"the server itself confirms all {len(api.ids_served):,} ids were looked up")
        ok(f"{sum(api.requests.values())} requests, {api.rejections} rejected "
           f"in {elapsed:.1f}s")
        await client.aclose()
    finally:
        for p in proxies:
            await p.stop()
        await api.stop()


async def test_routes_share_the_work():
    api = MockRotector(limit=LIMIT, window=WINDOW)
    base = await api.start()
    proxies = [ForwardProxy(f"p{i}") for i in range(3)]
    urls = [await p.start() for p in proxies]

    try:
        client, reports, _ = await _run(base, urls, ids=IDS[:3000])

        buckets = dict(api.requests)
        assert len(buckets) == 4, f"expected 3 proxies + direct, got {buckets}"
        assert DIRECT_BUCKET in buckets, "the user's own connection carried nothing"
        ok(f"all four routes carried traffic: {buckets}")

        counts = sorted(buckets.values())
        assert counts[0] > counts[-1] * 0.3, f"load badly skewed: {buckets}"
        ok(f"load spread reasonably evenly (min {counts[0]}, max {counts[-1]})")

        for proxy in proxies:
            assert proxy.forwarded > 0, f"{proxy.bucket} forwarded nothing"
        ok(f"each proxy actually relayed traffic: "
           f"{[p.forwarded for p in proxies]}")
        await client.aclose()
    finally:
        for p in proxies:
            await p.stop()
        await api.stop()


async def test_more_routes_is_faster():
    """Speedup must be *proportional* to routes, not just 'faster'.

    The workload is deliberately large enough that four routes are still rate
    limited. Sized any smaller, the pooled run would fit inside a single window
    and never throttle at all -- which produces a spectacular speedup number
    that says nothing about whether capacity actually scales.
    """
    api = MockRotector(limit=LIMIT, window=WINDOW)
    base = await api.start()
    proxies = [ForwardProxy(f"p{i}") for i in range(3)]
    urls = [await p.start() for p in proxies]
    heavy = [str(800_000_000_000_000_000 + i) for i in range(12000)]

    try:
        # direct only
        client1, reports1, solo = await _run(base, [], ids=heavy)
        await client1.aclose()
        assert len(reports1) == len(heavy) and not any(r.error for r in reports1.values())

        await asyncio.sleep(WINDOW + 0.3)   # let every bucket reset

        # direct + 3 proxies = 4 independent budgets
        client4, reports4, pooled = await _run(base, urls, ids=heavy)
        await client4.aclose()
        assert len(reports4) == len(heavy) and not any(r.error for r in reports4.values())

        speedup = solo / pooled
        ok(f"{len(heavy):,} ids: 1 route {solo:.1f}s -> 4 routes {pooled:.1f}s "
           f"({speedup:.1f}x faster)")
        # both runs throttle, so the ratio should track the route count
        assert speedup > 2.5, (
            f"4 routes only gave {speedup:.1f}x -- the pool is not being used in "
            f"parallel"
        )
        assert speedup < 6.0, (
            f"{speedup:.1f}x exceeds what 4 routes can explain; the solo run was "
            f"probably not rate limited, making this a meaningless comparison"
        )
        ok(f"speedup tracks the route count ({speedup:.1f}x on 4 routes, "
           f"both runs throttled)")
    finally:
        for p in proxies:
            await p.stop()
        await api.stop()


async def test_dead_proxies_do_not_lose_queries():
    """Half the proxies are broken; every id must still be answered."""
    api = MockRotector(limit=LIMIT, window=WINDOW)
    base = await api.start()
    good = [ForwardProxy(f"good{i}") for i in range(2)]
    bad = [ForwardProxy(f"bad{i}", fail=True) for i in range(2)]
    urls = [await p.start() for p in good + bad]
    urls.append("http://127.0.0.1:1")        # nothing listening at all

    try:
        client, reports, elapsed = await _run(base, urls, ids=IDS[:2000])

        assert set(reports) == set(IDS[:2000])
        errored = [r for r in reports.values() if r.error]
        assert not errored, f"{len(errored)} lookups lost to broken proxies"
        ok(f"all {len(reports):,} ids answered despite 3 of 5 proxies being broken")

        parked = [r.name for r in client.pool.proxies if not r.available()]
        assert len(parked) >= 3, f"broken proxies not parked: {parked}"
        ok(f"broken routes parked and skipped: {len(parked)} of "
           f"{len(client.pool.proxies)}")

        assert api.ids_served == set(IDS[:2000])
        ok("the server confirms every id still reached it")
        await client.aclose()
    finally:
        for p in good + bad:
            await p.stop()
        await api.stop()


async def test_halt_when_api_is_gone():
    """Nothing reachable anywhere -> halt, not a silent empty result."""
    proxies = [ForwardProxy("p0", fail=True)]
    urls = [await p.start() for p in proxies]
    try:
        client = _client("http://127.0.0.1:1", urls)
        try:
            await client.scan_members(IDS[:200])
            raise AssertionError("expected AllRoutesFailed, got a silent result")
        except AllRoutesFailed as exc:
            names = [n for n, _ in exc.attempts]
            assert DIRECT_BUCKET in names, names
            ok(f"halted rather than returning empty reports: {names}")
        await client.aclose()
    finally:
        for p in proxies:
            await p.stop()


async def main():
    await test_no_query_left_unanswered()
    print()
    await test_routes_share_the_work()
    print()
    await test_dead_proxies_do_not_lose_queries()
    print()
    await test_halt_when_api_is_gone()
    print()
    await test_more_routes_is_faster()
    print("\nALL PROXY SPEEDUP TESTS PASSED")


asyncio.run(main())
