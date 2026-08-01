"""Live check: drive the Rotector client hard and prove we never get a 429."""
import asyncio, sys, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from rsb.ratelimit import RateLimiter, batch_cost
from rsb.rotector import RotectorClient
from rsb.verdict import verdict_label, flag_name, category_name

STATS = {"n": 0, "429": 0, "min_remaining": 99, "codes": {}}


async def main():
    limiter = RateLimiter(limit=50, window=10.0, reserve=5)
    client = RotectorClient(limiter=limiter, cache_ttl=0, concurrency=3)

    original = client._http.post

    async def spy(*a, **kw):
        resp = await original(*a, **kw)
        STATS["n"] += 1
        STATS["codes"][resp.status_code] = STATS["codes"].get(resp.status_code, 0) + 1
        if resp.status_code == 429:
            STATS["429"] += 1
        rem = resp.headers.get("x-ratelimit-remaining")
        if rem is not None:
            STATS["min_remaining"] = min(STATS["min_remaining"], int(rem))
        return resp

    client._http.post = spy

    # Real ids known to have data, padded out to force many batches.
    real = ["1"]
    ids = real + [str(900_000_000_000_000_000 + i) for i in range(1400)]

    t0 = time.time()

    def prog(stage, done, of):
        if done % 1000 and done != of: return
        print(f"  {stage}: {done}/{of}  ({time.time()-t0:5.1f}s)", flush=True)

    reports = await client.scan_members(ids, on_progress=prog)
    elapsed = time.time() - t0

    print(f"\nscanned {len(reports)} ids in {elapsed:.1f}s")
    print(f"http requests: {STATS['n']}  codes={STATS['codes']}")
    print(f"429s: {STATS['429']}   lowest x-ratelimit-remaining seen: {STATS['min_remaining']}")

    units = STATS["n"]  # each POST is >=1 unit
    print(f"observed throughput: {units*2/elapsed*10:.1f} request-units per 10s window (cap 50)")

    r = reports["1"]
    print(f"\nsample report for discord id 1: verdict={verdict_label(r.verdict)}")
    for acc in r.accounts:
        print(f"  {acc.username} ({acc.user_id}) flag={flag_name(acc.flag_type)} "
              f"cat={category_name(acc.category)} conf={acc.confidence}")

    print("\nbatch_cost sanity:", [(n, batch_cost(n)) for n in (1, 50, 51, 100)])

    await client.aclose()
    assert STATS["429"] == 0, "GOT RATE LIMITED"
    assert STATS["min_remaining"] > 0, "hit zero remaining"
    print("\nPASS: no 429s, stayed inside the window")


asyncio.run(main())
