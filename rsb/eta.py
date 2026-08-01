"""Throughput measurement and time-remaining estimates.

Two different questions get answered here:

* *Before* a scan -- "roughly how long will this server take?"  Answered
  analytically from the member count and the request budget available across
  every healthy route.
* *During* a scan -- "how much longer?"  Answered from measured throughput over
  a trailing window, so a rate-limit stall or a slow proxy is reflected within
  seconds rather than being averaged away over the whole run.
"""

from __future__ import annotations

import time
from collections import deque

from .ratelimit import batch_cost

MAX_BATCH = 100

#: Fraction of Discord members assumed to have a Roblox account Rotector knows
#: about.  Only used for the pre-scan estimate -- once a scan starts the live
#: estimator measures reality and this stops mattering.
DEFAULT_LINK_RATE = 0.25


def format_duration(seconds: float | None) -> str:
    """``95`` -> ``"1m 35s"``. Coarse on purpose; this is an estimate."""
    if seconds is None:
        return "?"
    seconds = max(0.0, float(seconds))
    if seconds < 1:
        return "<1s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds + 0.5), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def units_for_ids(n_ids: int) -> int:
    """Request units a batched lookup of ``n_ids`` ids costs in total."""
    if n_ids <= 0:
        return 0
    full, remainder = divmod(n_ids, MAX_BATCH)
    total = full * batch_cost(MAX_BATCH)
    if remainder:
        total += batch_cost(remainder)
    return total


def estimate_scan_seconds(
    members: int,
    capacity_units_per_sec: float,
    link_rate: float = DEFAULT_LINK_RATE,
) -> float | None:
    """Predict how long the Rotector half of a scan will take.

    The Discord hop is exact -- one lookup per member.  The Roblox hop depends
    on how many members turn out to have linked accounts, which is not knowable
    up front, so ``link_rate`` stands in for it.
    """
    if members <= 0 or capacity_units_per_sec <= 0:
        return None
    linked = int(round(members * max(0.0, link_rate)))
    total_units = units_for_ids(members) + units_for_ids(linked)
    return total_units / capacity_units_per_sec


class RateEstimator:
    """Trailing-window throughput estimator.

    A whole-run average would hide a stall: after ten fast minutes, ten stalled
    seconds barely move it.  Sampling only the recent window means the ETA
    reacts to a rate-limit hold or a dead proxy almost immediately.
    """

    def __init__(
        self,
        window: float = 45.0,
        min_samples: int = 3,
        min_elapsed: float = 2.0,
    ) -> None:
        self.window = window
        self.min_samples = min_samples
        self.min_elapsed = min_elapsed
        self._samples: deque[tuple[float, int]] = deque()
        self._started: float | None = None

    def reset(self) -> None:
        self._samples.clear()
        self._started = None

    def update(self, done: int) -> None:
        now = time.monotonic()
        if self._started is None:
            self._started = now
        self._samples.append((now, done))
        cutoff = now - self.window
        # keep one sample older than the window so short scans still have a base
        while len(self._samples) > 2 and self._samples[1][0] < cutoff:
            self._samples.popleft()

    @property
    def elapsed(self) -> float:
        if self._started is None:
            return 0.0
        return time.monotonic() - self._started

    @property
    def rate(self) -> float | None:
        """Items per second over the window, or None if not yet meaningful."""
        if len(self._samples) < self.min_samples:
            return None
        (t0, d0), (t1, d1) = self._samples[0], self._samples[-1]
        span = t1 - t0
        progressed = d1 - d0
        if span < self.min_elapsed or progressed <= 0:
            return None
        return progressed / span

    def eta(self, done: int, total: int) -> float | None:
        """Seconds remaining, or None while the rate is still unknown."""
        remaining = total - done
        if remaining <= 0:
            return 0.0
        rate = self.rate
        if not rate:
            return None
        return remaining / rate

    def describe(self, done: int, total: int) -> str:
        """``"ETA 2m 30s (140/s)"``, degrading gracefully early on."""
        eta = self.eta(done, total)
        rate = self.rate
        if eta is None:
            return "ETA --"
        if rate and rate >= 1:
            return f"ETA {format_duration(eta)} ({rate:,.0f}/s)"
        return f"ETA {format_duration(eta)}"
