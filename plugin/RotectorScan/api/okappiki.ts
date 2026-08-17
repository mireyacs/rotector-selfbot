/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Okappiki API client — an alternative backend to api/rotector.ts. Ported from
 * rsb/okappiki.py.
 *
 *     GET https://okappiki.com/backend/api.php
 *         ?action=vencord_full_check&discord_id=<one id>
 *
 * One request answers for one Discord id, and the body carries three
 * independent sources at once:
 *
 *     {"success": true, "discord_id": "...",
 *      "okappiki": {"flagged": true, "reason": "...",
 *                   "roblox_username": null, "roblox_id": null},
 *      "rotector": {"flagged": true, "roblox_id": 9092847610,
 *                   "username": "...", "flag_type": 2, "reason": "..."},
 *      "mococo":   {"flagged": true, "score_sum": 40, "reason": "..."},
 *      "last_updated": "2026-08-03T08:21:58+00:00"}
 *
 * **This backend was written off once and should not be again.** An earlier
 * pass concluded that okappiki.com sends no CORS headers and is therefore
 * unreachable from a renderer. That conclusion came from probing an *invalid*
 * `action`, whose error path returns before the headers are written. The real
 * actions — `vencord_full_check`, and the cheaper `vencord_check_flag` the
 * DORM plugin uses for a flagged-or-not probe — answer with
 * `access-control-allow-origin: *`, so a plain `fetch()` works and no CSP
 * grant or main-process half is involved.
 *
 * **None of the endpoint is documented**, so everything below is what it was
 * observed doing rather than what it promises. Four observations shape the
 * code, all four carried over from the Python module:
 *
 *  - *There is no batch form.* `discord_ids`, comma-separated values and
 *    repeated parameters are all rejected. A ten-thousand-member server is ten
 *    thousand requests, against Rotector's ~218 for the same server. Anything
 *    that offers this backend to a user has to say so before the scan starts.
 *  - *Failures come back HTTP 200.* An unusable id returns
 *    `{"success": false, "message": "Invalid Discord ID"}` with a 200, so the
 *    status code cannot be trusted and `success` is what gets checked.
 *  - *Only the flag is guaranteed.* An unflagged member's source object is
 *    exactly `{"flagged": false}` — no reason, no ids, no nulls. Every other
 *    key is read as optional.
 *  - *Ids are not normalised consistently.* `00001212568604604629013` returns
 *    the same Rotector record as the canonical id while Okappiki's own list
 *    reports nothing, so the two sub-lookups disagree about what that id is.
 *    Ids are normalised here, before the request, rather than trusting either.
 *
 * Verdict tiering follows api/verdict.ts's existing rules. Only Rotector
 * publishes flag types with documented actionability, so **only Rotector's
 * flags reach THREAT**; an Okappiki or mococo sighting alone is CAUTION, which
 * is the verdict that already means "a person should look at this before
 * acting". That is not a conservatism knob — neither service publishes a claim
 * that its sighting is safe to act on, so there is nothing to grade a stronger
 * verdict against. api/triage.ts sits on top of these signals and answers the
 * other question, cautious or ban, from all three at once.
 *
 * Two rules that are not tuning parameters:
 *
 *  - **Concurrency stays at 4.** rsb/config.py records the measured curve over
 *    351 requests: 1.4 / 2.5 / 3.8 / 5.3 / 5.5 / 5.0 req/s at concurrency
 *    1..6, with p95 latency climbing from 0.9s to 1.6s over the last two
 *    steps. Throughput plateaus at 4–5 and *falls* at 6. Four is within 4% of
 *    peak at the best latency of any level, and a scan that runs for half an
 *    hour should not sit exactly on someone else's knee the whole time.
 *  - **Never fan out to go faster.** Every request here costs Okappiki a live
 *    Rotector lookup out of *their* budget. Spreading that over more
 *    connections, addresses or clients is the rate-limit circumvention
 *    Rotector's terms prohibit; one hop further down the chain does not make it
 *    something else. The limiter comes from `sharedLimiter(OKAPPIKI_BASE)` for
 *    the same reason api/rotector.ts's does — a second client must not open a
 *    second budget.
 *
 * Rotector's Terms of Use cap retention at 24h and an Okappiki response embeds
 * a Rotector verdict, so the same ceiling applies here by default: the cache is
 * in memory only, is swept on a timer, and its TTL is clamped to
 * MAX_CACHE_TTL_MS. Nothing in this module writes to disk, DataStore or the
 * settings store.
 *
 * That ceiling is lifted only by the `retainBeyondTerms` option, off by
 * default, and even then only as far as MAX_RETAINED_TTL_MS — an unexpiring
 * cache is a memory leak as well as a licensing problem. Rotector's retention
 * terms travel with an Okappiki answer because a Rotector answer is inside it,
 * so the same flag governs both clients and neither is the looser one.
 * Anything answered from beyond the 24-hour window is reported as stale with
 * its age wherever it is shown — see `isStale` and `staleNote` in api/types.ts.
 */

import { batchCost, isAbortError, RateLimiter, sharedLimiter, sleep } from "./ratelimit";
import type { ClientOptions, ScanOptions, ScanProgress } from "./rotector";
import { MAX_CACHE_TTL_MS, MAX_RETAINED_TTL_MS, TransportError } from "./rotector";
import { PARTIES, PARTY_LABEL } from "./triage";
import { emptyReport, MemberReport, RobloxAccount } from "./types";
import type { Signal } from "./verdict";
import { Verdict, verdictForFlag } from "./verdict";

export const OKAPPIKI_BASE = "https://okappiki.com";
export const OKAPPIKI_PATH = "/backend/api.php";

/** the action that answers with all three sources at once */
export const FULL_CHECK_ACTION = "vencord_full_check";
/**
 * The cheap flagged-or-not probe. Used only by `ping()`: it does not trigger
 * the live Rotector lookup a full check does, so a connectivity check does not
 * spend somebody else's API budget to learn that the network is up.
 */
export const FLAG_CHECK_ACTION = "vencord_check_flag";

/**
 * The sources a response can carry, in the order they are shown.
 *
 * Deliberately api/triage.ts's own list rather than a second copy: the parser
 * and the decision table have to agree about which databases exist and in what
 * order, and two literals in two files is how they stop agreeing.
 */
export const SOURCES = PARTIES;

/**
 * Discord's epoch puts every real snowflake at 17 digits or more. The endpoint
 * happily answers for "1" — with a confident-looking Rotector record — so
 * anything shorter is refused here instead of being reported as a finding.
 */
export const MIN_SNOWFLAKE_DIGITS = 17;

/**
 * How long one full check takes, measured 2026-08-16 over 351 requests: p50
 * 0.72–0.88s and p95 0.87–1.29s across concurrency 1 to 6, flat enough that a
 * single constant is honest. Used to bound the ETA, because the rate limiter is
 * deliberately set above what the client can produce and would otherwise
 * promise a scan that never arrives.
 */
export const MEAN_LATENCY = 0.8;

/** requests per window; see OkappikiConfig in rsb/config.py */
export const DEFAULT_RATE_LIMIT = 6;
/** window length in seconds */
export const DEFAULT_WINDOW = 1;
export const DEFAULT_RESERVE = 0;
/** the measured knee, minus the last 4% for headroom */
export const DEFAULT_CONCURRENCY = 4;

/**
 * The hard ceiling on concurrency, and not a preference.
 *
 * The measured curve above turns *down* at 6 while latency keeps climbing, so
 * a higher number is slower for us and heavier for them. Five is the measured
 * peak and the most this will accept; the default sits one below it.
 */
export const MAX_CONCURRENCY = 5;

/** how often the client drops expired responses it was never asked for again */
export const SWEEP_INTERVAL_MS = 5 * 60 * 1000;

/** seconds; a renderer fetch has no timeout of its own, so one is imposed */
const REQUEST_TIMEOUT_MS = 30_000;

/** the 23h Terms of Use ceiling, with a literal fallback (no type checking) */
const CEILING_MS = typeof MAX_CACHE_TTL_MS === "number" && MAX_CACHE_TTL_MS > 0
    ? MAX_CACHE_TTL_MS
    : 23 * 3600 * 1000;

/**
 * The ceiling that applies once `retainBeyondTerms` is on. Same literal
 * fallback, and for the sharper reason: if this import ever came back
 * `undefined`, the clamp would evaluate to NaN and a cache entry would never
 * look expired at all.
 */
const RETAINED_CEILING_MS = typeof MAX_RETAINED_TTL_MS === "number" && MAX_RETAINED_TTL_MS > 0
    ? MAX_RETAINED_TTL_MS
    : 365 * 24 * 3600 * 1000;

const STOPPED = "scan stopped before this member was checked";

export class OkappikiError extends Error {
    constructor(message: string) {
        super(message);
        this.name = "OkappikiError";
    }
}

interface CacheEntry {
    at: number;
    /** the raw response body, exactly as it arrived */
    value: any;
}

// --------------------------------------------------------------------------
// parsing
// --------------------------------------------------------------------------

/**
 * A canonical snowflake, or `null` if it is not one.
 *
 * Leading zeros are stripped because the endpoint's two sub-lookups disagree
 * about whether `0001234` is `1234`.
 */
export function normaliseId(raw: any): string | null {
    const text = String(raw ?? "").trim();
    if (!text.length || !/^\d+$/.test(text)) return null;
    const stripped = text.replace(/^0+/, "") || "0";
    if (stripped.length < MIN_SNOWFLAKE_DIGITS) return null;
    return stripped;
}

/**
 * Turn one response body into one signal per source that answered.
 *
 * Sources absent from the body are absent from the result: not answering and
 * answering "not flagged" are different facts, and flattening them would let a
 * backend outage read as a clean result. api/triage.ts depends on exactly this
 * — its rule R6 exists to catch the missing one.
 */
export function parseSignals(body: any): Signal[] {
    const signals: Signal[] = [];
    if (!body || typeof body !== "object") return signals;

    for (const source of SOURCES) {
        const raw = (body as any)[source];
        if (!raw || typeof raw !== "object" || Array.isArray(raw)) continue;

        const flagged = Boolean(raw.flagged);
        const flagType = maybeInt(raw.flag_type);

        let verdict: Verdict;
        if (!flagged) {
            verdict = Verdict.NO_DETECTIONS;
        } else if (source === "rotector") {
            // The one source with documented flag types. verdictForFlag holds
            // the rule that only Flagged and Confirmed are actionable — and
            // answers UNKNOWN when the flag type is missing, which is the
            // honest reading of "flagged, but they will not say as what".
            verdict = verdictForFlag(flagType);
        } else {
            // Undocumented, unverified, and no flag type to grade it by. This
            // is the ceiling for Okappiki's own list and for mococo, and it is
            // not a knob: neither publishes a claim that its sighting is safe
            // to act on, so there is nothing here that could reach THREAT.
            verdict = Verdict.CAUTION;
        }

        signals.push({
            source,
            flagged,
            verdict,
            reason: text(raw.reason),
            score: maybeInt(raw.score_sum),
            robloxId: maybeInt(raw.roblox_id),
            // The sources disagree on the key: Okappiki's own object says
            // "roblox_username", the embedded Rotector one says "username".
            robloxUsername: text(raw.roblox_username) || text(raw.username) || null,
            flagType,
        });
    }
    return signals;
}

/**
 * Build a report from one response body.
 *
 * Any source naming a Roblox account also produces a RobloxAccount, so the
 * ledger and the export columns — both written against Rotector's shape — have
 * the same thing to show they always had. The account carries no flag type
 * unless Rotector supplied one, so `accountVerdict` reads UNKNOWN for an
 * Okappiki or mococo sighting and the CAUTION comes from the signal instead.
 */
export function parseReport(discordId: string, body: any): MemberReport {
    const report = emptyReport(String(discordId));
    report.signals = parseSignals(body);

    for (const signal of report.signals) {
        if (!signal.flagged || signal.robloxId == null) continue;
        report.accounts.push(accountFor(signal));
    }
    return report;
}

function accountFor(signal: Signal): RobloxAccount {
    const reasons: RobloxAccount["reasons"] = {};
    if (signal.reason) {
        // The Python keys this `f"{signal.label} finding"`, where `label` is
        // `source.title()`. The plugin already has one canonical table of
        // service names in api/triage.ts, and a reason headed "Detective
        // Okappiki finding" reads better in the report than "Okappiki
        // finding"; the key is a display string only, so this is the one place
        // the port deliberately says it differently.
        reasons[`${sourceLabel(signal.source)} finding`] = { message: signal.reason };
    }
    return {
        userId: signal.robloxId as number,
        username: signal.robloxUsername || `(${signal.source})`,
        detectedAt: null,
        sources: [],
        flagType: signal.flagType ?? null,
        category: null,
        confidence: null,
        reasons,
        lastUpdated: null,
    };
}

function sourceLabel(source: string): string {
    const known = PARTY_LABEL?.[source];
    if (known) return known;
    const name = String(source || "");
    return name ? name.charAt(0).toUpperCase() + name.slice(1) : "unknown source";
}

/** Ids whose report carries at least one flagged source. */
export function lookupIds(reports: Iterable<MemberReport>): string[] {
    const out: string[] = [];
    for (const report of reports || []) {
        if (report?.signals?.some(s => s.flagged)) out.push(report.discordId);
    }
    return out;
}

// --------------------------------------------------------------------------
// client
// --------------------------------------------------------------------------

/**
 * Mirrors RotectorClient's surface, so api/backend.ts can swap them.
 *
 * The app holds one backend and does not care which; anything it reaches for —
 * `scanMembers`, `limiter`, `estimateSeconds`, `purgeCache`, `ping` — exists on
 * both. The differences are all inside: no API key, no second hop, and no
 * batching, so ids are dispatched one at a time under a worker pool instead of
 * being buffered into hundreds.
 */
export class OkappikiClient {
    readonly limiter: RateLimiter;
    readonly baseUrl: string;
    readonly maxRetries: number;
    /** in-memory cache lifetime in ms, already clamped under the live ceiling */
    readonly cacheTtlMs: number;
    /** whether this client was built with the 24-hour ceiling lifted */
    readonly retainBeyondTerms: boolean;
    /**
     * How many requests may be in flight at once. Held as a plain number as
     * well as a semaphore because a semaphore's counter drains as work starts,
     * and an ETA computed from it would wander for the length of the scan.
     */
    readonly maxInflight: number;

    private sem: Semaphore;
    private cache = new Map<string, CacheEntry>();
    private sweepTimer: any = null;

    constructor(opts: ClientOptions = {}) {
        this.baseUrl = stripSlash((opts as any)?.baseUrl || OKAPPIKI_BASE);
        // One limiter per host, not per client: the budget being spent is the
        // service's and it is counted per address, so a second client aimed at
        // the same host has to spend from the same window or neither of them is
        // measuring anything. The duck-typed check is because this file is
        // loaded without type checking and an injected limiter comes from
        // outside.
        //
        // The options are handed over **unresolved**, exactly as
        // api/rotector.ts does, and that is the whole point of the shape:
        // `sharedLimiter` forwards to `RateLimiter.configure`, which only skips
        // a field that is null or undefined. Substituting the DEFAULT_* numbers
        // here would turn every client built without explicit options — the
        // connectivity probe in api/host.ts is exactly that — into a caller with
        // an opinion, and it would silently *raise* a budget the user had
        // lowered in settings the moment somebody opened the status panel. An
        // undefined field leaves the running limiter alone, and on the very
        // first creation RateLimiter's own constructor defaults apply, which are
        // more conservative per second than Okappiki's measured 6-per-second, so
        // the no-options path cannot overspend either. The documented Okappiki
        // defaults are not lost: api/backend.ts's BACKEND_DEFAULTS carries them,
        // so every client the plugin actually scans with is built with them
        // spelled out.
        const injected = (opts as any)?.limiter;
        this.limiter = injected && typeof injected.acquire === "function"
            ? injected as RateLimiter
            : sharedLimiter(this.baseUrl, {
                limit: opts.rateLimit,
                window: opts.window,
                reserve: opts.reserve,
            });
        this.maxRetries = clampInt(opts.maxRetries, 4, 0, 10);
        // Rotector's 24h ceiling is the default here too, because a Rotector
        // verdict is inside every response this client caches. The operator can
        // lift it deliberately — see `retainBeyondTerms` — and when they have,
        // anything served from beyond the window is reported as stale rather
        // than passed off as current.
        this.retainBeyondTerms = (opts as any)?.retainBeyondTerms === true;
        const ceiling = this.retainBeyondTerms ? RETAINED_CEILING_MS : CEILING_MS;
        this.cacheTtlMs = Math.min(
            Math.max(0, toNumber(opts.cacheTtl, 3600) * 1000),
            ceiling,
        );
        // Clamped to MAX_CONCURRENCY rather than to whatever the user typed:
        // see that constant. Going above the measured knee makes the scan
        // slower and the service's day worse at the same time.
        this.maxInflight = clampInt(opts.concurrency, DEFAULT_CONCURRENCY, 1, MAX_CONCURRENCY);
        this.sem = new Semaphore(this.maxInflight);
    }

    // -- transport ---------------------------------------------------------

    /**
     * One request, through the concurrency semaphore, with a timeout.
     *
     * Only `accept` is sent. That header is CORS-safelisted, so this stays a
     * simple request and never triggers a preflight the endpoint has not been
     * observed answering. There is deliberately no `user-agent`: browsers
     * forbid setting it, and the attempt would fail the fetch outright.
     *
     * `fetch` rejects with a TypeError when the request never left the browser
     * — a CSP refusal, a missing `Access-Control-Allow-Origin`, DNS failure or
     * no network all land here with no status code and no way to tell them
     * apart from JS. TransportError is api/rotector.ts's shape for exactly that
     * and is reused so one `instanceof` check in api/host.ts covers both
     * backends.
     */
    private async send(url: string, signal?: AbortSignal): Promise<Response> {
        const link = linkSignal(signal, REQUEST_TIMEOUT_MS);
        const release = await this.sem.acquire();
        try {
            return await fetch(url, {
                method: "GET",
                headers: { accept: "application/json" },
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
                    `okappiki: no response after ${Math.round(REQUEST_TIMEOUT_MS / 1000)}s`,
                    false,
                );
            }
            if (isAbortError(err)) throw err;
            if (err instanceof TypeError || err?.name === "TypeError") {
                throw new TransportError(
                    `okappiki: the request never left the browser (${err?.message || "network error"})`,
                    true,
                );
            }
            throw new TransportError(`okappiki: ${err?.message || String(err)}`, false);
        } finally {
            release();
            link.done();
        }
    }

    private url(action: string, discordId: string): string {
        const params = `action=${encodeURIComponent(action)}&discord_id=${encodeURIComponent(discordId)}`;
        return `${this.baseUrl}${OKAPPIKI_PATH}?${params}`;
    }

    /**
     * One rate-limited GET, with bounded soft retries.
     *
     * Retries are capped on purpose: a permanently throttled or broken endpoint
     * must raise rather than loop, because a lookup that returns empty is
     * indistinguishable from "nothing found" and that would read as a clean
     * bill of health for the member.
     *
     * The service publishes no rate limit, no `Retry-After` and no
     * `x-ratelimit-*` headers — and even if it did, a cross-origin response
     * exposes none of them to JS. Backoff is therefore blind, exactly as it is
     * for Rotector.
     */
    private async get(discordId: string, action: string, signal?: AbortSignal): Promise<any> {
        const maxSoft = this.maxRetries + 4;
        let soft = 0;

        for (;;) {
            // One id, one request unit — there is no batch form to cost more.
            // batchCost(1) rather than a bare 1 so the two backends agree about
            // what a unit is.
            await this.limiter.acquire(batchCost(1), signal);
            const resp = await this.send(this.url(action, discordId), signal);

            if (resp.status === 429) {
                // Backpressure, not a fault. No Retry-After is readable here,
                // so the limiter holds every caller for a full window.
                this.limiter.penalise();
                soft++;
                if (soft > maxSoft) {
                    throw new OkappikiError(`still rate limited after ${soft} retries`);
                }
                continue;
            }

            if (resp.status >= 500) {
                soft++;
                if (soft > maxSoft) {
                    throw new OkappikiError(
                        `server error ${resp.status} after ${soft} retries`,
                    );
                }
                await sleep(Math.min(Math.pow(2, soft), 15) * 1000, signal);
                continue;
            }

            if (resp.status >= 400) {
                const body = await safeText(resp);
                throw new OkappikiError(`HTTP ${resp.status}: ${body.slice(0, 200)}`);
            }

            const body = await safeJson(resp);
            if (body == null || typeof body !== "object" || Array.isArray(body)) {
                throw new OkappikiError("the response was not a JSON object");
            }
            if (!(body as any).success) {
                // A 200 carrying a refusal; the message is the only detail
                // given. This is why the status code is not trusted.
                throw new OkappikiError(text((body as any).message) || "request refused");
            }
            return body;
        }
    }

    // -- caching -----------------------------------------------------------

    private cacheGet(key: string): any | null {
        const entry = this.cache.get(key);
        if (!entry) return null;
        if (Date.now() - entry.at > this.cacheTtlMs) {
            this.cache.delete(key);
            return null;
        }
        return entry.value;
    }

    private cachePut(key: string, value: any): void {
        if (this.cacheTtlMs <= 0) return;
        this.cache.set(key, { at: Date.now(), value });
        this.ensureSweep();
    }

    /**
     * Drop expired responses on a timer, not only when they are read again.
     *
     * Evicting on read is not enough on its own: an id nobody looks up a second
     * time is never read again, so its raw body — which embeds a Rotector
     * verdict — would sit in memory for the whole Discord session. That is
     * exactly the retention Rotector's terms forbid.
     */
    private ensureSweep(): void {
        if (this.sweepTimer !== null || this.cacheTtlMs <= 0) return;
        this.sweepTimer = setInterval(() => this.sweep(), SWEEP_INTERVAL_MS) as any;
    }

    private sweep(): void {
        const now = Date.now();
        for (const [key, entry] of Array.from(this.cache)) {
            if (now - entry.at > this.cacheTtlMs) this.cache.delete(key);
        }
        if (!this.cache.size) this.clearSweep();
    }

    private clearSweep(): void {
        if (this.sweepTimer === null) return;
        clearInterval(this.sweepTimer);
        this.sweepTimer = null;
    }

    /** Drop every cached response. */
    purgeCache(): void {
        this.cache.clear();
        this.clearSweep();
    }

    // -- endpoints ---------------------------------------------------------

    /** One member, cached. Never throws: a failure becomes a report error. */
    async lookupMember(id: string, signal?: AbortSignal): Promise<MemberReport> {
        const given = String(id ?? "");
        const canonical = normaliseId(given);
        if (canonical == null) {
            return emptyReport(given, `not a Discord id: ${given.slice(0, 32) || "(empty)"}`);
        }

        const cached = this.cacheGet(canonical);
        if (cached != null) return parseReport(given, cached);

        let body: any;
        try {
            body = await this.get(canonical, FULL_CHECK_ACTION, signal);
        } catch (err: any) {
            // Including aborts. A stopped scan is a partial answer rather than
            // a failure, and every caller of this wants a report back for the
            // id it asked about — never a rejection that loses the rest.
            return emptyReport(given, isAbortError(err) ? STOPPED : errorText(err));
        }

        this.cachePut(canonical, body);
        return parseReport(given, body);
    }

    /**
     * The raw body, for the detail pane.
     *
     * Unlike Rotector there is no richer single-user form — this is the same
     * endpoint a scan uses, which is also why it raises rather than returning
     * an error report: the caller asked for the raw answer, not a verdict.
     */
    async lookupDiscordUserDetail(id: string, signal?: AbortSignal): Promise<any> {
        const given = String(id ?? "");
        const canonical = normaliseId(given);
        if (canonical == null) {
            throw new OkappikiError(`not a Discord id: ${given.slice(0, 32) || "(empty)"}`);
        }
        const cached = this.cacheGet(canonical);
        if (cached != null) return cached;
        const body = await this.get(canonical, FULL_CHECK_ACTION, signal);
        this.cachePut(canonical, body);
        return body;
    }

    // -- scanning ----------------------------------------------------------

    /**
     * Scan a set of Discord ids, one request each.
     *
     * There is no batch endpoint, so this is a worker pool over
     * `lookupMember` — `maxInflight` wide and no wider. The guarantees match
     * api/rotector.ts's because the surfaces above depend on them:
     *
     *  - every input id gets a report, even when its request failed outright;
     *  - partial results are published as each one lands, so the ledger fills
     *    while the scan runs;
     *  - aborting resolves rather than rejecting, and whatever was already
     *    found is kept — a stopped scan is a partial answer, never "nothing
     *    found".
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
                const p: ScanProgress = {
                    stage,
                    done: doneOverride == null ? done : doneOverride,
                    total,
                };
                opts.onProgress(p);
            } catch {
                // A UI callback throwing must not abort the scan.
            }
        };

        if (!total) {
            progress("Scan complete");
            return reports;
        }

        progress("Checking members", 0);

        const handle = async (uid: string) => {
            const report = signal?.aborted
                ? emptyReport(uid, STOPPED)
                : await this.lookupMember(uid, signal);
            done++;
            reports.set(uid, report);
            progress(report.error ? "Some lookups failed" : "Checking members");
            if (opts.onPartial) {
                try {
                    opts.onPartial([report]);
                } catch {
                    // as above
                }
            }
        };

        await runPool(wanted, this.maxInflight, handle);

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

    // -- estimates ---------------------------------------------------------

    capacityUnitsPerSec(): number {
        return this.limiter.capacityPerSec();
    }

    /**
     * One request per member, one hop, no batching — so this is division.
     *
     * Deliberately not the arithmetic api/rotector.ts uses, which models
     * Rotector's batching and its second pass and would predict a scan roughly
     * a hundred times faster than this backend can manage.
     *
     * Bounded by *two* ceilings, not one. The rate limiter is the obvious one,
     * but it is deliberately configured above what this client can actually
     * produce, so on its own it would promise a scan that never arrives. The
     * real constraint is how many requests can be in flight at once divided by
     * how long one takes, and measurement says one takes about MEAN_LATENCY.
     * Whichever ceiling is lower is the one the scan will actually hit.
     */
    estimateSeconds(members: number): number | null {
        const n = Math.floor(toNumber(members, 0));
        if (n <= 0) return null;

        const rateBound = this.capacityUnitsPerSec();
        const inflightBound = MEAN_LATENCY > 0 ? this.maxInflight / MEAN_LATENCY : 0;
        const bounds = [rateBound, inflightBound].filter(b => isFinite(b) && b > 0);
        if (!bounds.length) return null;

        const capacity = Math.min(...bounds);
        if (capacity <= 0) return null;
        return n / capacity;
    }

    /**
     * One cheap request, to tell "works" from "CORS/CSP blocked" from
     * "service down".
     *
     * Uses the flag-only action rather than a full check: a full check makes
     * Okappiki perform a live Rotector lookup out of their budget, and a
     * connectivity probe has no business spending that. It still costs a
     * request unit from the shared limiter, with a hard cap so a rate-limit
     * hold cannot make it hang.
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
            // "1" is not a real snowflake and normaliseId would refuse it for a
            // lookup. It is fine here precisely because this asks nothing about
            // a person: whatever the endpoint makes of it, an answer proves the
            // transport works, which is the only thing being probed.
            const resp = await this.send(this.url(FLAG_CHECK_ACTION, "1"), controller?.signal);

            if (resp.status === 429) {
                this.limiter.penalise();
                return { ok: false, error: "rate limited (HTTP 429)", corsBlocked: false };
            }
            if (resp.status >= 400) {
                const body = await safeText(resp);
                return {
                    ok: false,
                    error: `HTTP ${resp.status}${body ? `: ${body.slice(0, 120)}` : ""}`,
                    corsBlocked: false,
                };
            }
            const body = await safeJson(resp);
            if (body == null || typeof body !== "object" || Array.isArray(body)) {
                return { ok: false, error: "the response was not JSON", corsBlocked: false };
            }
            if (typeof (body as any).success !== "boolean") {
                return {
                    ok: false,
                    error: "the response was JSON but not in this endpoint's shape",
                    corsBlocked: false,
                };
            }
            // A `success: false` for the probe id is still an answer: the
            // request left the browser, reached okappiki.com and came back
            // readable. Reporting that as a failure would have the status panel
            // blame the service for the id the probe made up.
            return { ok: true };
        } catch (err: any) {
            if (err instanceof TransportError) {
                return { ok: false, error: err.message, corsBlocked: (err as any).corsBlocked === true };
            }
            if (isAbortError(err)) {
                // Nothing was sent — almost always the limiter still holding
                // callers back. Saying "the service answered" would blame it
                // for our own queue.
                return { ok: false, error: "the check timed out", corsBlocked: false, neverSent: true };
            }
            return { ok: false, error: errorText(err), corsBlocked: false };
        } finally {
            clearTimeout(timer);
        }
    }
}

/** How many reports in a scan result came back with an error. */
export function countUnanswered(reports: Iterable<MemberReport>): number {
    let n = 0;
    for (const report of reports || []) if (report?.error) n++;
    return n;
}

// --------------------------------------------------------------------------
// small utilities
//
// Private copies of the ones in api/rotector.ts. That module does not export
// them and this one does not own it, so they are duplicated rather than
// reached for — four small pure functions is a cheaper price than editing a
// file another agent owns.
// --------------------------------------------------------------------------

/**
 * A fixed-size worker pool. `handle` is expected to swallow its own errors —
 * it does above — but anything that escapes is contained here so one bad
 * request cannot reject the whole scan and lose the answers that did arrive.
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

/** Python's `_text`: a trimmed string, or "" for null/undefined/empty. */
function text(value: any): string {
    if (value == null) return "";
    const out = String(value).trim();
    return out;
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
