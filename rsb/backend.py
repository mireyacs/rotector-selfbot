"""Picks the lookup backend a scan runs against.

The app holds exactly one backend and does not branch on which. Both clients
expose the same surface -- ``scan_stream``, ``scan_members``, ``limiter``,
``pool``, ``estimate_seconds``, ``purge_cache``, ``aclose`` -- so everything
that differs between them stays behind this function.

They are alternatives rather than layers. Okappiki's response happens to carry
a Rotector verdict alongside its own two sources, so choosing it does not mean
giving Rotector's answer up; it means asking a different service for it, with
a different rate ceiling and no batch endpoint.
"""

from __future__ import annotations

from .config import BACKENDS, Config
from .okappiki import OkappikiClient
from .ratelimit import RateLimiter
from .rotector import RotectorClient

#: what each backend is called in the UI, and where its data comes from
LABELS = {
    "rotector": "Rotector",
    "okappiki": "Okappiki",
}

ATTRIBUTIONS = {
    "rotector": "Data: Rotector (https://rotector.com)",
    "okappiki": (
        "Data: Okappiki (https://okappiki.com), which reports Okappiki, "
        "Rotector and mococo findings together"
    ),
}


def backend_label(name: str) -> str:
    return LABELS.get(name, name or "?")


def effective_cache_ttl(config: Config, configured: int) -> int:
    """The cache lifetime a client should be built with, in seconds.

    While ``scan.retain_beyond_terms`` is off this is exactly the configured
    ``cache_ttl``, which the client then clamps under the 24-hour ceiling
    Rotector's Terms of Use set. With it on, the operator has said how long
    findings should be kept in ``scan.retention_hours``, and that is the number
    that decides -- a one-hour cache would otherwise throw the data away long
    before the window they asked for was up, which is not what switching the
    setting on was for.
    """
    if not config.scan.retain_beyond_terms:
        return configured
    return max(0, int(config.scan.retention_hours)) * 3600


def build_backend(config: Config, proxies: list[str] | None = None):
    """The client for ``config.scan.backend``.

    An unrecognised name raises rather than quietly falling back to Rotector:
    a typo in ``config.toml`` should not silently scan against a service the
    operator did not choose. ``Config.validate`` catches this at startup, so
    reaching here with a bad name means it was set after that.
    """
    name = (config.scan.backend or "").strip().lower()
    if name not in BACKENDS:
        raise ValueError(
            f"unknown scan.backend {config.scan.backend!r}; "
            f"expected one of {', '.join(BACKENDS)}"
        )

    if name == "okappiki":
        return OkappikiClient(
            limiter=RateLimiter(
                limit=config.okappiki.rate_limit,
                window=config.okappiki.window,
                reserve=config.okappiki.reserve,
            ),
            cache_ttl=effective_cache_ttl(config, config.okappiki.cache_ttl),
            retain_beyond_terms=config.scan.retain_beyond_terms,
            concurrency=config.okappiki.concurrency,
            proxies=proxies,
            direct_as_fallback=config.proxy.direct_as_fallback,
        )

    return RotectorClient(
        api_key=config.rotector.api_key,
        limiter=RateLimiter(
            limit=config.rotector.rate_limit,
            window=config.rotector.window,
            reserve=config.rotector.reserve,
        ),
        cache_ttl=effective_cache_ttl(config, config.rotector.cache_ttl),
        retain_beyond_terms=config.scan.retain_beyond_terms,
        concurrency=config.rotector.concurrency,
        proxies=proxies,
        direct_as_fallback=config.proxy.direct_as_fallback,
    )
