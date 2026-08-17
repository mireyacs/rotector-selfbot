/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Rotector API client. Ported from the client half of rsb/rotector.py, plus
 * estimate_scan_seconds from rsb/eta.py.
 *
 * Turning a list of Discord member ids into a verdict takes two hops, because
 * the Discord endpoint does not itself return a flag status:
 *
 *   1. POST /v1/lookup/discord/user  (<=100 ids)
 *      -> tracked server memberships + linked Roblox accounts
 *   2. POST /v1/lookup/roblox/user   (<=100 ids)
 *      -> flag type, category, confidence and reasons per Roblox account
 *
 * Both endpoints share one rate-limit bucket, so they share one RateLimiter —
 * and so does every other client pointed at the same host, because the bucket
 * is the server's and it is counted per IP. The limiter comes from
 * `sharedLimiter(baseUrl)` in api/ratelimit.ts rather than being constructed
 * here, so building a second client (the settings panel's connectivity probe
 * does) does not hand it a second, unwatched budget.
 *
 * Two things from the Python client are deliberately absent:
 *
 *  - **Proxies and the RoutePool.** A renderer has exactly one route: its own
 *    origin, with the user's own IP. There is no request-level proxy setting in
 *    `fetch`, and routing Discord's renderer through a third party would be a
 *    worse idea than the extra throughput is worth. Everything the pool did —
 *    route health, per-route limiters, capacity summing — collapses to the one
 *    limiter below.
 *  - **Response-header observation.** See api/ratelimit.ts: roscoe exposes only
 *    `X-Token-Expires` to cross-origin JS, so `x-ratelimit-*` and `retry-after`
 *    read as null here. 429 handling is blind backoff.
 *
 * Rotector Terms of Use #1 forbids retaining responses for longer than 24
 * hours, which is what the caches here do unless they are told otherwise: they
 * are in-memory only, are cleared when the client is replaced, and their TTL is
 * clamped to MAX_CACHE_TTL_MS. Nothing in this module writes to disk, DataStore
 * or the settings store.
 *
 * That ceiling is lifted by exactly one thing — the `retainBeyondTerms` option,
 * off by default, which the operator sets deliberately — and even then it is
 * lifted to MAX_RETAINED_TTL_MS rather than removed, because an unexpiring
 * cache is a memory leak as well as a licensing problem and a year-old flag
 * status is not evidence about anybody. Switching it on is a choice to hold
 * Rotector's responses outside the terms they were served under.
 *
 * Nothing is quietly passed off as current either way. Every MemberReport
 * carries the time its data actually arrived, and anything answered from beyond
 * the 24-hour window is reported as stale, with its age, wherever it is shown
 * or written — see `isStale` and `staleNote` in api/types.ts.
 */

import { batchCost, isAbortError, RateLimiter, sharedLimiter, sleep } from "./ratelimit";
import { emptyReport, MemberReport, RobloxAccount, TrackedServer } from "./types";

export const BASE_URL = "https://roscoe.rotector.com";
export const MAX_BATCH = 100;
/**
 * The default ceiling, from the Terms of Use: the configured TTL is clamped to
 * this unless `retainBeyondTerms` has been switched on.
 */
export const MAX_CACHE_TTL_MS = 23 * 3600 * 1000;

/**
 * The ceiling that applies once the operator has switched `retainBeyondTerms`
 * on. Not unbounded: an unexpiring cache is a memory leak as well as a
 * licensing problem, and a year-old flag status is not evidence about anybody.
 */
export const MAX_RETAINED_TTL_MS = 365 * 24 * 3600 * 1000;

/** how often the client drops expired responses it was never asked for again */
export const SWEEP_INTERVAL_MS = 5 * 60 * 1000;
/**
 * Kept for identity and parity with the Python client, but **never sent**:
 * `user-agent` is a forbidden header name in the Fetch spec, so setting it
 * either throws or is silently dropped depending on the engine. The only
 * identification the API gets from a renderer is the origin and, when it is
 * configured, the API key.
 */
export const USER_AGENT = "rotector-selfbot-plugin/1.0 (+https://rotector.com)";

/**
 * Fraction of Discord members assumed to have a Roblox account Rotector knows
 * about. Only used for the pre-scan estimate — once a scan starts, the real
 * counts are what the UI reports.
 */
export const DEFAULT_LINK_RATE = 0.25;

/** seconds; a renderer fetch has no timeout of its own, so one is imposed */
const REQUEST_TIMEOUT_MS = 30_000;

export class RotectorError extends Error {
    constructor(message: string) {
        super(message);
        this.name = "RotectorError";
    }
}

/** thrown when the request never reached the API (CORS, CSP, offline) */
export class TransportError extends RotectorError {
    corsBlocked: boolean;

    constructor(message: string, corsBlocked: boolean) {
        super(message);
        this.name = "TransportError";
        this.corsBlocked = corsBlocked;
    }
}

export interface ClientOptions {
    apiKey?: string;
    /** request units per window */
    rateLimit?: number;
    /** window length in seconds */
    window?: number;
    reserve?: number;
    /**
     * cache lifetime in **seconds**; clamped to MAX_CACHE_TTL_MS, or to
     * MAX_RETAINED_TTL_MS when `retainBeyondTerms` is on
     */
    cacheTtl?: number;
    /**
     * Keep findings past the 24-hour window Rotector's Terms of Use allow.
     *
     * Off by default and it should stay off unless the operator has decided
     * otherwise on purpose: switching it on is a choice to hold Rotector's
     * responses outside the terms they were served under. It only raises the
     * ceiling `cacheTtl` is clamped to — it does not lengthen a TTL nobody
     * asked for, which is why api/backend.ts derives the TTL from
     * `retentionHours` when this is on.
     */
    retainBeyondTerms?: boolean;
    concurrency?: number;
    baseUrl?: string;
    maxRetries?: number;
    /**
     * A limiter to spend from instead of this host's shared one.
     *
     * Only for a caller that genuinely has a separate budget — a second host,
     * or a test. Passing one for the same host as another client is how the
     * plugin ends up overspending a budget neither half can see.
     */
    limiter?: RateLimiter;
}

export interface ScanProgress {
    stage: string;
    done: number;
    total: number;
}

export interface ScanOptions {
    onProgress?(p: ScanProgress): void;
    onPartial?(reports: MemberReport[]): void;
    signal?: AbortSignal;
}

interface CacheEntry {
    at: number;
    value: any;
}

// --------------------------------------------------------------------------
// client
// --------------------------------------------------------------------------

export class RotectorClient {
    readonly limiter: RateLimiter;
    readonly baseUrl: string;
    readonly maxRetries: number;
    /** in-memory cache lifetime in ms, already clamped under the live ceiling */
    readonly cacheTtlMs: number;
    /** whether this client was built with the 24-hour ceiling lifted */
    readonly retainBeyondTerms: boolean;
    /** how many batch requests may be in flight at once */
    readonly scanConcurrency: number;

    private apiKey: string;
    private sem: Semaphore;
    private discordCache = new Map<string, CacheEntry>();
    private robloxCache = new Map<number, CacheEntry>();
    private sweepTimer: any = null;

    constructor(opts: ClientOptions = {}) {
        this.apiKey = typeof opts.apiKey === "string" ? opts.apiKey.trim() : "";
        this.baseUrl = stripSlash(opts.baseUrl || BASE_URL);
        // One limiter per host, not per client. The rate limit belongs to the
        // server and is counted per IP, so every client aimed at this base URL
        // — the scan's, the settings panel's connectivity probe's — has to
        // spend from the same window or none of them is measuring anything.
        // The duck-typed check is because this file is loaded without type
        // checking and an injected limiter comes from outside.
        const injected = opts.limiter;
        this.limiter = injected && typeof (injected as any).acquire === "function"
            ? injected
            : sharedLimiter(this.baseUrl, {
                limit: opts.rateLimit,
                window: opts.window,
                reserve: opts.reserve,
            });
        this.maxRetries = clampInt(opts.maxRetries, 4, 0, 10);
        // The 24h ceiling is Rotector's Terms of Use #1 and is the default. The
        // operator can lift it deliberately — see `retainBeyondTerms` — and when
        // they have, anything served from beyond the window is reported as
        // stale rather than passed off as current.
        this.retainBeyondTerms = opts.retainBeyondTerms === true;
        const ceiling = this.retainBeyondTerms ? MAX_RETAINED_TTL_MS : MAX_CACHE_TTL_MS;
        this.cacheTtlMs = Math.min(Math.max(0, toNumber(opts.cacheTtl, 3600) * 1000), ceiling);
        // The Python client sized concurrency from the number of routes. There
        // is one route here, so this is simply what the user configured — and
        // the limiter, not this, is what actually paces the scan.
        this.scanConcurrency = clampInt(opts.concurrency, 3, 1, 16);
        this.sem = new Semaphore(this.scanConcurrency);
    }

    // -- transport ---------------------------------------------------------

    private headers(hasBody: boolean): Record<string, string> {
        const headers: Record<string, string> = { accept: "application/json" };
        // `content-type: application/json` is what makes this a non-simple
        // request and triggers a CORS preflight. The API answers OPTIONS, so
        // that is fine — but it is why a misconfigured CORS setup shows up as a
        // TypeError with no status code rather than an HTTP error.
        if (hasBody) headers["content-type"] = "application/json";
        if (this.apiKey) headers.authorization = `Bearer ${this.apiKey}`;
        // No `user-agent`: see the USER_AGENT comment above.
        return headers;
    }

    /**
     * One request, through the concurrency semaphore, with a timeout.
     *
     * `fetch` rejects with a TypeError when the request never left the browser
     * — a CSP `connect-src` refusal, a missing `Access-Control-Allow-Origin`, a
     * failed preflight, DNS failure or no network all land here with no status
     * code and no way to tell them apart from JS. That is the whole reason
     * TransportError carries `corsBlocked`: api/host.ts has to probe further to
     * say anything useful about it.
     */
    private async send(path: string, body: any | null, signal?: AbortSignal): Promise<Response> {
        const link = linkSignal(signal, REQUEST_TIMEOUT_MS);
        const release = await this.sem.acquire();
        try {
            return await fetch(this.baseUrl + path, {
                method: body == null ? "GET" : "POST",
                headers: this.headers(body != null),
                body: body == null ? undefined : JSON.stringify(body),
                signal: link.signal,
                // Never attach Discord's cookies to a third-party request.
                credentials: "omit",
                mode: "cors",
                cache: "no-store",
                referrerPolicy: "no-referrer",
            });
        } catch (err: any) {
            if (link.timedOut()) {
                throw new TransportError(
                    `${path}: no response after ${Math.round(REQUEST_TIMEOUT_MS / 1000)}s`,
                    false,
                );
            }
            if (isAbortError(err)) throw err;
            if (err instanceof TypeError || err?.name === "TypeError") {
                throw new TransportError(
                    `${path}: the request never left the browser (${err?.message || "network error"})`,
                    true,
                );
            }
            throw new TransportError(`${path}: ${err?.message || String(err)}`, false);
        } finally {
            release();
            link.done();
        }
    }

    /**
     * One rate-limited POST to a batch endpoint, with bounded soft retries.
     *
     * Retries are capped on purpose: a permanently throttled or broken endpoint
     * must raise rather than loop, because a lookup that returns empty is
     * indistinguishable from "nothing found" and that would read as a clean
     * bill of health for everyone in the batch.
     */
    private async postBatch(path: string, ids: string[], signal?: AbortSignal): Promise<any> {
        const cost = batchCost(ids.length);
        const payload = { ids: ids.map(id => String(id)) };
        const maxSoft = this.maxRetries + 4;
        let soft = 0;

        for (;;) {
            await this.limiter.acquire(cost, signal);
            const resp = await this.send(path, payload, signal);

            if (resp.status === 429) {
                // Backpressure, not a fault. No Retry-After is readable here,
                // so the limiter holds every caller for a full window.
                this.limiter.penalise();
                soft++;
                if (soft > maxSoft) {
                    throw new RotectorError(`${path}: still rate limited after ${soft} retries`);
                }
                continue;
            }

            if (resp.status >= 500) {
                soft++;
                if (soft > maxSoft) {
                    throw new RotectorError(
                        `${path}: server error ${resp.status} after ${soft} retries`,
                    );
                }
                await sleep(Math.min(Math.pow(2, soft), 15) * 1000, signal);
                continue;
            }

            if (resp.status >= 400) {
                const text = await safeText(resp);
                throw new RotectorError(`${path} -> HTTP ${resp.status}: ${text.slice(0, 200)}`);
            }

            const body = await safeJson(resp);
            if (body == null || typeof body !== "object") {
                throw new RotectorError(`${path}: response was not JSON`);
            }
            if ((body as any).success !== true) {
                throw new RotectorError(`${path} -> ${(body as any).error || "unknown error"}`);
            }
            return (body as any).data || {};
        }
    }

    // -- caching -----------------------------------------------------------

    private cacheGet(cache: Map<any, CacheEntry>, key: any): any | null {
        const entry = cache.get(key);
        if (!entry) return null;
        if (Date.now() - entry.at > this.cacheTtlMs) {
            cache.delete(key);
            return null;
        }
        return entry.value;
    }

    private cachePut(cache: Map<any, CacheEntry>, key: any, value: any): void {
        if (this.cacheTtlMs <= 0) return;
        cache.set(key, { at: Date.now(), value });
        this.ensureSweep();
    }

    /**
     * Drop expired responses on a timer, not only when they are read again.
     *
     * `cacheGet` evicting on read is not enough on its own: an id nobody looks
     * up a second time is never read again, so its raw Rotector payload would
     * sit in memory for the whole Discord session — days — which is exactly the
     * retention Rotector's terms forbid and exactly what the report store's own
     * sweeper promises does not happen. The store's sweeper cannot reach these
     * maps; they are the client's, so the client sweeps them.
     */
    private ensureSweep(): void {
        if (this.sweepTimer !== null || this.cacheTtlMs <= 0) return;
        this.sweepTimer = setInterval(() => this.sweep(), SWEEP_INTERVAL_MS) as any;
    }

    private sweep(): void {
        const now = Date.now();
        for (const cache of [this.discordCache, this.robloxCache] as Map<any, CacheEntry>[]) {
            for (const [key, entry] of Array.from(cache)) {
                if (now - entry.at > this.cacheTtlMs) cache.delete(key);
            }
        }
        if (!this.discordCache.size && !this.robloxCache.size) this.clearSweep();
    }

    private clearSweep(): void {
        if (this.sweepTimer === null) return;
        clearInterval(this.sweepTimer);
        this.sweepTimer = null;
    }

    /** Drop every cached response (also called on scan restart). */
    purgeCache(): void {
        this.discordCache.clear();
        this.robloxCache.clear();
        this.clearSweep();
    }

    // -- endpoints ---------------------------------------------------------

    async lookupDiscordUsers(ids: string[], signal?: AbortSignal): Promise<Map<string, any>> {
        const wanted = unique((ids || []).map(id => String(id)).filter(Boolean));
        const out = new Map<string, any>();
        const missing: string[] = [];

        for (const uid of wanted) {
            const cached = this.cacheGet(this.discordCache, uid);
            if (cached == null) missing.push(uid);
            else out.set(uid, cached);
        }

        for (const chunk of chunks(missing, MAX_BATCH)) {
            const data = await this.postBatch("/v1/lookup/discord/user", chunk, signal);
            for (const uid of chunk) {
                // An id the API says nothing about still gets an entry, so a
                // second lookup does not re-ask for a member it already knows
                // is not tracked.
                const entry = data?.[uid] || { id: uid, servers: [], connections: [] };
                this.cachePut(this.discordCache, uid, entry);
                out.set(uid, entry);
            }
        }
        return out;
    }

    async lookupRobloxUsers(ids: number[], signal?: AbortSignal): Promise<Map<number, any>> {
        const wanted: number[] = [];
        const seen = new Set<number>();
        for (const raw of ids || []) {
            const rid = maybeInt(raw);
            if (rid == null || seen.has(rid)) continue;
            seen.add(rid);
            wanted.push(rid);
        }

        const out = new Map<number, any>();
        const missing: number[] = [];
        for (const rid of wanted) {
            const cached = this.cacheGet(this.robloxCache, rid);
            if (cached == null) missing.push(rid);
            else out.set(rid, cached);
        }

        for (const chunk of chunks(missing, MAX_BATCH)) {
            const data = await this.postBatch(
                "/v1/lookup/roblox/user",
                chunk.map(rid => String(rid)),
                signal,
            );
            for (const rid of chunk) {
                const entry = data?.[String(rid)] || { id: rid, flagType: null };
                this.cachePut(this.robloxCache, rid, entry);
                out.set(rid, entry);
            }
        }
        return out;
    }

    /** single-user GET; unlike the batch form this includes altAccounts */
    async lookupDiscordUserDetail(id: string, signal?: AbortSignal): Promise<any> {
        const uid = String(id || "").trim();
        if (!uid) throw new RotectorError("no user id given");

        await this.limiter.acquire(1, signal);
        const resp = await this.send(`/v1/lookup/discord/user/${encodeURIComponent(uid)}`, null, signal);

        if (resp.status === 429) {
            this.limiter.penalise();
            throw new RotectorError("rate limited");
        }
        if (resp.status >= 400) {
            const text = await safeText(resp);
            throw new RotectorError(`detail lookup -> HTTP ${resp.status}: ${text.slice(0, 200)}`);
        }
        const body = await safeJson(resp);
        if (body == null || typeof body !== "object") {
            throw new RotectorError("detail lookup: response was not JSON");
        }
        if ((body as any).success !== true) {
            throw new RotectorError(String((body as any).error || "unknown error"));
        }
        return (body as any).data || {};
    }

    // -- orchestration -----------------------------------------------------

    /**
     * Scan a set of Discord ids, in batches, concurrently.
     *
     * Guarantees, all of them load-bearing for the surfaces above:
     *
     *  - every input id gets a report, even if its batch failed outright;
     *  - partial results are published as each batch lands, so the ledger fills
     *    while the scan runs rather than all at once at the end;
     *  - progress is reported through the stages the Python client names;
     *  - aborting resolves rather than rejecting. Whatever was already found is
     *    kept and everything still outstanding is returned as an error report
     *    saying the scan was stopped — a stopped scan is a partial answer, not
     *    a failure, and it must never be mistaken for "nothing found".
     */
    async scanMembers(ids: string[], opts: ScanOptions = {}): Promise<Map<string, MemberReport>> {
        const signal = opts.signal;
        const wanted = unique((ids || []).map(id => String(id)).filter(Boolean));
        const reports = new Map<string, MemberReport>();
        const total = wanted.length;
        let done = 0;

        const progress = (stage: string, doneOverride?: number) => {
            if (!opts.onProgress) return;
            try {
                opts.onProgress({ stage, done: doneOverride == null ? done : doneOverride, total });
            } catch {
                // A UI callback throwing must not abort the scan.
            }
        };

        if (!total) {
            progress("Scan complete");
            return reports;
        }

        const publish = (batch: MemberReport[], count: number, stage: string) => {
            done += count;
            for (const report of batch) reports.set(report.discordId, report);
            progress(stage);
            if (batch.length && opts.onPartial) {
                try {
                    opts.onPartial(batch);
                } catch {
                    // as above
                }
            }
        };

        const handle = async (chunk: string[]) => {
            if (signal?.aborted) {
                publish(chunk.map(uid => emptyReport(uid, STOPPED)), chunk.length, "Scan stopped");
                return;
            }

            progress("Looking up Discord accounts");

            let discordData: Map<string, any>;
            try {
                discordData = await this.lookupDiscordUsers(chunk, signal);
            } catch (err: any) {
                const message = isAbortError(err) ? STOPPED : errorText(err);
                publish(
                    chunk.map(uid => emptyReport(uid, message)),
                    chunk.length,
                    isAbortError(err) ? "Scan stopped" : "Some lookups failed",
                );
                return;
            }

            const batch: MemberReport[] = [];
            const robloxIds: number[] = [];
            const seenRoblox = new Set<number>();

            for (const uid of chunk) {
                const raw = discordData.get(uid) || {};
                const report = emptyReport(uid);
                for (const srv of asArray(raw.servers)) {
                    report.servers.push(parseServer(srv));
                }
                for (const conn of asArray(raw.connections)) {
                    const rid = maybeInt(conn?.robloxUserId);
                    if (rid == null) continue;
                    report.accounts.push(parseConnection(rid, conn));
                    if (!seenRoblox.has(rid)) {
                        seenRoblox.add(rid);
                        robloxIds.push(rid);
                    }
                }
                batch.push(report);
            }

            if (robloxIds.length) {
                progress(
                    `Checking ${robloxIds.length.toLocaleString()} linked Roblox ` +
                    `account${robloxIds.length === 1 ? "" : "s"} for flags`,
                );
                let flags = new Map<number, any>();
                try {
                    flags = await this.lookupRobloxUsers(robloxIds, signal);
                } catch (err: any) {
                    const message = isAbortError(err)
                        ? STOPPED
                        : `flag lookup failed: ${errorText(err)}`;
                    // The Discord hop succeeded, so the linked accounts are
                    // real and are kept — but with no flag status they must not
                    // read as unflagged, hence the error on every report.
                    for (const report of batch) report.error = message;
                }
                for (const report of batch) {
                    for (const account of report.accounts) {
                        applyFlag(account, flags.get(account.userId));
                    }
                }
            }

            publish(batch, chunk.length, "Checking members");
        };

        const batches = chunks(wanted, MAX_BATCH);
        await runPool(batches, this.scanConcurrency, handle);

        // Belt and braces: every id that entered the scan must have a report.
        const stopped = !!signal?.aborted;
        const missing = wanted.filter(uid => !reports.has(uid));
        for (const uid of missing) {
            reports.set(uid, emptyReport(uid, stopped ? STOPPED : "no response"));
        }
        if (missing.length) {
            progress(`${missing.length.toLocaleString()} lookups unanswered`, total);
        }
        progress(stopped ? "Scan stopped" : "Scan complete", total);
        return reports;
    }

    /** Combined request-unit throughput; one route, so one limiter. */
    capacityUnitsPerSec(): number {
        return this.limiter.capacityPerSec();
    }

    /**
     * How long scanning `members` should take on this backend, in seconds.
     *
     * Lives on the client because the arithmetic is the backend's: this one
     * batches a hundred ids per request and makes a second pass over whoever
     * turns out to have a linked account. Ported from
     * rsb/eta.py:estimate_scan_seconds.
     */
    estimateSeconds(members: number): number | null {
        const capacity = this.capacityUnitsPerSec();
        const n = Math.floor(toNumber(members, 0));
        if (n <= 0 || capacity <= 0) return null;
        const linked = Math.round(n * Math.max(0, DEFAULT_LINK_RATE));
        return (unitsForIds(n) + unitsForIds(linked)) / capacity;
    }

    /**
     * One cheap request, to tell "works" from "CORS/CSP blocked" from
     * "API down". Costs a single request unit and is paced like everything
     * else, with a hard cap so a rate-limit hold cannot make it hang.
     */
    async ping(): Promise<{ ok: boolean; error?: string; corsBlocked?: boolean; neverSent?: boolean; }> {
        const controller = newController();
        const timer = setTimeout(() => {
            try {
                controller?.abort();
            } catch {
                // nothing to do; the fetch timeout still applies
            }
        }, 12_000);
        try {
            await this.limiter.acquire(1, controller?.signal);
            const resp = await this.send("/v1/lookup/discord/user", { ids: ["1"] }, controller?.signal);
            if (resp.status === 429) {
                this.limiter.penalise();
                return { ok: false, error: "rate limited (HTTP 429)", corsBlocked: false };
            }
            if (resp.status >= 400) {
                const text = await safeText(resp);
                return {
                    ok: false,
                    error: `HTTP ${resp.status}${text ? `: ${text.slice(0, 120)}` : ""}`,
                    corsBlocked: false,
                };
            }
            const body = await safeJson(resp);
            if (body == null || typeof body !== "object") {
                return { ok: false, error: "the response was not JSON", corsBlocked: false };
            }
            if ((body as any).success !== true) {
                return {
                    ok: false,
                    error: String((body as any).error || "the API rejected the request"),
                    corsBlocked: false,
                };
            }
            return { ok: true };
        } catch (err: any) {
            if (err instanceof TransportError) {
                return { ok: false, error: err.message, corsBlocked: err.corsBlocked };
            }
            if (isAbortError(err)) {
                // Nothing was sent. Almost always the limiter still holding
                // callers back — a scan spending the window, or a 429 penalty
                // being waited out. Reporting this as "the API answered" would
                // blame the service for our own queue, so it is flagged as
                // never-sent and worded as a queue by the caller.
                return { ok: false, error: "the check timed out", corsBlocked: false, neverSent: true };
            }
            return { ok: false, error: errorText(err), corsBlocked: false };
        } finally {
            clearTimeout(timer);
        }
    }
}

// --------------------------------------------------------------------------
// eta helpers (rsb/eta.py)
// --------------------------------------------------------------------------

/** Request units a batched lookup of `count` ids costs in total. */
export function unitsForIds(count: number): number {
    const n = Math.floor(toNumber(count, 0));
    if (n <= 0) return 0;
    const full = Math.floor(n / MAX_BATCH);
    const remainder = n % MAX_BATCH;
    let total = full * batchCost(MAX_BATCH);
    if (remainder) total += batchCost(remainder);
    return total;
}

/** `95` -> `"1m 35s"`. Coarse on purpose; this is an estimate. */
export function formatDuration(seconds: number | null | undefined): string {
    if (seconds == null || !isFinite(Number(seconds))) return "?";
    const value = Math.max(0, Number(seconds));
    if (value < 1) return "<1s";
    if (value < 60) return `${Math.round(value)}s`;
    const whole = Math.floor(value + 0.5);
    const totalMinutes = Math.floor(whole / 60);
    const secs = whole % 60;
    if (totalMinutes < 60) return `${totalMinutes}m ${pad2(secs)}s`;
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    return `${hours}h ${pad2(minutes)}m`;
}

/** How many reports in a scan result came back with an error. */
export function countUnanswered(reports: Iterable<MemberReport>): number {
    let n = 0;
    for (const report of reports) if (report?.error) n++;
    return n;
}

// --------------------------------------------------------------------------
// parsing
// --------------------------------------------------------------------------

const STOPPED = "scan stopped before this member was checked";

function parseServer(raw: any): TrackedServer {
    return {
        serverId: String(raw?.serverId ?? ""),
        serverName: raw?.serverName || "(unnamed)",
        joinedAt: maybeInt(raw?.joinedAt),
        firstSeenAt: maybeInt(raw?.firstSeenAt),
        isTase: !!raw?.isTase,
        inGracePeriod: !!raw?.inGracePeriod,
    };
}

function parseConnection(robloxUserId: number, raw: any): RobloxAccount {
    return {
        userId: robloxUserId,
        username: raw?.robloxUsername || String(robloxUserId),
        detectedAt: maybeInt(raw?.detectedAt),
        sources: asArray(raw?.sources)
            .map(maybeInt)
            .filter((s): s is number => s != null),
        flagType: null,
        category: null,
        confidence: null,
        reasons: {},
        lastUpdated: null,
    };
}

function applyFlag(account: RobloxAccount, raw: any): void {
    if (!raw || typeof raw !== "object") return;
    account.flagType = maybeInt(raw.flagType);
    account.category = maybeInt(raw.category);
    const confidence = raw.confidence;
    account.confidence = confidence == null || !isFinite(Number(confidence))
        ? null
        : Number(confidence);
    account.reasons = raw.reasons && typeof raw.reasons === "object" ? raw.reasons : {};
    account.lastUpdated = maybeInt(raw.lastUpdated);
}

// --------------------------------------------------------------------------
// small utilities
// --------------------------------------------------------------------------

/**
 * A fixed-size worker pool. `handle` is expected to swallow its own errors —
 * it does above — but anything that escapes is contained here so one bad batch
 * cannot reject the whole scan and lose the batches that did succeed.
 */
async function runPool<T>(items: T[], size: number, handle: (item: T) => Promise<void>): Promise<void> {
    let index = 0;
    const width = Math.max(1, Math.min(size, items.length));
    const workers: Promise<void>[] = [];
    for (let i = 0; i < width; i++) {
        workers.push((async () => {
            for (;;) {
                const next = index++;
                if (next >= items.length) return;
                try {
                    await handle(items[next]);
                } catch {
                    // handle() already turns failures into error reports; this
                    // is only here so an unexpected throw cannot take the pool
                    // down with it.
                }
            }
        })());
    }
    await Promise.all(workers);
}

class Semaphore {
    private slots: number;
    private waiters: Array<() => void> = [];

    constructor(size: number) {
        this.slots = Math.max(1, Math.floor(size) || 1);
    }

    acquire(): Promise<() => void> {
        return new Promise<() => void>(resolve => {
            const grant = () => {
                let released = false;
                resolve(() => {
                    if (released) return;
                    released = true;
                    this.release();
                });
            };
            if (this.slots > 0) {
                this.slots--;
                grant();
            } else {
                this.waiters.push(() => {
                    this.slots--;
                    grant();
                });
            }
        });
    }

    private release(): void {
        this.slots++;
        const next = this.waiters.shift();
        if (next) next();
    }
}

interface LinkedSignal {
    signal: AbortSignal | undefined;
    timedOut(): boolean;
    done(): void;
}

/**
 * Combine the caller's signal with a timeout into one signal.
 *
 * `AbortSignal.any` and `AbortSignal.timeout` exist in current Chromium, but
 * this is loaded into whatever Electron the user happens to run, so both are
 * built by hand.
 */
function linkSignal(signal: AbortSignal | undefined, timeoutMs: number): LinkedSignal {
    const controller = newController();
    if (!controller) {
        // No AbortController at all: pass the caller's signal through and give
        // up on the timeout rather than failing the request.
        return { signal, timedOut: () => false, done: () => { /* nothing to unhook */ } };
    }

    let timedOut = false;
    const timer = setTimeout(() => {
        timedOut = true;
        try {
            controller.abort();
        } catch {
            // already aborted
        }
    }, timeoutMs);

    const onAbort = () => {
        try {
            controller.abort();
        } catch {
            // already aborted
        }
    };

    if (signal) {
        if (signal.aborted) onAbort();
        else {
            try {
                signal.addEventListener?.("abort", onAbort);
            } catch {
                // a signal-shaped object that is not an EventTarget: the
                // request simply cannot be cancelled early.
            }
        }
    }

    return {
        signal: controller.signal,
        timedOut: () => timedOut,
        done: () => {
            clearTimeout(timer);
            try {
                signal?.removeEventListener?.("abort", onAbort);
            } catch {
                // as above
            }
        },
    };
}

function newController(): AbortController | null {
    try {
        return typeof AbortController === "undefined" ? null : new AbortController();
    } catch {
        return null;
    }
}

async function safeJson(resp: Response): Promise<any> {
    try {
        return await resp.json();
    } catch {
        return null;
    }
}

async function safeText(resp: Response): Promise<string> {
    try {
        return (await resp.text()) || "";
    } catch {
        return "";
    }
}

function errorText(err: any): string {
    if (!err) return "unknown error";
    if (typeof err === "string") return err;
    return String(err.message || err);
}

function asArray(value: any): any[] {
    return Array.isArray(value) ? value : [];
}

function unique(items: string[]): string[] {
    const seen = new Set<string>();
    const out: string[] = [];
    for (const item of items) {
        if (seen.has(item)) continue;
        seen.add(item);
        out.push(item);
    }
    return out;
}

function chunks<T>(items: T[], size: number): T[][] {
    const out: T[][] = [];
    for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
    return out;
}

function maybeInt(value: any): number | null {
    if (value == null || value === "") return null;
    const n = Number(value);
    if (!isFinite(n)) return null;
    return Math.trunc(n);
}

function toNumber(value: any, fallback: number): number {
    const n = Number(value);
    return isFinite(n) ? n : fallback;
}

function clampInt(value: any, fallback: number, min: number, max: number): number {
    const n = Math.floor(Number(value));
    if (!isFinite(n)) return fallback;
    return Math.max(min, Math.min(max, n));
}

function stripSlash(url: string): string {
    return String(url).replace(/\/+$/, "");
}

function pad2(n: number): string {
    return n < 10 ? `0${n}` : String(n);
}

// Re-exported so callers that need to recognise (or raise) the same
// cancellation shape the client throws do not have to reach into
// api/ratelimit.ts for it.
export { abortError, isAbortError } from "./ratelimit";
