"""Okappiki API client -- an alternative backend to :mod:`rsb.rotector`.

    GET https://okappiki.com/backend/api.php
        ?action=vencord_full_check&discord_id=<one id>

One request answers for one Discord id, and the body carries three independent
sources at once::

    {"success": true, "discord_id": "...",
     "okappiki": {"flagged": true, "reason": "...",
                  "roblox_username": null, "roblox_id": null},
     "rotector": {"flagged": true, "roblox_id": 9092847610,
                  "username": "...", "flag_type": 2, "reason": "..."},
     "mococo":   {"flagged": true, "score_sum": 40, "reason": "..."},
     "last_updated": "2026-08-03T08:21:58+00:00"}

**None of this is documented**, so everything below is what the endpoint was
observed doing rather than what it promises. Four observations shape the code:

* *There is no batch form.* ``discord_ids``, comma-separated values and repeated
  parameters are all rejected. A ten-thousand-member server is ten thousand
  requests, against Rotector's ~218 for the same server, and the defaults in
  :class:`~rsb.config.OkappikiConfig` are slow because the service publishes no
  rate limit to respect.
* *Failures come back HTTP 200.* An unusable id returns
  ``{"success": false, "message": "Invalid Discord ID"}`` with a 200, so the
  status code cannot be trusted and ``success`` is what gets checked.
* *Only the flag is guaranteed.* An unflagged member's source object is exactly
  ``{"flagged": false}`` -- no reason, no ids, no nulls. Every other key is read
  as optional.
* *Ids are not normalised consistently.* ``00001212568604604629013`` returns the
  same Rotector record as the canonical id while Okappiki's own list reports
  nothing, so the two sub-lookups disagree about what that id is. Ids are
  normalised here, before the request, rather than trusting either.

Verdict tiering follows :mod:`rsb.verdict`'s existing rules. Only Rotector
publishes flag types with documented actionability, so only Rotector's flags
reach THREAT; an Okappiki or mococo sighting alone is CAUTION, which is the
verdict that already means "a person should look at this before acting".
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable, Sequence

import httpx

from .proxy import AllRoutesFailed, RoutePool
from .ratelimit import RateLimiter
from .rotector import MemberReport, ProgressCallback, RobloxAccount, _absorb
from .verdict import Signal, Verdict, verdict_for_flag

BASE_URL = "https://okappiki.com"
API_PATH = "/backend/api.php"
ACTION = "vencord_full_check"
USER_AGENT = "rotector-selfbot/1.0 (+https://github.com/mireyacs/rotector-selfbot)"

#: Rotector's Terms of Use cap retention at 24h and an Okappiki response embeds
#: a Rotector verdict, so the same ceiling is applied to this cache.
MAX_CACHE_TTL = 23 * 3600

#: the sources a response can carry, in the order they are shown
SOURCES = ("rotector", "okappiki", "mococo")

#: Discord's epoch puts every real snowflake at 17 digits or more. The endpoint
#: happily answers for "1" -- with a confident-looking Rotector record -- so
#: anything shorter is refused here instead of being reported as a finding.
MIN_SNOWFLAKE_DIGITS = 17

#: seconds of quiet before a partial batch is sent anyway; kept for interface
#: parity with the Rotector client, which buffers ids into batches of 100
IDLE_FLUSH = 2.0


class OkappikiError(RuntimeError):
    pass


def normalise_id(raw) -> str | None:
    """A canonical snowflake, or ``None`` if it is not one.

    Leading zeros are stripped because the endpoint's two sub-lookups disagree
    about whether ``0001234`` is ``1234``.
    """
    text = str(raw).strip()
    if not text.isdigit():
        return None
    text = text.lstrip("0") or "0"
    if len(text) < MIN_SNOWFLAKE_DIGITS:
        return None
    return text


def _text(value) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _maybe_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_signals(body: dict) -> list[Signal]:
    """Turn one response body into one signal per source that answered.

    Sources absent from the body are absent from the result: not answering and
    answering "not flagged" are different facts, and flattening them would let
    a backend outage read as a clean result.
    """
    signals: list[Signal] = []
    for source in SOURCES:
        raw = body.get(source)
        if not isinstance(raw, dict):
            continue
        flagged = bool(raw.get("flagged"))
        flag_type = _maybe_int(raw.get("flag_type"))

        if not flagged:
            verdict = Verdict.NO_DETECTIONS
        elif source == "rotector":
            # the one source with documented flag types; verdict_for_flag holds
            # the rule that only Flagged and Confirmed are actionable
            verdict = verdict_for_flag(flag_type)
        else:
            # undocumented, unverified, and no flag type to grade it by
            verdict = Verdict.CAUTION

        signals.append(
            Signal(
                source=source,
                flagged=flagged,
                verdict=verdict,
                reason=_text(raw.get("reason")),
                score=_maybe_int(raw.get("score_sum")),
                roblox_id=_maybe_int(raw.get("roblox_id")),
                # the sources disagree on the key: Okappiki's own object says
                # "roblox_username", the embedded Rotector one says "username"
                roblox_username=(
                    _text(raw.get("roblox_username")) or _text(raw.get("username"))
                    or None
                ),
                flag_type=flag_type,
            )
        )
    return signals


def parse_report(discord_id: str, body: dict) -> MemberReport:
    """Build a report from one response body.

    Any source naming a Roblox account also produces a :class:`RobloxAccount`,
    so the results table and the export columns -- both written against
    Rotector's shape -- have the same thing to show they always had.
    """
    report = MemberReport(discord_id=discord_id)
    report.signals = parse_signals(body)

    for signal in report.signals:
        if not signal.flagged or signal.roblox_id is None:
            continue
        report.accounts.append(
            RobloxAccount(
                user_id=signal.roblox_id,
                username=signal.roblox_username or f"({signal.source})",
                flag_type=signal.flag_type,
                sources=[],
                reasons=(
                    {f"{signal.label} finding": {"message": signal.reason}}
                    if signal.reason
                    else {}
                ),
            )
        )
    return report


class OkappikiClient:
    """Mirrors :class:`~rsb.rotector.RotectorClient`'s surface.

    The app holds one backend and does not care which; anything it reaches for
    -- ``scan_stream``, ``pool``, ``limiter``, ``estimate_seconds`` -- exists
    on both. The differences are all inside: no API key, no second hop, and no
    batching, so ids are dispatched one at a time under a semaphore instead of
    being buffered into hundreds.
    """

    def __init__(
        self,
        limiter: RateLimiter | None = None,
        cache_ttl: int = 3600,
        concurrency: int = 2,
        max_retries: int = 4,
        timeout: float = 30.0,
        proxies: list[str] | None = None,
        direct_as_fallback: bool = False,
        base_url: str = BASE_URL,
        scan_concurrency: int | None = None,
    ) -> None:
        self.limiter = limiter or RateLimiter(limit=5, window=1.0, reserve=0)
        self.cache_ttl = min(max(0, cache_ttl), MAX_CACHE_TTL)
        self.max_retries = max_retries

        headers = {"user-agent": USER_AGENT, "accept": "application/json"}
        self.base_url = base_url
        self._http = httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=timeout
        )
        self.pool = RoutePool.build(
            direct_client=self._http,
            direct_limiter=self.limiter,
            proxies=proxies,
            headers=headers,
            timeout=timeout,
            direct_as_fallback=direct_as_fallback,
            base_url=base_url,
        )

        routes = len(self.pool.routes)
        self.scan_concurrency = scan_concurrency or max(concurrency, routes * 2)
        self._sem = asyncio.Semaphore(max(self.scan_concurrency, concurrency))
        self._cache: dict[str, tuple[float, dict]] = {}

    async def aclose(self) -> None:
        await self.pool.aclose()
        await self._http.aclose()

    async def __aenter__(self) -> "OkappikiClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    def capacity_units_per_sec(self) -> float:
        return self.pool.capacity_units_per_sec()

    def estimate_seconds(self, members: int) -> float | None:
        """One request per member, one hop, no batching -- so this is division.

        Deliberately not routed through :func:`rsb.eta.estimate_scan_seconds`,
        which models Rotector's batching and its second pass and would predict
        a scan roughly a hundred times faster than this backend can manage.
        """
        capacity = self.capacity_units_per_sec()
        if members <= 0 or capacity <= 0:
            return None
        return members / capacity

    # -- caching -----------------------------------------------------------

    def _cache_get(self, key: str) -> dict | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if time.time() - stored_at > self.cache_ttl:
            self._cache.pop(key, None)
            return None
        return value

    def purge_cache(self) -> None:
        self._cache.clear()

    # -- transport ---------------------------------------------------------

    async def _get(self, discord_id: str) -> dict:
        """One rate-limited GET, routed over the healthiest available path.

        Mirrors the Rotector client's two-axis bound: route failures park the
        route and move on until every route has been tried, while 429s and 5xx
        keep the route but are capped so a throttled endpoint raises rather
        than looping.
        """
        params = {"action": ACTION, "discord_id": discord_id}
        attempts: list[tuple[str, str]] = []
        tried: set[str] = set()
        soft_retries = 0
        max_soft = self.max_retries + 4

        while True:
            route = self.pool.pick(exclude=tried)
            if route is None:
                raise AllRoutesFailed(attempts or self.pool.failure_summary())

            await route.limiter.acquire(1)
            async with self._sem:
                try:
                    resp = await route.client.get(API_PATH, params=params)
                except httpx.HTTPError as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    route.penalise(error)
                    attempts.append((route.name, error))
                    tried.add(route.name)
                    continue

            route.limiter.observe(resp.headers)

            if resp.status_code == 429:
                route.limiter.penalise(None)
                soft_retries += 1
                if soft_retries > max_soft:
                    raise OkappikiError(
                        f"still rate limited after {soft_retries} retries"
                    )
                continue

            if resp.status_code in (403, 407, 502, 503) and not route.is_direct:
                error = f"HTTP {resp.status_code} via proxy"
                route.penalise(error)
                attempts.append((route.name, error))
                tried.add(route.name)
                continue

            if resp.status_code >= 500:
                soft_retries += 1
                if soft_retries > max_soft:
                    raise OkappikiError(
                        f"server error {resp.status_code} after {soft_retries} retries"
                    )
                await asyncio.sleep(min(2**soft_retries, 15))
                continue

            if resp.status_code >= 400:
                raise OkappikiError(f"HTTP {resp.status_code}: {resp.text[:200]}")

            try:
                body = resp.json()
            except ValueError:
                error = "response was not JSON (intercepting proxy?)"
                route.penalise(error)
                attempts.append((route.name, error))
                tried.add(route.name)
                continue

            route.recover()
            if not isinstance(body, dict):
                raise OkappikiError("response was not a JSON object")
            if not body.get("success"):
                # a 200 carrying a refusal; the message is the only detail given
                raise OkappikiError(_text(body.get("message")) or "request refused")
            return body

    # -- endpoints ---------------------------------------------------------

    async def lookup_member(self, discord_id: str) -> MemberReport:
        """One member, cached. Never raises: a failure becomes a report error."""
        canonical = normalise_id(discord_id)
        if canonical is None:
            return MemberReport(
                discord_id=str(discord_id),
                error=f"not a Discord id: {str(discord_id)[:32]!r}",
            )

        cached = self._cache_get(canonical)
        if cached is not None:
            return parse_report(str(discord_id), cached)

        try:
            body = await self._get(canonical)
        except AllRoutesFailed:
            raise
        except Exception as exc:  # noqa: BLE001
            return MemberReport(discord_id=str(discord_id), error=str(exc))

        if self.cache_ttl:
            self._cache[canonical] = (time.time(), body)
        return parse_report(str(discord_id), body)

    async def lookup_discord_user_detail(self, discord_id: str) -> dict:
        """The raw body, for the detail pane. Unlike Rotector there is no
        richer single-user form -- this is the same endpoint a scan uses."""
        canonical = normalise_id(discord_id)
        if canonical is None:
            raise OkappikiError(f"not a Discord id: {str(discord_id)[:32]!r}")
        return await self._get(canonical)

    # -- scanning ----------------------------------------------------------

    async def scan_stream(
        self,
        incoming: "asyncio.Queue",
        on_progress: ProgressCallback | None = None,
        on_partial: Callable[[list[MemberReport]], None] | None = None,
        idle_flush: float | None = IDLE_FLUSH,
    ) -> dict[str, MemberReport]:
        """Scan ids as they arrive.

        Same contract as the Rotector client's: ``incoming`` yields ids singly
        or in lists, ``None`` closes the stream, deduplication is global, and
        every id that arrives is guaranteed a report. ``idle_flush`` is accepted
        and ignored -- there is no batch to flush, so an id is dispatched the
        moment it turns up.
        """
        reports: dict[str, MemberReport] = {}
        seen: set[str] = set()
        tasks: list[asyncio.Task] = []
        lock = asyncio.Lock()
        done = 0

        async def handle(discord_id: str) -> None:
            report = await self.lookup_member(discord_id)
            nonlocal done
            async with lock:
                done += 1
                reports[discord_id] = report
                if on_progress:
                    stage = (
                        "Some lookups failed" if report.error else "Checking members"
                    )
                    on_progress(stage, done, len(seen))
                if on_partial:
                    on_partial([report])

        def submit(discord_id: str) -> None:
            if discord_id in seen:
                return
            seen.add(discord_id)
            task = asyncio.create_task(handle(discord_id))
            task.add_done_callback(_absorb)
            tasks.append(task)

        while True:
            item = await incoming.get()
            if item is None:
                break
            if isinstance(item, (list, tuple, set)):
                for one in item:
                    submit(str(one))
            else:
                submit(str(item))

        if tasks:
            # return_exceptions, then re-raise: a bare gather leaves the *other*
            # tasks' errors unretrieved the moment one of them fails, and a
            # cancellation leaves all of them running. Either way the traceback
            # arrives later, from a task nobody is awaiting.
            try:
                results = await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
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
        return reports

    async def scan_members(
        self,
        discord_ids: Sequence[str],
        on_progress: ProgressCallback | None = None,
        on_partial: Callable[[list[MemberReport]], None] | None = None,
    ) -> dict[str, MemberReport]:
        ids = list(dict.fromkeys(str(i) for i in discord_ids))
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait(ids)
        queue.put_nowait(None)
        return await self.scan_stream(
            queue, on_progress=on_progress, on_partial=on_partial
        )

    @property
    def unanswered(self) -> int:
        return 0

    @staticmethod
    def count_unanswered(reports: dict[str, MemberReport]) -> int:
        return sum(1 for r in reports.values() if r.error)


def lookup_ids(reports: Iterable[MemberReport]) -> list[str]:
    """Ids whose report carries at least one flagged source."""
    return [r.discord_id for r in reports if r.flagged_signals]
