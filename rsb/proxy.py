"""Outbound routes: configured proxies plus the user's direct connection.

Rotector's rate limit is scoped per IP, so every route carries its own
:class:`~rsb.ratelimit.RateLimiter` -- a budget spent through one proxy does not
consume another's.

    NOTE ON TERMS OF USE.  Rotector's terms prohibit "circumventing rate limits
    through multiple keys, rotating IPs, or other means".  Fanning a scan out
    across proxies does exactly that.  This module is therefore opt-in and
    disabled by default; the sanctioned way to raise throughput is an API key
    from panel.rotector.com, which lifts the limit on a single connection.

Routing policy:

* Healthy proxies are preferred, ordered by whichever has the most rate-limit
  headroom right now, so work spreads instead of hammering the first entry.
* A proxy that errors is put in exponential backoff and skipped; the request is
  immediately retried on another route.
* The direct connection is the fallback once no proxy is usable (or a co-equal
  route if ``direct_as_fallback`` is off).
* If every route including direct has failed, :class:`AllRoutesFailed` is
  raised carrying each route's own error, so the UI can explain precisely what
  went wrong rather than saying "network error".
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import httpx

from .ratelimit import RateLimiter

#: consecutive failures before a route is parked
DEFAULT_MAX_FAILURES = 3
#: backoff schedule for a parked route
BACKOFF_BASE = 5.0
BACKOFF_MAX = 300.0

DIRECT_NAME = "direct"


class AllRoutesFailed(RuntimeError):
    """Every route, including the direct connection, is unusable."""

    def __init__(self, attempts: list[tuple[str, str]]) -> None:
        self.attempts = attempts
        if attempts:
            detail = "\n".join(f"  - {name}: {err}" for name, err in attempts)
        else:
            detail = "  - no routes were configured"
        super().__init__(f"every route failed:\n{detail}")

    @property
    def direct_error(self) -> str | None:
        for name, err in self.attempts:
            if name == DIRECT_NAME:
                return err
        return None


def parse_proxy(raw: str) -> str | None:
    """Normalise the proxy spellings that appear in the wild into a URL.

    Accepts ``scheme://[user:pass@]host:port``, ``host:port``,
    ``user:pass@host:port`` and the ``host:port:user:pass`` form that proxy
    vendors hand out.  Returns None for blanks and comments.
    """
    raw = (raw or "").strip()
    if not raw or raw.startswith("#"):
        return None

    if "://" in raw:
        scheme, _, rest = raw.partition("://")
        scheme = scheme.lower()
        if scheme not in ("http", "https", "socks4", "socks5", "socks5h"):
            return None
        if not rest:
            return None
        return f"{scheme}://{rest}"

    # bare forms, all assumed http
    if "@" in raw:
        return f"http://{raw}"

    parts = raw.split(":")
    if len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    if len(parts) == 4:
        host, port, user, password = parts
        return f"http://{user}:{password}@{host}:{port}"
    return None


def proxy_label(url: str) -> str:
    """``http://u:p@1.2.3.4:8080`` -> ``1.2.3.4:8080``, credentials dropped."""
    try:
        scheme, _, rest = url.partition("://")
        host = rest.rsplit("@", 1)[-1]
        prefix = "" if scheme in ("http", "https") else f"{scheme}://"
        return f"{prefix}{host}"
    except Exception:
        return url


@dataclass
class RouteStatus:
    """Snapshot for display."""

    name: str
    is_direct: bool
    healthy: bool
    in_backoff: bool
    backoff_for: float
    failures: int
    last_error: str | None
    latency_ms: float | None
    budget_available: int
    budget_limit: int


class Route:
    """One outbound path, with its own client, rate budget and health."""

    def __init__(
        self,
        name: str,
        client: httpx.AsyncClient,
        limiter: RateLimiter,
        *,
        is_direct: bool = False,
        url: str | None = None,
        max_failures: int = DEFAULT_MAX_FAILURES,
        owns_client: bool = True,
    ) -> None:
        self.name = name
        self.client = client
        self.limiter = limiter
        self.is_direct = is_direct
        self.url = url
        self.max_failures = max_failures
        self.owns_client = owns_client

        self.failures = 0
        self.disabled_until = 0.0
        self.last_error: str | None = None
        self.latency_ms: float | None = None

    # -- health ------------------------------------------------------------

    @property
    def in_backoff(self) -> bool:
        return time.time() < self.disabled_until

    @property
    def healthy(self) -> bool:
        """Whether the route has been behaving, independent of backoff."""
        return self.failures < self.max_failures

    def available(self) -> bool:
        """Usable *right now*."""
        return not self.in_backoff

    def penalise(self, error: str) -> None:
        """Park the route, with backoff that escalates on repeat failures.

        Parking happens on the *first* failure rather than after N strikes: a
        proxy that hangs costs a full connect timeout every time it is tried,
        so retrying it two more times before benching it would stall the scan
        for a minute per request.
        """
        self.failures += 1
        self.last_error = error
        step = min(self.failures - 1, 6)
        delay = min(BACKOFF_BASE * (2**step), BACKOFF_MAX)
        self.disabled_until = time.time() + delay

    def recover(self) -> None:
        """A success clears the strike count."""
        if self.failures or self.disabled_until:
            self.failures = 0
            self.disabled_until = 0.0

    def status(self) -> RouteStatus:
        budget = self.limiter.snapshot()
        return RouteStatus(
            name=self.name,
            is_direct=self.is_direct,
            healthy=self.healthy,
            in_backoff=self.in_backoff,
            backoff_for=max(0.0, self.disabled_until - time.time()),
            failures=self.failures,
            last_error=self.last_error,
            latency_ms=self.latency_ms,
            budget_available=budget.available,
            budget_limit=max(1, budget.limit - budget.reserve),
        )

    async def aclose(self) -> None:
        if self.owns_client:
            await self.client.aclose()


class RoutePool:
    """Picks routes, tracks their health, and reports why everything failed."""

    def __init__(
        self,
        routes: list[Route],
        direct_as_fallback: bool = True,
    ) -> None:
        self.routes = routes
        self.direct_as_fallback = direct_as_fallback
        self._cursor = 0

    # -- construction ------------------------------------------------------

    @classmethod
    def build(
        cls,
        direct_client: httpx.AsyncClient,
        direct_limiter: RateLimiter,
        proxies: list[str] | None = None,
        *,
        headers: dict | None = None,
        timeout: float = 30.0,
        direct_as_fallback: bool = True,
        max_failures: int = DEFAULT_MAX_FAILURES,
        base_url: str = "",
        limiter_template: RateLimiter | None = None,
    ) -> "RoutePool":
        template = limiter_template or direct_limiter
        routes: list[Route] = []

        for raw in proxies or []:
            url = parse_proxy(raw)
            if url is None:
                continue
            client = httpx.AsyncClient(
                base_url=base_url,
                headers=headers,
                timeout=timeout,
                proxy=url,
            )
            routes.append(
                Route(
                    name=proxy_label(url),
                    client=client,
                    limiter=RateLimiter(
                        limit=template.limit,
                        window=template.window,
                        reserve=template.reserve,
                    ),
                    url=url,
                    max_failures=max_failures,
                )
            )

        # The direct route reuses the caller's client so that anything already
        # holding it (tests, instrumentation) keeps working.
        routes.append(
            Route(
                name=DIRECT_NAME,
                client=direct_client,
                limiter=direct_limiter,
                is_direct=True,
                max_failures=max_failures,
                owns_client=False,
            )
        )
        return cls(routes, direct_as_fallback=direct_as_fallback)

    # -- selection ---------------------------------------------------------

    @property
    def proxies(self) -> list[Route]:
        return [r for r in self.routes if not r.is_direct]

    @property
    def direct(self) -> Route | None:
        return next((r for r in self.routes if r.is_direct), None)

    def pick(self, exclude: set[str] | None = None) -> Route | None:
        """Best route to use right now, or None if nothing is usable."""
        exclude = exclude or set()

        def usable(routes: list[Route]) -> list[Route]:
            return [r for r in routes if r.name not in exclude and r.available()]

        candidates = usable(self.proxies)
        if not self.direct_as_fallback:
            direct = self.direct
            if direct is not None and direct.name not in exclude and direct.available():
                candidates.append(direct)

        if not candidates:
            # fall back to the direct connection
            direct = self.direct
            if direct is not None and direct.name not in exclude and direct.available():
                return direct
            return None

        # Prefer whoever has the most budget; rotate on ties so load spreads.
        self._cursor = (self._cursor + 1) % max(1, len(candidates))
        ordered = sorted(
            enumerate(candidates),
            key=lambda pair: (
                -pair[1].limiter.snapshot().available,
                (pair[0] - self._cursor) % len(candidates),
            ),
        )
        return ordered[0][1]

    # -- reporting ---------------------------------------------------------

    def failure_summary(self) -> list[tuple[str, str]]:
        return [
            (r.name, r.last_error or ("in backoff" if r.in_backoff else "unavailable"))
            for r in self.routes
        ]

    def snapshot(self) -> list[RouteStatus]:
        return [r.status() for r in self.routes]

    def available_count(self) -> int:
        """Routes usable right now (not parked)."""
        return sum(1 for r in self.routes if r.available())

    def healthy_count(self) -> int:
        return self.available_count()

    def capacity_units_per_sec(self) -> float:
        """Combined request-unit throughput of every usable route."""
        total = 0.0
        for route in self.routes:
            if not route.available():
                continue
            if route.is_direct and self.direct_as_fallback and self.proxies:
                # only a standby; do not promise its throughput
                continue
            snap = route.limiter.snapshot()
            total += max(1, snap.limit - snap.reserve) / max(0.001, route.limiter.window)
        if total <= 0:
            direct = self.direct
            if direct is not None:
                snap = direct.limiter.snapshot()
                total = max(1, snap.limit - snap.reserve) / max(0.001, direct.limiter.window)
        return total

    async def aclose(self) -> None:
        await asyncio.gather(
            *(r.aclose() for r in self.routes), return_exceptions=True
        )


# --------------------------------------------------------------------------
# probing (used by the proxy tester)
# --------------------------------------------------------------------------

#: probes go straight at the API we actually care about
ROTECTOR_PROBE = "https://roscoe.rotector.com/v1/lookup/discord/user/1"


@dataclass
class ProbeResult:
    raw: str
    url: str | None
    label: str
    ok: bool = False
    latency_ms: float | None = None
    status: int | None = None
    rate_limit: int | None = None
    rate_remaining: int | None = None
    rate_reset: float | None = None
    error: str | None = None
    #: requests *we* already spent on this exit in the current window
    own_prior: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def used_in_window(self) -> int | None:
        """Requests already spent against this exit IP's current window."""
        if self.rate_limit is None or self.rate_remaining is None:
            return None
        return self.rate_limit - self.rate_remaining

    @property
    def independent_budget(self) -> bool | None:
        """Whether this proxy brought its own, untouched rate budget.

        Our probe is one request, plus ``own_prior`` we already spent on this
        exit earlier in the same window (a retest, say).  Anything beyond that
        means something *else* is drawing on the same budget -- another proxy
        behind the same exit, or unrelated traffic from that IP -- so it adds
        less capacity than it appears to.
        """
        used = self.used_in_window
        if used is None:
            return None
        return used <= 1 + self.own_prior

    @property
    def verdict(self) -> str:
        if not self.ok:
            return "FAIL"
        if self.status is None or self.status >= 400:
            return "NO API"
        if self.independent_budget is False:
            return "SHARED"
        return "OK"


async def probe_proxy(
    raw: str,
    *,
    timeout: float = 20.0,
    user_agent: str = "rotector-selfbot/1.0",
    probe_url: str = ROTECTOR_PROBE,
    own_prior: int = 0,
) -> ProbeResult:
    """Test one proxy against the Rotector API itself.

    Everything measured comes from the API this tool actually uses -- no
    third-party IP echo service is involved, so a proxy that reaches the
    internet but is blocked by Rotector is correctly reported as useless
    rather than as working.

    ``raw`` may be any form :func:`parse_proxy` accepts; pass ``"direct"`` to
    probe the user's own connection.
    """
    is_direct = raw.strip().lower() == DIRECT_NAME
    url = None if is_direct else parse_proxy(raw)
    label = DIRECT_NAME if is_direct else (proxy_label(url) if url else raw.strip())
    result = ProbeResult(raw=raw, url=url, label=label, own_prior=own_prior)

    if not is_direct and url is None:
        result.error = "unrecognised proxy format"
        return result

    kwargs = {
        "timeout": timeout,
        "headers": {"user-agent": user_agent, "accept": "application/json"},
    }
    if url:
        kwargs["proxy"] = url

    try:
        async with httpx.AsyncClient(**kwargs) as client:
            started = time.monotonic()
            try:
                resp = await client.get(probe_url)
            except httpx.HTTPError as exc:
                result.error = f"{type(exc).__name__}: {exc}"
                return result

            result.latency_ms = (time.monotonic() - started) * 1000
            result.ok = True
            result.status = resp.status_code
            result.rate_limit = _header_int(resp, "x-ratelimit-limit")
            result.rate_remaining = _header_int(resp, "x-ratelimit-remaining")
            reset = resp.headers.get("x-ratelimit-reset")
            try:
                result.rate_reset = float(reset) if reset else None
            except ValueError:
                result.rate_reset = None

            if resp.status_code == 403:
                result.notes.append("Rotector refused this exit IP")
            elif resp.status_code == 429:
                result.notes.append("exit IP is already rate limited")
            elif resp.status_code >= 400:
                result.notes.append(f"Rotector answered HTTP {resp.status_code}")
            elif result.independent_budget is False:
                others = (result.used_in_window or 0) - 1 - result.own_prior
                result.notes.append(
                    f"{others} other request(s) already on this exit's window - "
                    f"shares a budget, so adds little capacity"
                )
            elif result.rate_limit:
                result.notes.append(f"own budget: {result.rate_limit}/window")
    except Exception as exc:  # noqa: BLE001 - proxy setup itself can explode
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"

    return result


def _header_int(resp: httpx.Response, name: str) -> int | None:
    try:
        return int(resp.headers[name])
    except (KeyError, TypeError, ValueError):
        return None


def summarise_pool(results: list[ProbeResult]) -> dict:
    """Aggregate what a probed pool is actually worth."""
    usable = [r for r in results if r.verdict in ("OK", "SHARED")]
    independent = [r for r in results if r.verdict == "OK"]
    budget = sum(r.rate_limit or 0 for r in independent)
    return {
        "total": len(results),
        "usable": len(usable),
        "independent": len(independent),
        "shared": sum(1 for r in results if r.verdict == "SHARED"),
        "failed": sum(1 for r in results if r.verdict == "FAIL"),
        "no_api": sum(1 for r in results if r.verdict == "NO API"),
        "combined_budget": budget,
    }
