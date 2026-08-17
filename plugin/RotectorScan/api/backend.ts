/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Picks the lookup backend a scan runs against. Ported from rsb/backend.py.
 *
 * The plugin holds exactly one backend and does not branch on which. Both
 * clients expose the same surface — `scanMembers`, `limiter`,
 * `estimateSeconds`, `capacityUnitsPerSec`, `purgeCache`, `ping` — so
 * everything that differs between them stays behind `buildBackend`.
 *
 * They are alternatives rather than layers. Okappiki's response happens to
 * carry a Rotector verdict alongside its own two sources, so choosing it does
 * not mean giving Rotector's answer up; it means asking a different service for
 * it, with a different rate ceiling, no batch endpoint, and two extra sources
 * whose sightings are CAUTION-strength by construction.
 *
 * What the choice actually costs, because the scan UI has to say it before a
 * scan starts:
 *
 *  - Rotector answers a hundred ids per request and then makes a second pass
 *    over the linked Roblox accounts. A ten-thousand-member server is roughly
 *    218 requests.
 *  - Okappiki answers one id per request, full stop. The same server is ten
 *    thousand requests, each of which costs Okappiki a live Rotector lookup out
 *    of *their* budget. That is the reason the concurrency ceiling in
 *    api/okappiki.ts is not a preference, and the reason nothing here may be
 *    made faster by spreading it over more connections.
 *
 * This module is deliberately free of imports from settings.ts and store.ts: it
 * is the seam the rest of the plugin builds a client through, and a seam that
 * reads settings for itself would be a second place the backend choice is made.
 * `backendOptionsFrom` takes a plain record instead, so the caller keeps
 * ownership of where settings come from.
 */

import {
    DEFAULT_CONCURRENCY as OKAPPIKI_CONCURRENCY,
    DEFAULT_RATE_LIMIT as OKAPPIKI_RATE_LIMIT,
    DEFAULT_RESERVE as OKAPPIKI_RESERVE,
    DEFAULT_WINDOW as OKAPPIKI_WINDOW,
    MAX_CONCURRENCY as OKAPPIKI_MAX_CONCURRENCY,
    OKAPPIKI_BASE,
    OkappikiClient,
} from "./okappiki";
import type { RateLimiter } from "./ratelimit";
import type { ClientOptions, ScanOptions } from "./rotector";
import { MAX_CACHE_TTL_MS, MAX_RETAINED_TTL_MS, RotectorClient } from "./rotector";
import { APPEAL_LINKS } from "./triage";
import type { MemberReport } from "./types";
import { ATTRIBUTION } from "./verdict";

export type BackendName = "rotector" | "okappiki";

export const BACKENDS: BackendName[] = ["rotector", "okappiki"];

/** the default, and the one every other module assumes when nothing is set */
export const DEFAULT_BACKEND: BackendName = "rotector";

/** what each backend is called in the UI */
export const BACKEND_LABELS: Record<string, string> = {
    rotector: "Rotector",
    okappiki: "Okappiki",
};

/**
 * Where each backend's data comes from, for the footer of every findings
 * surface.
 *
 * Rotector's terms require that an action taken on their data attributes them
 * or links rotector.com so the affected person can appeal, and the same
 * courtesy is owed to the other two services an Okappiki response carries, so
 * Okappiki's line names all three and links all three appeal routes.
 */
export const BACKEND_ATTRIBUTIONS: Record<string, string> = {
    rotector: ATTRIBUTION,
    okappiki:
        `Data: Okappiki (${OKAPPIKI_BASE}), which reports Okappiki, Rotector and ` +
        `mococo findings together - appeals at ${APPEAL_LINKS.okappiki}, ` +
        `${APPEAL_LINKS.rotector} and ${APPEAL_LINKS.mococo}`,
};

/**
 * The sentence a surface must show before running a scan on this backend, or
 * null when there is nothing extra to say.
 */
export const BACKEND_WARNINGS: Record<string, string> = {
    okappiki:
        "Okappiki has no batch endpoint: this is one request per member, so a " +
        "10,000-member server is 10,000 requests where Rotector would take about " +
        "218. Every one of them costs Okappiki a live Rotector lookup out of their " +
        "budget, so it runs four at a time and cannot be made to go faster.",
};

export function backendLabel(name: string): string {
    const key = normaliseName(name);
    return BACKEND_LABELS[key] ?? (name ? String(name) : "?");
}

export function backendAttribution(name: string): string {
    const key = normaliseName(name);
    return BACKEND_ATTRIBUTIONS[key] ?? ATTRIBUTION;
}

/** The extra sentence this backend needs before a scan, or null. */
export function backendWarning(name: string): string | null {
    return BACKEND_WARNINGS[normaliseName(name)] ?? null;
}

export function isBackendName(name: any): name is BackendName {
    return BACKENDS.indexOf(normaliseName(name) as BackendName) !== -1;
}

// --------------------------------------------------------------------------
// the shared surface
// --------------------------------------------------------------------------

/** the result shape both clients' connectivity probe returns */
export interface BackendPing {
    ok: boolean;
    error?: string;
    corsBlocked?: boolean;
    neverSent?: boolean;
}

/**
 * Everything a caller may use without knowing which backend it holds.
 *
 * Deliberately the *intersection* of the two clients rather than the union.
 * Rotector's two-hop batch endpoints and Okappiki's per-member lookup have no
 * equivalent on the other side, so a surface written against this interface
 * cannot accidentally depend on one backend's shape and then break under the
 * other. `lookupMember` is the one optional member, because a caller that
 * genuinely wants a single id can use it when it is there and fall back to a
 * one-element `scanMembers` when it is not.
 */
export interface LookupBackend {
    readonly limiter: RateLimiter;
    readonly baseUrl: string;
    scanMembers(ids: string[], opts?: ScanOptions): Promise<Map<string, MemberReport>>;
    lookupDiscordUserDetail(id: string, signal?: AbortSignal): Promise<any>;
    estimateSeconds(members: number): number | null;
    capacityUnitsPerSec(): number;
    purgeCache(): void;
    ping(): Promise<BackendPing>;
    lookupMember?(id: string, signal?: AbortSignal): Promise<MemberReport>;
}

/**
 * The client for `name`.
 *
 * An unrecognised name throws rather than quietly falling back to Rotector: a
 * typo in the settings should not silently scan against a service the operator
 * did not choose, and a scan that ran against the wrong service is not
 * something an error message afterwards can undo. Callers that want a safe
 * default should pass `DEFAULT_BACKEND` explicitly.
 */
export function buildBackend(name: string, opts: ClientOptions = {}): LookupBackend {
    const key = normaliseName(name);
    if (!isBackendName(key)) {
        throw new Error(
            `unknown scan backend ${JSON.stringify(String(name ?? ""))}; ` +
            `expected one of ${BACKENDS.join(", ")}`
        );
    }
    if (key === "okappiki") {
        // No API key is passed through, and that is on purpose: Okappiki takes
        // none, and forwarding a Rotector key to a third-party host would hand
        // someone else's service a credential it never asked for.
        const rest: ClientOptions = { ...(opts || {}) };
        delete (rest as any).apiKey;
        return new OkappikiClient(rest) as unknown as LookupBackend;
    }
    return new RotectorClient(opts) as unknown as LookupBackend;
}

// --------------------------------------------------------------------------
// options, per backend
// --------------------------------------------------------------------------

/**
 * The defaults from rsb/config.py, per backend.
 *
 * Kept here rather than in settings.ts so that a caller building a client
 * without a settings store — a test, a probe, a first run before the plugin is
 * registered — still gets the numbers the Python project measured rather than
 * whatever the other backend's defaults happened to be. Pointing Rotector's
 * 50-per-10s at Okappiki would be a fifty-fold overspend of somebody's budget
 * on the first scan.
 */
export const BACKEND_DEFAULTS: Record<BackendName, ClientOptions> = {
    rotector: {
        rateLimit: 50,
        window: 10,
        reserve: 5,
        concurrency: 3,
        cacheTtl: 3600,
    },
    okappiki: {
        rateLimit: OKAPPIKI_RATE_LIMIT,
        window: OKAPPIKI_WINDOW,
        reserve: OKAPPIKI_RESERVE,
        concurrency: OKAPPIKI_CONCURRENCY,
        cacheTtl: 3600,
    },
};

// --------------------------------------------------------------------------
// retention
//
// Ported from rsb/backend.py's effective_cache_ttl. The two settings that shape
// it — `retainBeyondTerms` and `retentionHours` — are read here rather than in
// settings.ts for the same reason the per-backend keys are: this is the seam a
// client is built through, so the arithmetic that decides how long findings
// live has one home, and store.ts and features/history.ts can reach it without
// importing the settings module they are imported by.
// --------------------------------------------------------------------------

/** the shipped value of `retentionHours`: a week */
export const RETENTION_HOURS_DEFAULT = 168;

/**
 * A year, matching the ceiling both clients clamp to. Past that it is a memory
 * leak rather than a retention policy, and a year-old flag status is not
 * evidence about anybody.
 */
export const RETENTION_HOURS_MAX = 8760;

/** the two ceilings, with literal fallbacks because there is no type checking */
const TERMS_CEILING_MS = typeof MAX_CACHE_TTL_MS === "number" && MAX_CACHE_TTL_MS > 0
    ? MAX_CACHE_TTL_MS
    : 23 * 3600 * 1000;
const RETAINED_CEILING_MS = typeof MAX_RETAINED_TTL_MS === "number" && MAX_RETAINED_TTL_MS > 0
    ? MAX_RETAINED_TTL_MS
    : 365 * 24 * 3600 * 1000;

/**
 * Whether the operator has chosen to keep findings past the terms' window.
 *
 * Strictly `=== true`, so a missing settings store, a half-written settings
 * file or a truthy-but-not-boolean value all read as off. The permissive
 * direction is the one that breaks a promise here.
 */
export function retainBeyondTermsFrom(stored: Record<string, any> | null | undefined): boolean {
    return (stored && typeof stored === "object" ? stored.retainBeyondTerms : undefined) === true;
}

/**
 * How long findings are kept once the ceiling is lifted, in hours.
 *
 * Clamped to 1..8760 exactly as rsb/config.py validates it, and only consulted
 * while `retainBeyondTerms` is on — while it is off the value is ignored
 * entirely, which is what its own description promises.
 */
export function retentionHoursFrom(stored: Record<string, any> | null | undefined): number {
    const s = stored && typeof stored === "object" ? stored : {};
    return Math.floor(num(s.retentionHours, RETENTION_HOURS_DEFAULT, 1, RETENTION_HOURS_MAX));
}

/**
 * The cache lifetime a client should be built with, in **seconds**.
 *
 * While `retainBeyondTerms` is off this is exactly the configured `cacheTtl`,
 * which the client then clamps under the 24-hour ceiling Rotector's Terms of
 * Use set. With it on, the operator has said how long findings should be kept
 * in `retentionHours`, and that is the number that decides — a one-hour cache
 * would otherwise throw the data away long before the window they asked for was
 * up, which is not what switching the setting on was for.
 */
export function effectiveCacheTtl(stored: Record<string, any> | null | undefined, configured: number): number {
    if (!retainBeyondTermsFrom(stored)) return configured;
    return retentionHoursFrom(stored) * 3600;
}

/**
 * The age past which a finding may no longer be held at all, in milliseconds.
 *
 * This is the ceiling, not the TTL: store.ts and features/history.ts enforce it
 * on every read, every write and every sweep, so a finding is dropped at this
 * age whatever a cache thinks. Read it fresh each time rather than caching it,
 * so flipping the setting takes effect without a reload.
 */
export function retentionCeilingMsFrom(stored: Record<string, any> | null | undefined): number {
    if (!retainBeyondTermsFrom(stored)) return TERMS_CEILING_MS;
    const asked = retentionHoursFrom(stored) * 3600 * 1000;
    return Math.max(0, Math.min(asked, RETAINED_CEILING_MS));
}

/**
 * Turn a settings record into the options for one backend.
 *
 * The two backends read different keys — `rateLimit` and friends for Rotector,
 * `okappikiRateLimit` and friends for Okappiki — because their budgets are not
 * comparable numbers and one pair of fields for both would mean switching
 * backend silently applied the other one's rate to it.
 *
 * `stored` is a plain record rather than the settings object, so this module
 * stays free of the settings/store import cycle and remains callable before the
 * plugin is registered. Every read is defensive: a missing or mistyped value
 * falls back to the measured default rather than throwing.
 *
 * `retainBeyondTerms` and the TTL it implies go through `effectiveCacheTtl`
 * rather than being read per backend, because the question it answers — how
 * long may a finding be held — is one question about Rotector's data and not
 * two about two services. An Okappiki response has a Rotector verdict inside
 * it, so a flag that lifted one ceiling and not the other would be a promise
 * kept on one backend and broken on the other.
 */
export function backendOptionsFrom(name: string, stored: Record<string, any> | null | undefined): ClientOptions {
    const s = stored && typeof stored === "object" ? stored : {};
    const key = normaliseName(name);
    const retain = retainBeyondTermsFrom(s);

    if (key === "okappiki") {
        const d = BACKEND_DEFAULTS.okappiki;
        return {
            rateLimit: num(s.okappikiRateLimit, d.rateLimit!, 1, 10_000),
            window: num(s.okappikiWindow, d.window!, 0.5, 3600),
            reserve: num(s.okappikiReserve, d.reserve!, 0, 9_999),
            // Clamped here as well as in the client, so a settings screen
            // showing the effective value shows the one that will be used.
            concurrency: num(s.okappikiConcurrency, d.concurrency!, 1, OKAPPIKI_MAX_CONCURRENCY),
            // seconds; api/okappiki.ts clamps this under whichever ceiling the
            // flag below selects
            cacheTtl: effectiveCacheTtl(s, num(s.okappikiCacheTtl, d.cacheTtl!, 0, 24 * 3600)),
            retainBeyondTerms: retain,
        };
    }

    const d = BACKEND_DEFAULTS.rotector;
    const apiKey = typeof s.apiKey === "string" ? s.apiKey.trim() : "";
    return {
        apiKey: apiKey || undefined,
        rateLimit: num(s.rateLimit, d.rateLimit!, 1, 10_000),
        window: num(s.window, d.window!, 0.5, 3600),
        reserve: num(s.reserve, d.reserve!, 0, 9_999),
        concurrency: num(s.concurrency, d.concurrency!, 1, 16),
        cacheTtl: effectiveCacheTtl(s, num(s.cacheTtl, d.cacheTtl!, 0, 24 * 3600)),
        retainBeyondTerms: retain,
    };
}

/**
 * The backend named in a settings record, defaulting to Rotector.
 *
 * Unlike `buildBackend` this does not throw — it is what a UI reads to decide
 * what to show, and a settings screen that crashes on a bad value cannot be
 * used to fix the bad value. `buildBackend` is still the place a typo is
 * caught, because that is the last point before requests actually go out.
 */
export function backendFrom(stored: Record<string, any> | null | undefined): BackendName {
    const raw = stored && typeof stored === "object" ? stored.backend : undefined;
    const key = normaliseName(raw);
    return isBackendName(key) ? key : DEFAULT_BACKEND;
}

// --------------------------------------------------------------------------
// small utilities
// --------------------------------------------------------------------------

function normaliseName(name: any): string {
    return String(name ?? "").trim().toLowerCase();
}

/** A finite number inside `[min, max]`, or the fallback. Never NaN. */
function num(value: unknown, fallback: number, min: number, max: number): number {
    const parsed = typeof value === "number" ? value : Number(value);
    if (!isFinite(parsed)) return fallback;
    return Math.min(max, Math.max(min, parsed));
}
