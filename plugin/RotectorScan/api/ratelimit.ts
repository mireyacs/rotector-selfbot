/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Sliding-window rate limiter for the Rotector API. Ported from
 * rsb/ratelimit.py.
 *
 * The documented budget is 50 requests per 10 second window, scoped per IP (or
 * per key, if an API key is supplied). Batch endpoints cost more than one
 * request: measured against the live API, a batch of N ids costs ceil(N / 50)
 * request units — a 100-id batch costs 2, anything up to 50 costs 1.
 *
 * The Python version keeps three layers: a local sliding window, header sync
 * from `X-RateLimit-Remaining` / `X-RateLimit-Reset`, and an exact
 * `Retry-After` on a 429. **Only the first survives the port**, and this is the
 * one place the plugin must diverge from rsb/ratelimit.py:
 *
 *   roscoe.rotector.com sends `access-control-expose-headers: X-Token-Expires`
 *   and nothing else. Under the browser's CORS rules a cross-origin response
 *   only exposes the safelisted headers plus whatever that list names, so
 *   `x-ratelimit-limit`, `x-ratelimit-remaining`, `x-ratelimit-reset` and
 *   `retry-after` are all unreadable from a renderer — `response.headers.get()`
 *   returns null for every one of them. There is therefore no `observe()` here
 *   and `penalise()` takes a blind delay rather than a server-supplied one.
 *
 * Because the server's view is invisible, the local window is the only thing
 * standing between a scan and a 429, so it is deliberately the pacer for every
 * request: nothing in this plugin issues a request without spending units here
 * first, and everything aimed at one host spends them from the *same* limiter
 * (see `sharedLimiter` below) — a per-object budget would only be counting the
 * requests it happens to know about.
 */

/** how many ids fit into a single request unit on the batch endpoints */
export const IDS_PER_UNIT = 50;

/** Request units a batch of `idCount` ids will cost. */
export function batchCost(idCount: number): number {
    const n = Number(idCount);
    if (!isFinite(n) || n <= 0) return 1;
    return Math.max(1, Math.ceil(n / IDS_PER_UNIT));
}

export interface RateLimiterOptions {
    /** request units per window; the documented standard tier is 50 */
    limit?: number;
    /** window length in **seconds** */
    window?: number;
    /** units held back as headroom against clock skew and shared IPs */
    reserve?: number;
}

// --------------------------------------------------------------------------
// abort plumbing
// --------------------------------------------------------------------------

/**
 * An abort rejection that matches what `fetch` throws, so one `isAbortError`
 * check covers both a cancelled sleep and a cancelled request.
 *
 * DOMException is constructible in every browser Discord runs on, but this is
 * loaded without type checking into a runtime we do not own, so the plain Error
 * fallback is there rather than assuming.
 */
export function abortError(message = "aborted"): Error {
    try {
        return new DOMException(message, "AbortError") as unknown as Error;
    } catch {
        const err = new Error(message);
        err.name = "AbortError";
        return err;
    }
}

export function isAbortError(err: unknown): boolean {
    if (!err) return false;
    const name = (err as any).name;
    return name === "AbortError" || name === "TimeoutError";
}

/** Sleep that rejects with an AbortError the moment `signal` fires. */
export function sleep(ms: number, signal?: AbortSignal): Promise<void> {
    return new Promise<void>((resolve, reject) => {
        if (signal && signal.aborted) {
            reject(abortError());
            return;
        }
        let timer: any = null;
        const onAbort = () => {
            cleanup();
            reject(abortError());
        };
        const cleanup = () => {
            if (timer !== null) clearTimeout(timer);
            timer = null;
            try {
                signal?.removeEventListener?.("abort", onAbort);
            } catch {
                // the signal was not a real EventTarget; nothing to unhook
            }
        };
        timer = setTimeout(() => {
            cleanup();
            resolve();
        }, Math.max(0, ms));
        try {
            signal?.addEventListener?.("abort", onAbort);
        } catch {
            // as above — a caller passing something signal-shaped but inert
            // still gets a working sleep, it just cannot be cancelled.
        }
    });
}

// --------------------------------------------------------------------------
// the limiter
// --------------------------------------------------------------------------

interface Spend {
    at: number;
    cost: number;
}

export class RateLimiter {
    limit: number;
    /** window length in seconds, kept in the Python module's units */
    window: number;
    reserve: number;

    private events: Spend[] = [];
    private blockedUntil = 0;
    /**
     * Python serialises `acquire` behind an asyncio.Lock. A renderer has no
     * such primitive, so waiters queue on a promise chain: without it two
     * callers can both read the same "there is room" and both spend, which is
     * exactly the overspend the reserve exists to absorb.
     */
    private chain: Promise<void> = Promise.resolve();

    constructor(opts: RateLimiterOptions = {}) {
        this.limit = toPositive(opts.limit, 50);
        this.window = toPositive(opts.window, 10);
        this.reserve = clampReserve(opts.reserve ?? 5, this.limit);
    }

    /**
     * Apply new settings to a limiter that is already running.
     *
     * Only the fields actually supplied are touched, because this is what a
     * shared limiter is reconfigured through: a caller that has no opinion
     * about the budget (the connectivity probe, say) must not reset the budget
     * a scan is running under. The spend history is deliberately kept — the
     * window the server is counting does not restart because the user typed a
     * different number into the settings screen.
     */
    configure(opts: RateLimiterOptions = {}): void {
        if (opts.limit != null) this.limit = toPositive(opts.limit, this.limit);
        if (opts.window != null) this.window = toPositive(opts.window, this.window);
        // Re-clamped even when reserve was not supplied, because a lowered
        // limit can leave an untouched reserve above it.
        this.reserve = clampReserve(opts.reserve ?? this.reserve, this.limit);
    }

    // -- internals ---------------------------------------------------------

    private get effectiveLimit(): number {
        return Math.max(1, this.limit - this.reserve);
    }

    private get windowMs(): number {
        return Math.max(1, this.window * 1000);
    }

    private prune(now: number): void {
        const cutoff = now - this.windowMs;
        while (this.events.length && this.events[0].at <= cutoff) this.events.shift();
    }

    private used(now: number): number {
        this.prune(now);
        let total = 0;
        for (const ev of this.events) total += ev.cost;
        return total;
    }

    // -- public API --------------------------------------------------------

    /**
     * Wait until `cost` units are available, then spend them.
     *
     * Rejects with an AbortError if `signal` fires while waiting; the units are
     * not spent in that case.
     */
    acquire(cost = 1, signal?: AbortSignal): Promise<void> {
        const spend = Math.max(1, Math.floor(Number(cost)) || 1);
        const run = () => this.acquireNow(spend, signal);
        // `then(run, run)` so one waiter's abort does not strand the queue.
        const result = this.chain.then(run, run);
        this.chain = result.then(noop, noop);
        return result;
    }

    private async acquireNow(cost: number, signal?: AbortSignal): Promise<void> {
        for (;;) {
            if (signal && signal.aborted) throw abortError();
            const now = Date.now();

            if (now < this.blockedUntil) {
                await sleep(this.blockedUntil - now, signal);
                continue;
            }

            const used = this.used(now);
            // The empty-queue escape hatch lets an oversized cost through
            // rather than deadlocking; in practice cost is never above 2.
            if (used + cost <= this.effectiveLimit || this.events.length === 0) {
                this.events.push({ at: now, cost });
                return;
            }

            // Sleep until the oldest spend falls out of the window.
            const wait = this.events[0].at + this.windowMs - now;
            await sleep(Math.max(wait, 10), signal);
        }
    }

    /**
     * Blind backoff after a 429.
     *
     * The Python version passes the response's `Retry-After` here. That header
     * is not exposed cross-origin (see the module docstring), so the only
     * honest fallback is to hold every caller for a full window — long enough
     * that whatever the server was counting has rolled over.
     */
    penalise(seconds?: number): void {
        const delay = seconds && seconds > 0 ? seconds : this.window;
        this.blockedUntil = Math.max(this.blockedUntil, Date.now() + delay * 1000);
    }

    /** units per second this limiter can sustain */
    capacityPerSec(): number {
        return this.effectiveLimit / Math.max(0.001, this.window);
    }

    /** units still spendable in the current window */
    available(): number {
        return Math.max(0, this.limit - this.reserve - this.used(Date.now()));
    }

    /** seconds a penalty still holds callers for; 0 when nothing is blocked */
    blockedFor(): number {
        return Math.max(0, (this.blockedUntil - Date.now()) / 1000);
    }

    /** Forget the window and any penalty. Used when a scan restarts. */
    reset(): void {
        this.events = [];
        this.blockedUntil = 0;
    }
}

// --------------------------------------------------------------------------
// one limiter per host
// --------------------------------------------------------------------------

/**
 * Limiters, keyed by base URL, for the life of the session.
 *
 * The budget being spent is the server's, and the server counts per IP (or per
 * key) — not per object. So a limiter per client object is not a limiter at
 * all: the connectivity probe in api/host.ts builds its own client, and with
 * its own limiter every probe would spend a request unit that the running
 * scan's limiter knows nothing about, which is exactly the overspend this
 * module's docstring says nothing is allowed to do. Everything that talks to a
 * host shares the host's limiter instead.
 *
 * This registry lives here rather than in api/rotector.ts because api/host.ts
 * has to reach it too, and ratelimit.ts is the one module in the chain that
 * imports nothing of the plugin's — so there is no cycle to create.
 */
const limiters = new Map<string, RateLimiter>();

/**
 * The limiter for `key` (a base URL), creating it on first use.
 *
 * Options supplied on a later call reconfigure the existing limiter rather than
 * replacing it, so changing the rate limit in settings takes effect without
 * handing the new client a fresh, empty window to overspend from.
 */
export function sharedLimiter(key: string, opts: RateLimiterOptions = {}): RateLimiter {
    const id = String(key || "");
    const existing = limiters.get(id);
    if (existing) {
        existing.configure(opts);
        return existing;
    }
    const limiter = new RateLimiter(opts);
    limiters.set(id, limiter);
    return limiter;
}

/** Forget every shared limiter. Only for tests and a full plugin teardown. */
export function resetSharedLimiters(): void {
    for (const limiter of limiters.values()) limiter.reset();
    limiters.clear();
}

/**
 * The Python constructor raises when reserve >= limit. Throwing here would take
 * the whole plugin down over a mistyped setting, so the value is clamped the
 * same way observe() used to clamp it instead.
 */
function clampReserve(value: unknown, limit: number): number {
    const reserve = Math.max(0, Math.floor(Number(value)) || 0);
    if (reserve >= limit) return Math.max(0, Math.floor(limit / 10));
    return reserve;
}

function toPositive(value: unknown, fallback: number): number {
    const n = Number(value);
    if (!isFinite(n) || n <= 0) return fallback;
    return n;
}

function noop(): void {
    // deliberately empty: the chain only needs settlement, not the value
}
