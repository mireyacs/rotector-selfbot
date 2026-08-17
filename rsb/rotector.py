"""Rotector API client.

Turning a list of Discord member ids into a threat verdict takes two hops,
because the Discord endpoint does not itself return a flag status:

  1. ``POST /v1/lookup/discord/user``  (<=100 ids)
     -> tracked server memberships + linked Roblox accounts
  2. ``POST /v1/lookup/roblox/user``   (<=100 ids)
     -> flag type, category, confidence and reasons per Roblox account

Both endpoints share one per-IP rate limit bucket, so they share one
:class:`~rsb.ratelimit.RateLimiter`.

Rotector Terms of Use #1 forbids retaining responses for longer than 24 hours,
which is what the cache here does unless it is told otherwise: it is in-memory
only, its TTL is clamped to :data:`MAX_CACHE_TTL`, and nothing is written to
disk by this module.  That ceiling is lifted only by an explicit setting --
``scan.retain_beyond_terms``, off by default -- and switching it on is a choice
to hold Rotector's responses outside the terms they were served under.

Nothing is quietly passed off as current either way.  Every
:class:`MemberReport` carries the time its data actually arrived, and anything
answered from beyond the 24-hour window is reported as stale, with its age,
wherever it is shown or written -- see :func:`is_stale` and :func:`stale_note`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

import httpx

from .eta import estimate_scan_seconds
from .proxy import AllRoutesFailed, RoutePool
from .ratelimit import RateLimiter, batch_cost
from .verdict import Signal, Verdict, verdict_for_flag

BASE_URL = "https://roscoe.rotector.com"
MAX_BATCH = 100
#: hard ceiling from the Terms of Use; the configured TTL is clamped to this
MAX_CACHE_TTL = 23 * 3600
#: the ceiling that applies when the operator has switched `retain_beyond_terms`
#: on. Not unbounded: an unbounded cache is a memory leak as well as a licensing
#: problem, and a year-old flag status is not evidence about anybody.
MAX_RETAINED_TTL = 365 * 24 * 3600
USER_AGENT = "rotector-selfbot/1.0 (+https://rotector.com)"

ProgressCallback = Callable[[str, int, int], None]

#: seconds of quiet before a partial batch is sent anyway
IDLE_FLUSH = 2.0



def _absorb(task: "asyncio.Task") -> None:
    """Mark a finished task's exception as seen.

    Attached the moment a batch task is created, because the gather that would
    normally collect it is only reached on the happy path: cancel the stream
    while it is still consuming ids -- which is what quitting the app mid-scan
    does -- and every task already in flight is orphaned. Its error then
    surfaces later as "Task exception was never retrieved", from a traceback
    with no caller and no UI left to report into.

    Reading the exception here is idempotent; the gather still sees it too.
    """
    if not task.cancelled():
        task.exception()


class RotectorError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# result types
# --------------------------------------------------------------------------


@dataclass
class TrackedServer:
    server_id: str
    server_name: str
    joined_at: int | None
    first_seen_at: int | None
    is_tase: bool
    in_grace_period: bool

    @classmethod
    def parse(cls, raw: dict) -> "TrackedServer":
        return cls(
            server_id=str(raw.get("serverId", "")),
            server_name=raw.get("serverName") or "(unnamed)",
            joined_at=_maybe_int(raw.get("joinedAt")),
            first_seen_at=_maybe_int(raw.get("firstSeenAt")),
            is_tase=bool(raw.get("isTase")),
            in_grace_period=bool(raw.get("inGracePeriod")),
        )


@dataclass
class RobloxAccount:
    user_id: int
    username: str
    detected_at: int | None = None
    sources: list[int] = field(default_factory=list)
    # filled in by the second hop
    flag_type: int | None = None
    category: int | None = None
    confidence: float | None = None
    reasons: dict[str, dict] = field(default_factory=dict)
    last_updated: int | None = None

    @property
    def verdict(self) -> Verdict:
        return verdict_for_flag(self.flag_type)

    @property
    def profile_url(self) -> str:
        return f"https://www.roblox.com/users/{self.user_id}/profile"


@dataclass
class MemberReport:
    """Everything the active backend knows about one Discord member.

    Rotector fills ``accounts`` and ``servers``. A backend that answers per
    source rather than per Roblox account -- see :mod:`rsb.okappiki` -- fills
    ``signals`` instead, and may fill both when a source does name an account.
    """

    discord_id: str
    accounts: list[RobloxAccount] = field(default_factory=list)
    servers: list[TrackedServer] = field(default_factory=list)
    error: str | None = None
    #: per-source findings; always empty under the Rotector backend
    signals: list[Signal] = field(default_factory=list)
    #: when the data behind this report actually arrived from the backend --
    #: which is not when the report was built, because a cache hit describes
    #: the world as of the response it replays. Defaults to now, so a report
    #: assembled from a fresh answer is dated correctly without being asked;
    #: whoever serves one from cache is responsible for backdating it.
    fetched_at: float = field(default_factory=time.time)

    @property
    def verdict(self) -> Verdict:
        found = [acc.verdict for acc in self.accounts]
        found += [sig.verdict for sig in self.signals]
        if not found:
            return Verdict.UNKNOWN
        return max(found)

    @property
    def flagged_signals(self) -> list[Signal]:
        """Only the sources that actually said something, worst first."""
        return sorted(
            (sig for sig in self.signals if sig.flagged),
            key=lambda sig: sig.verdict,
            reverse=True,
        )

    @property
    def worst_account(self) -> RobloxAccount | None:
        if not self.accounts:
            return None
        return max(self.accounts, key=lambda a: (a.verdict, a.confidence or 0))

    @property
    def tracked_server_count(self) -> int:
        return len(self.servers)


# --------------------------------------------------------------------------
# staleness
# --------------------------------------------------------------------------

#: Rotector's Terms of Use #1 allow their responses to be kept for 24 hours, so
#: this is the line past which a finding is no longer something the terms cover
#: -- and, more to the point for whoever reads it, no longer a claim about now.
TERMS_WINDOW = 24 * 3600


def report_age(report: MemberReport, now: float | None = None) -> float:
    """How long ago the data behind ``report`` arrived, in seconds."""
    return max(0.0, (time.time() if now is None else now) - report.fetched_at)


def is_stale(report: MemberReport, now: float | None = None) -> bool:
    """Whether this finding is older than the terms' 24-hour window.

    A stale finding is not a wrong one, but it is a weaker claim than a fresh
    one and has to be shown as such: flag types change, appeals succeed, and an
    account that cleared its violations yesterday still reads as flagged in a
    cache that outlived the window.
    """
    return report_age(report, now) >= TERMS_WINDOW


def describe_age(seconds: float) -> str:
    """An age a person can read: "3 days", "26 hours", "12 minutes"."""
    seconds = max(0.0, seconds)
    if seconds >= 2 * 86400:
        return f"{int(seconds // 86400)} days"
    if seconds >= 3600:
        hours = int(seconds // 3600)
        return f"{hours} hour" + ("" if hours == 1 else "s")
    minutes = int(seconds // 60)
    return f"{minutes} minute" + ("" if minutes == 1 else "s")


def short_age(seconds: float) -> str:
    """The same age where there is only room for a few columns: "3d", "26h"."""
    seconds = max(0.0, seconds)
    if seconds >= 86400:
        return f"{int(seconds // 86400)}d"
    if seconds >= 3600:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 60)}m"


def stale_note(report: MemberReport, now: float | None = None) -> str:
    """The sentence every surface uses for a stale finding, or "" if fresh.

    Written once here so the table, the detail pane and all four export formats
    say the same thing: a reader who meets this in an exported file did not
    choose to keep the data past the window and is owed the reason it might no
    longer be true.
    """
    if not is_stale(report, now):
        return ""
    return (
        f"STALE - answered {describe_age(report_age(report, now))} ago, past the "
        "24-hour window Rotector's Terms of Use allow. Flag types change and "
        "appeals succeed, so re-check before acting on this."
    )


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------


class RotectorClient:
    def __init__(
        self,
        api_key: str | None = None,
        limiter: RateLimiter | None = None,
        cache_ttl: int = 3600,
        retain_beyond_terms: bool = False,
        concurrency: int = 3,
        max_retries: int = 4,
        timeout: float = 30.0,
        proxies: list[str] | None = None,
        direct_as_fallback: bool = False,
        base_url: str = BASE_URL,
        scan_concurrency: int | None = None,
    ) -> None:
        self.limiter = limiter or RateLimiter()
        self.set_retention(cache_ttl, retain_beyond_terms)
        self.max_retries = max_retries
        self._sem = asyncio.Semaphore(max(1, concurrency))

        headers = {"user-agent": USER_AGENT, "accept": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        self.base_url = base_url
        self._http = httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=timeout
        )
        # The direct route wraps _http, so proxying is transparent to callers
        # and a scan degrades to a plain single-connection scan when no proxy
        # is configured.
        self.pool = RoutePool.build(
            direct_client=self._http,
            direct_limiter=self.limiter,
            proxies=proxies,
            headers=headers,
            timeout=timeout,
            direct_as_fallback=direct_as_fallback,
            base_url=base_url,
        )

        # Concurrency is sized from the pool: one route can only ever have so
        # much in flight, but every extra route adds real parallel capacity.
        routes = len(self.pool.routes)
        self.scan_concurrency = scan_concurrency or max(concurrency, routes * 3)
        self._sem = asyncio.Semaphore(max(self.scan_concurrency, concurrency))

        self._discord_cache: dict[str, tuple[float, dict]] = {}
        self._roblox_cache: dict[int, tuple[float, dict]] = {}

    async def aclose(self) -> None:
        await self.pool.aclose()
        await self._http.aclose()

    def set_retention(self, cache_ttl: int, retain_beyond_terms: bool) -> None:
        """Set how long answers may be held, and clamp it to what is allowed.

        The 24h ceiling is Rotector's Terms of Use #1 and is the default. The
        operator can lift it deliberately -- see ScanConfig.retain_beyond_terms
        -- and when they have, anything served from beyond the window is
        reported as stale rather than passed off as current.

        Separate from ``__init__`` so the setting can be changed while the app
        is running without rebuilding the client and losing its cache. Turning
        it back *off* drops whatever the cache is still holding: the shorter
        TTL would expire those entries on the next read anyway, but the point
        of the default is not keeping Rotector's responses around, and leaving
        them in memory until something happens to ask for them is not that.
        """
        ceiling = MAX_RETAINED_TTL if retain_beyond_terms else MAX_CACHE_TTL
        was_retaining = getattr(self, "retain_beyond_terms", False)
        self.cache_ttl = min(max(0, cache_ttl), ceiling)
        self.retain_beyond_terms = bool(retain_beyond_terms)
        if was_retaining and not self.retain_beyond_terms:
            self.purge_cache()

    def capacity_units_per_sec(self) -> float:
        """Combined request-unit throughput across every usable route."""
        return self.pool.capacity_units_per_sec()

    def estimate_seconds(self, members: int) -> float | None:
        """How long scanning ``members`` should take on this backend.

        Lives on the client because the arithmetic is the backend's: this one
        batches a hundred ids per request and makes a second pass over whoever
        turns out to have a linked account.
        """
        return estimate_scan_seconds(members, self.capacity_units_per_sec())

    async def __aenter__(self) -> "RotectorClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    # -- transport ---------------------------------------------------------

    async def _post_batch(self, path: str, ids: Sequence[str]) -> dict:
        """One rate-limited POST, routed over the healthiest available path.

        The loop is bounded on two independent axes so it can neither spin nor
        exit quietly:

        * *route* failures (transport errors, proxy-shaped HTTP codes, non-JSON
          bodies) park the route and add it to ``tried``; once every route has
          been tried, :class:`AllRoutesFailed` is raised with each one's error.
        * *soft* retries (429, 5xx) keep the route but are capped, so a
          permanently throttled endpoint raises instead of looping forever.

        Either way this returns data or raises -- it never returns empty, which
        is what would make a lookup silently look like "nothing found".
        """
        cost = batch_cost(len(ids))
        payload = {"ids": [str(i) for i in ids]}
        attempts: list[tuple[str, str]] = []
        tried: set[str] = set()
        soft_retries = 0
        max_soft = self.max_retries + 4

        while True:
            route = self.pool.pick(exclude=tried)
            if route is None:
                raise AllRoutesFailed(attempts or self.pool.failure_summary())

            await route.limiter.acquire(cost)
            async with self._sem:
                try:
                    resp = await route.client.post(path, json=payload)
                except httpx.HTTPError as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    route.penalise(error)
                    attempts.append((route.name, error))
                    tried.add(route.name)
                    continue

            route.limiter.observe(resp.headers)

            if resp.status_code == 429:
                # backpressure, not a fault: the limiter now holds this route
                route.limiter.penalise(_retry_after(resp.headers))
                soft_retries += 1
                if soft_retries > max_soft:
                    raise RotectorError(
                        f"{path}: still rate limited after {soft_retries} retries"
                    )
                continue

            if resp.status_code in (403, 407, 502, 503) and not route.is_direct:
                # blocked exit IP, bad proxy auth, or a dead upstream
                error = f"HTTP {resp.status_code} via proxy"
                route.penalise(error)
                attempts.append((route.name, error))
                tried.add(route.name)
                continue

            if resp.status_code >= 500:
                soft_retries += 1
                if soft_retries > max_soft:
                    raise RotectorError(
                        f"{path}: server error {resp.status_code} after "
                        f"{soft_retries} retries"
                    )
                await asyncio.sleep(min(2**soft_retries, 15))
                continue

            if resp.status_code >= 400:
                raise RotectorError(
                    f"{path} -> HTTP {resp.status_code}: {resp.text[:200]}"
                )

            try:
                body = resp.json()
            except ValueError:
                error = "response was not JSON (intercepting proxy?)"
                route.penalise(error)
                attempts.append((route.name, error))
                tried.add(route.name)
                continue

            route.recover()
            if not body.get("success"):
                raise RotectorError(f"{path} -> {body.get('error', 'unknown error')}")
            return body.get("data") or {}

    # -- caching -----------------------------------------------------------

    def _cache_get(self, cache: dict, key) -> dict | None:
        entry = cache.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if time.time() - stored_at > self.cache_ttl:
            cache.pop(key, None)
            return None
        return value

    def _cache_put(self, cache: dict, key, value: dict) -> None:
        if self.cache_ttl:
            cache[key] = (time.time(), value)

    def _fetched_at(self, cache: dict, key) -> float:
        """When the data now standing behind ``key`` was actually received.

        A cache hit is not a fresh answer, and a report built from one has to
        carry the age of the response it replays rather than the moment it was
        assembled. A key missing from the cache was answered just now: either
        the lookup put it there a moment ago, or ``cache_ttl`` is 0 and nothing
        is ever cached at all, and in both cases the data is current.
        """
        entry = cache.get(key)
        return entry[0] if entry else time.time()

    def purge_cache(self) -> None:
        """Drop every cached response (also called on scan restart)."""
        self._discord_cache.clear()
        self._roblox_cache.clear()

    # -- endpoints ---------------------------------------------------------

    async def lookup_discord_users(self, ids: Iterable[str]) -> dict[str, dict]:
        wanted = [str(i) for i in dict.fromkeys(ids)]
        out: dict[str, dict] = {}
        missing: list[str] = []
        for uid in wanted:
            cached = self._cache_get(self._discord_cache, uid)
            if cached is None:
                missing.append(uid)
            else:
                out[uid] = cached

        for chunk in _chunks(missing, MAX_BATCH):
            data = await self._post_batch("/v1/lookup/discord/user", chunk)
            for uid in chunk:
                entry = data.get(uid) or {"id": uid, "servers": [], "connections": []}
                self._cache_put(self._discord_cache, uid, entry)
                out[uid] = entry
        return out

    async def lookup_roblox_users(self, ids: Iterable[int]) -> dict[int, dict]:
        wanted = list(dict.fromkeys(int(i) for i in ids))
        out: dict[int, dict] = {}
        missing: list[int] = []
        for rid in wanted:
            cached = self._cache_get(self._roblox_cache, rid)
            if cached is None:
                missing.append(rid)
            else:
                out[rid] = cached

        for chunk in _chunks(missing, MAX_BATCH):
            data = await self._post_batch(
                "/v1/lookup/roblox/user", [str(c) for c in chunk]
            )
            for rid in chunk:
                entry = data.get(str(rid)) or {"id": rid, "flagType": None}
                self._cache_put(self._roblox_cache, rid, entry)
                out[rid] = entry
        return out

    # -- orchestration -----------------------------------------------------

    async def scan_stream(
        self,
        incoming: "asyncio.Queue",
        on_progress: ProgressCallback | None = None,
        on_partial: Callable[[list[MemberReport]], None] | None = None,
        idle_flush: float | None = IDLE_FLUSH,
    ) -> dict[str, MemberReport]:
        """Scan ids as they arrive, rather than waiting for the full set.

        ``incoming`` yields ids (singly or in lists); ``None`` closes the
        stream.  Ids are buffered into full batches and dispatched as soon as
        each batch fills, so lookups overlap whatever is still producing ids --
        for a guild scan, that means checking members while the member list is
        still being read.

        A partial batch is dispatched after ``idle_flush`` seconds of quiet.
        Without that, a source that trickles -- a live message feed, say --
        would sit in the buffer forever waiting for a hundredth id that may
        never come.

        Batches run concurrently, deduplication is global across the stream,
        and every id that arrives is guaranteed a report.
        """
        reports: dict[str, MemberReport] = {}
        seen: set[str] = set()
        buffer: list[str] = []
        tasks: list[asyncio.Task] = []
        sem = asyncio.Semaphore(self.scan_concurrency)
        lock = asyncio.Lock()
        done = 0

        async def publish(batch: list[MemberReport], count: int, stage: str) -> None:
            nonlocal done
            async with lock:
                done += count
                for report in batch:
                    reports[report.discord_id] = report
                if on_progress:
                    on_progress(stage, done, len(seen))
                if on_partial and batch:
                    on_partial(batch)

        async def handle(chunk: list[str]) -> None:
            async with sem:
                if on_progress:
                    on_progress("Looking up Discord accounts", done, len(seen))
                try:
                    discord_data = await self.lookup_discord_users(chunk)
                except AllRoutesFailed:
                    raise
                except Exception as exc:  # noqa: BLE001
                    batch = [
                        MemberReport(discord_id=uid, error=str(exc)) for uid in chunk
                    ]
                    await publish(batch, len(chunk), "Some lookups failed")
                    return

                batch_reports: list[MemberReport] = []
                roblox_ids: set[int] = set()
                for uid in chunk:
                    raw = discord_data.get(uid, {})
                    report = MemberReport(
                        discord_id=uid,
                        fetched_at=self._fetched_at(self._discord_cache, uid),
                    )
                    for srv in raw.get("servers") or []:
                        report.servers.append(TrackedServer.parse(srv))
                    for conn in raw.get("connections") or []:
                        rid = _maybe_int(conn.get("robloxUserId"))
                        if rid is None:
                            continue
                        report.accounts.append(
                            RobloxAccount(
                                user_id=rid,
                                username=conn.get("robloxUsername") or str(rid),
                                detected_at=_maybe_int(conn.get("detectedAt")),
                                sources=list(conn.get("sources") or []),
                            )
                        )
                        roblox_ids.add(rid)
                    batch_reports.append(report)

                if roblox_ids:
                    if on_progress:
                        on_progress(
                            f"Checking {len(roblox_ids):,} linked Roblox "
                            f"account{'s' if len(roblox_ids) != 1 else ''} for flags",
                            done,
                            len(seen),
                        )
                    try:
                        flags = await self.lookup_roblox_users(roblox_ids)
                    except AllRoutesFailed:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        for report in batch_reports:
                            report.error = f"flag lookup failed: {exc}"
                        flags = {}
                    for report in batch_reports:
                        for acc in report.accounts:
                            _apply_flag(acc, flags.get(acc.user_id))
                            # A report is only as current as the oldest response
                            # behind it: the flag lookup is the half that says
                            # whether someone is flagged, so a fresh membership
                            # record paired with a day-old flag status is a
                            # day-old finding, and has to be dated as one.
                            report.fetched_at = min(
                                report.fetched_at,
                                self._fetched_at(self._roblox_cache, acc.user_id),
                            )

                await publish(batch_reports, len(chunk), "Checking members")

        def dispatch(force: bool = False) -> None:
            while len(buffer) >= MAX_BATCH or (force and buffer):
                chunk = buffer[:MAX_BATCH]
                del buffer[:MAX_BATCH]
                task = asyncio.create_task(handle(chunk))
                task.add_done_callback(_absorb)
                tasks.append(task)

        while True:
            if idle_flush and buffer:
                try:
                    item = await asyncio.wait_for(incoming.get(), timeout=idle_flush)
                except TimeoutError:
                    dispatch(force=True)
                    continue
            else:
                item = await incoming.get()
            if item is None:
                break
            for uid in item if isinstance(item, (list, tuple, set)) else [item]:
                uid = str(uid)
                if uid not in seen:
                    seen.add(uid)
                    buffer.append(uid)
            dispatch()
        dispatch(force=True)

        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            # The app is going away mid-scan. Cancelling this coroutine does not
            # touch the batch tasks it started, so without draining them here
            # their errors land in a task nobody is awaiting and surface as
            # "Task exception was never retrieved" after the UI has gone.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for outcome in results:
            if isinstance(outcome, AllRoutesFailed):
                raise outcome
        for outcome in results:
            if isinstance(outcome, BaseException):
                raise outcome

        # Belt and braces: every id that entered the stream must have a report.
        missing = [uid for uid in seen if uid not in reports]
        for uid in missing:
            reports[uid] = MemberReport(discord_id=uid, error="no response")
        if missing and on_progress:
            on_progress(f"{len(missing)} lookups unanswered", len(seen), len(seen))

        if on_progress:
            on_progress("Scan complete", len(seen), len(seen))
        return reports

    async def scan_members(
        self,
        discord_ids: Sequence[str],
        on_progress: ProgressCallback | None = None,
        on_partial: Callable[[list[MemberReport]], None] | None = None,
    ) -> dict[str, MemberReport]:
        """Scan a known set of ids. Thin wrapper over :meth:`scan_stream`."""
        ids = list(dict.fromkeys(str(i) for i in discord_ids))
        queue: asyncio.Queue = asyncio.Queue()
        for chunk in _chunks(ids, MAX_BATCH):
            queue.put_nowait(chunk)
        queue.put_nowait(None)
        return await self.scan_stream(
            queue, on_progress=on_progress, on_partial=on_partial
        )

    @property
    def unanswered(self) -> int:
        """Set by the caller after a scan; see :meth:`count_unanswered`."""
        return 0

    @staticmethod
    def count_unanswered(reports: dict[str, MemberReport]) -> int:
        return sum(1 for r in reports.values() if r.error)

    async def lookup_discord_user_detail(self, discord_id: str) -> dict:
        """Single-user GET; unlike the batch form this includes ``altAccounts``."""
        route = self.pool.pick()
        if route is None:
            raise AllRoutesFailed(self.pool.failure_summary())
        await route.limiter.acquire(1)
        async with self._sem:
            resp = await route.client.get(f"/v1/lookup/discord/user/{discord_id}")
        route.limiter.observe(resp.headers)
        if resp.status_code == 429:
            route.limiter.penalise(_retry_after(resp.headers))
            raise RotectorError("rate limited")
        resp.raise_for_status()
        body = resp.json()
        if not body.get("success"):
            raise RotectorError(body.get("error", "unknown error"))
        return body.get("data") or {}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _apply_flag(account: RobloxAccount, raw: dict | None) -> None:
    if not raw:
        return
    account.flag_type = _maybe_int(raw.get("flagType"))
    account.category = _maybe_int(raw.get("category"))
    confidence = raw.get("confidence")
    account.confidence = float(confidence) if confidence is not None else None
    account.reasons = raw.get("reasons") or {}
    account.last_updated = _maybe_int(raw.get("lastUpdated"))


def _chunks(items: Sequence, size: int):
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def _maybe_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _retry_after(headers) -> float | None:
    try:
        return float(headers.get("retry-after"))
    except (TypeError, ValueError):
        return None
