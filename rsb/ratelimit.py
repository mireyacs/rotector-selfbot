"""Sliding-window rate limiter for the Rotector API.

The documented budget is **50 requests per 10 second window, scoped per IP**
(or per key, if an API key is supplied).  Batch endpoints cost more than one
request: measured against the live API, a batch of N ids costs
``ceil(N / 50)`` request units -- a 100-id batch costs 2, anything up to 50
costs 1.

Three layers keep us inside the window:

1. A local sliding window that refuses to spend more than ``limit - reserve``
   units in any ``window`` seconds.  This is what actually paces requests.
2. Header sync.  Every response carries ``X-RateLimit-Remaining`` and
   ``X-RateLimit-Reset``; if the server thinks we have less headroom than we
   do (another process sharing the IP, say), we adopt the server's view and
   hard-block until the window resets.
3. ``Retry-After`` on a 429 is honoured exactly, and blocks every caller.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass

#: how many ids fit into a single request unit on the batch endpoints
IDS_PER_UNIT = 50


def batch_cost(n_ids: int) -> int:
    """Request units a batch of ``n_ids`` ids will cost."""
    return max(1, math.ceil(n_ids / IDS_PER_UNIT))


@dataclass(frozen=True)
class LimiterState:
    """Snapshot for the status bar."""

    used: int
    limit: int
    reserve: int
    blocked_for: float
    server_remaining: int | None

    @property
    def available(self) -> int:
        return max(0, self.limit - self.reserve - self.used)


class RateLimiter:
    """Async sliding-window limiter. Safe to share across tasks."""

    def __init__(
        self,
        limit: int = 50,
        window: float = 10.0,
        reserve: int = 5,
    ) -> None:
        if reserve >= limit:
            raise ValueError("reserve must be smaller than limit")
        self.limit = limit
        self.window = window
        self.reserve = reserve
        self._events: deque[tuple[float, int]] = deque()
        self._blocked_until = 0.0
        self._server_remaining: int | None = None
        self._lock = asyncio.Lock()

    # -- internals ---------------------------------------------------------

    @property
    def _effective_limit(self) -> int:
        return self.limit - self.reserve

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        while self._events and self._events[0][0] <= cutoff:
            self._events.popleft()

    def _used(self, now: float) -> int:
        self._prune(now)
        return sum(cost for _, cost in self._events)

    # -- public API --------------------------------------------------------

    async def acquire(self, cost: int = 1) -> None:
        """Block until ``cost`` request units can be spent, then spend them."""
        async with self._lock:
            while True:
                now = time.time()

                if now < self._blocked_until:
                    await asyncio.sleep(self._blocked_until - now)
                    continue

                used = self._used(now)
                # `not self._events` lets an oversized cost through rather than
                # deadlocking; in practice cost is never above 2.
                if used + cost <= self._effective_limit or not self._events:
                    self._events.append((now, cost))
                    return

                # Sleep until the oldest event falls out of the window.
                wait = self._events[0][0] + self.window - now
                await asyncio.sleep(max(wait, 0.01))

    def observe(self, headers) -> None:
        """Reconcile with the server's own accounting after a response."""
        limit = _int(headers.get("x-ratelimit-limit"))
        remaining = _int(headers.get("x-ratelimit-remaining"))
        reset = _float(headers.get("x-ratelimit-reset"))

        if limit and limit > 0:
            self.limit = limit
            if self.reserve >= limit:
                self.reserve = max(0, limit // 10)

        if remaining is None:
            return
        self._server_remaining = remaining

        # The server is authoritative. If it says we are at or below our
        # safety reserve, stop dead until the window rolls over.
        if remaining <= self.reserve and reset:
            self._blocked_until = max(self._blocked_until, reset + 0.25)

    def penalise(self, retry_after: float | None) -> None:
        """Apply the ``Retry-After`` from a 429 to every waiting caller."""
        delay = retry_after if retry_after and retry_after > 0 else self.window
        self._blocked_until = max(self._blocked_until, time.time() + delay)
        self._server_remaining = 0

    def snapshot(self) -> LimiterState:
        now = time.time()
        return LimiterState(
            used=self._used(now),
            limit=self.limit,
            reserve=self.reserve,
            blocked_for=max(0.0, self._blocked_until - now),
            server_remaining=self._server_remaining,
        )


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
