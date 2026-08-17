/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The in-memory report cache every surface reads from, plus the lazy batched
 * lookups that fill it.
 *
 * Two constraints shape this module and neither is negotiable:
 *
 *  - Rotector's Terms of Use forbid retaining their responses beyond 24 hours,
 *    so nothing here is ever written to `@api/DataStore` or `settings.store`.
 *    A live finding's only home is this Map, and it is swept on a timer and
 *    clamped to the retention ceiling — 23 hours by default, whatever TTL the
 *    user configured. (features/history.ts is the one module that does write
 *    findings down, for finished scans, under that same ceiling and with the
 *    same sweep — a separate database of its own, never this one's.)
 *
 *    That ceiling is the one number here a setting can move, and only one
 *    setting moves it: `retainBeyondTerms`, off by default. It is read fresh on
 *    every check rather than captured at load, so flipping it takes effect
 *    without a reload — including flipping it *back*, which has to start
 *    dropping over-age findings immediately rather than at the next restart.
 *    Nothing about it changes where findings live or makes an old one read as
 *    current: everything past 24 hours is stale, and `isStale`/`staleNote` in
 *    api/types.ts are what every surface says so with.
 *  - A member list can render a hundred rows in a second. One lookup per row
 *    would be a hundred requests; instead `request()` drops ids into a set that
 *    is flushed once, after a short idle gap, as a single batch of up to 100.
 */

import { useEffect, useState } from "@webpack/common";

import { buildBackend, DEFAULT_BACKEND } from "./api/backend";
import type { LookupBackend } from "./api/backend";
import { MAX_BATCH, MAX_CACHE_TTL_MS } from "./api/rotector";
import { emptyReport, isStale, MemberReport, reportVerdict } from "./api/types";
import { Verdict } from "./api/verdict";
import { backendName, clientOptions, lazyLookupAllowed, retentionCeilingMs } from "./settings";

// The plugin is loaded without type checking, so a sibling module that is
// missing an export hands us `undefined` rather than failing to build. These
// two values are load-bearing for the retention promise and for batch sizing,
// so they get literal fallbacks rather than being trusted blindly.
const DEFAULT_CEILING_MS = typeof MAX_CACHE_TTL_MS === "number" && MAX_CACHE_TTL_MS > 0
    ? MAX_CACHE_TTL_MS
    : 23 * 3600 * 1000;
const BATCH = typeof MAX_BATCH === "number" && MAX_BATCH > 0 ? MAX_BATCH : 100;

/**
 * The age past which an entry may not be served, in milliseconds.
 *
 * Called on every read and every sweep rather than captured once: the ceiling
 * follows `retainBeyondTerms`, and a value captured at load would mean the
 * setting only took effect after a reload — in both directions, which is the
 * one that matters. Switching the flag back off has to start dropping over-age
 * findings straight away.
 *
 * A settings store that is not up yet, or a sibling that failed to evaluate,
 * falls back to the terms' own 23 hours. The permissive direction is the one
 * that breaks the promise, so the fallback is the strict number.
 */
function ceilingMs(): number {
    try {
        const ms = retentionCeilingMs();
        return Number.isFinite(ms) && ms > 0 ? ms : DEFAULT_CEILING_MS;
    } catch (err) {
        return DEFAULT_CEILING_MS;
    }
}

/** how long the queue waits for more ids before it sends what it has */
const FLUSH_IDLE_MS = 400;

/** how often expired entries are dropped */
const SWEEP_INTERVAL_MS = 60_000;

/**
 * A failed lookup is not a finding and must not be cached like one. Holding an
 * error for an hour would mean one CORS refusal silently stops the plugin
 * looking anything up until the TTL runs out, so errors expire in a minute and
 * the next `request()` tries again.
 */
const ERROR_TTL_MS = 60_000;

export interface StoreEntry {
    report?: MemberReport;
    error?: string;
    pending: boolean;
    at: number;
}

export interface StoreStats {
    cached: number;
    pending: number;
    findings: number;
    /**
     * How many of the cached reports are past the terms' 24-hour window.
     *
     * Always zero while `retainBeyondTerms` is off, because nothing survives
     * that long to be counted. It is here so that the one number that says what
     * lifting the ceiling actually did is available to the status panel rather
     * than being something a user has to open reports one at a time to notice.
     */
    stale: number;
}

/** Discord snowflakes only; anything else is a caller bug and is dropped. */
function isSnowflake(id: unknown): id is string {
    return typeof id === "string" && id.length > 0 && id.length < 32 && /^\d+$/.test(id);
}

/**
 * The active backend's cache lifetime in seconds, or the shared default.
 *
 * Every settings read in this module goes through a settings.ts helper rather
 * than through `settings.store` directly: the store getter throws until the
 * plugin is registered and this module is constructed at import time, so a
 * bare read is a thrown error inside a member-list row.
 */
function configuredCacheTtlSeconds(): number {
    try {
        const ttl = Number(clientOptions()?.cacheTtl);
        return Number.isFinite(ttl) ? ttl : 3600;
    } catch (err) {
        return 3600;
    }
}

function describeError(err: any): string {
    if (!err) return "The lookup failed for an unknown reason.";
    if (typeof err === "string") return err;
    const message = typeof err.message === "string" ? err.message : String(err);
    return message || "The lookup failed for an unknown reason.";
}

/**
 * A token that changes whenever an entry changes.
 *
 * `useReport` compares this rather than the entry object, because `put()`
 * replaces entries wholesale and a React state comparison on the object would
 * re-render every subscriber on every batch.
 */
function entryToken(entry: StoreEntry | undefined): string {
    if (!entry) return "";
    return `${entry.at}|${entry.pending ? 1 : 0}|${entry.report ? 1 : 0}|${entry.error ?? ""}`;
}

class ReportStore {
    private entries = new Map<string, StoreEntry>();
    private listeners = new Set<() => void>();

    private _client: LookupBackend | null = null;
    /**
     * The backend and options the held client was built from.
     *
     * Every setting that shapes a request carries an `onChange` that calls
     * `setClient(null)`, but a settings file edited by hand, or written through
     * `Vencord.Settings` by something else, fires no handler at all — and the
     * consequence of missing one is not a stale preference, it is a scan
     * running against the service the operator switched *away* from, or
     * Rotector's rate applied to Okappiki's window. So the signature is checked
     * on every use rather than trusted to a handler.
     */
    private _clientKey = "";

    /** ids waiting for the next flush */
    private queued = new Set<string>();
    /** ids currently inside a flush, so they are not queued twice */
    private inflight = new Set<string>();

    private flushTimer: any = null;
    private sweepTimer: any = null;
    /** set while a coalesced notification is waiting for the end of the tick */
    private notifyTimer: any = null;
    private controller: AbortController | null = null;
    private flushing = false;

    /**
     * Bumped by `stop()`. A flush that was already in flight when the plugin
     * was switched off checks this before writing what it found, so a disabled
     * plugin cannot repopulate the cache it just cleared. Using a counter
     * rather than a boolean means re-enabling the plugin needs no reset call:
     * the next request simply belongs to the next generation.
     */
    private generation = 0;

    // ------------------------------------------------------------------
    // client
    // ------------------------------------------------------------------

    setClient(client: LookupBackend | null): void {
        this._client = client;
        this._clientKey = client ? "external" : "";
        // Reports already held stay held: they are answers about members, not
        // about the connection, and re-fetching them would spend rate limit to
        // learn what we already know. That holds across a backend switch too —
        // an Okappiki response carries a Rotector verdict inside it, so the
        // answers do not stop being answers because the next question will be
        // asked somewhere else.
    }

    /**
     * The live backend client, built from settings the first time anything
     * needs it and rebuilt whenever those settings describe a different one.
     *
     * `buildBackend` throws on a name it does not recognise rather than falling
     * back to Rotector, and that is deliberate: a typo must not silently scan
     * against a service the operator did not choose. The throw reaches the
     * caller, which is a scan or a lazy flush, and both report it.
     */
    client(): LookupBackend {
        const wanted = this.describeClient();
        if (this._client && (this._clientKey === "external" || this._clientKey === wanted)) {
            return this._client;
        }

        let name = DEFAULT_BACKEND as string;
        let opts: any;
        try {
            name = backendName();
            opts = clientOptions();
        } catch (err) {
            // Before the plugin is registered `settings.store` throws; the
            // default backend and the client's own defaults are the right
            // answer in that window.
            opts = undefined;
        }
        this._client = buildBackend(name, opts);
        this._clientKey = wanted;
        return this._client;
    }

    /** A signature of the settings a client would be built from right now. */
    private describeClient(): string {
        try {
            return JSON.stringify([backendName(), clientOptions()]);
        } catch (err) {
            // Settings are not readable yet, so nothing can have changed under
            // us either; the empty signature only ever matches a fresh build.
            return "";
        }
    }

    private signal(): AbortSignal | undefined {
        if (!this.controller) {
            try {
                this.controller = new AbortController();
            } catch (err) {
                return undefined;
            }
        }
        return this.controller.signal;
    }

    // ------------------------------------------------------------------
    // reads
    // ------------------------------------------------------------------

    get(id: string): MemberReport | undefined {
        const entry = this.entries.get(id);
        if (!entry?.report) return undefined;
        const born = entry.report.fetchedAt || entry.at;
        if (Date.now() - born > ceilingMs()) {
            // Past the retention ceiling. Dropped here as well as in the
            // sweeper, so a stopped timer can never make us serve expired data,
            // and re-checked on every read so lowering the ceiling bites now.
            // Note this is the *retention* line and not the staleness one: a
            // finding inside the ceiling but older than 24 hours is served, and
            // served labelled — see `isStale` in api/types.ts.
            this.entries.delete(id);
            return undefined;
        }
        return entry.report;
    }

    verdict(id: string): Verdict | undefined {
        const report = this.get(id);
        return report ? reportVerdict(report) : undefined;
    }

    isPending(id: string): boolean {
        return this.entries.get(id)?.pending === true;
    }

    error(id: string): string | undefined {
        return this.entries.get(id)?.error;
    }

    entry(id: string): StoreEntry | undefined {
        return this.entries.get(id);
    }

    // ------------------------------------------------------------------
    // writes
    // ------------------------------------------------------------------

    /**
     * The current generation, for a caller that holds results across an await.
     *
     * A scan modal runs for minutes and writes what it finds through `put`. If
     * the user purges the cache or disables the plugin halfway through, those
     * writes must stop — otherwise the scan quietly refills the cache someone
     * just cleared, and `put`'s own `ensureSweep` restarts a timer `stop`
     * cleared, leaving Rotector data and a live interval behind a disabled
     * plugin. Long-running callers capture this before they start and hand it
     * back on every write.
     */
    get epoch(): number {
        return this.generation;
    }

    put(reports: Iterable<MemberReport>, epoch?: number): void {
        // A stale writer is dropped in full rather than partially applied.
        if (epoch !== undefined && epoch !== this.generation) return;

        let changed = false;
        for (const report of reports || []) {
            if (!report || !isSnowflake(report.discordId)) continue;
            this.write(report);
            changed = true;
        }
        if (changed) {
            this.ensureSweep();
            this.notify();
        }
    }

    private write(report: MemberReport): void {
        const id = report.discordId;
        this.entries.set(id, {
            report,
            error: report.error ?? undefined,
            pending: false,
            at: Date.now(),
        });
        this.queued.delete(id);
        this.inflight.delete(id);
    }

    // ------------------------------------------------------------------
    // lazy lookups
    // ------------------------------------------------------------------

    /**
     * Whether a lookup may be issued because somebody scrolled past a member.
     *
     * Two gates, and the second one is not a preference. `autoLookup` is the
     * user's answer, honoured under Rotector where this queue turns a hundred
     * rendered rows into a single hundred-id request. Under Okappiki there is
     * no batch endpoint: the same hundred rows would be a hundred requests,
     * four at a time, each one costing Okappiki a live Rotector lookup out of
     * *their* budget — so a member list scrolled past would quietly become a
     * scan of it, spending someone else's rate limit on a question nobody
     * asked. settings.ts decides; this is the one place that acts on it, so
     * every lazy path is covered by the same decision. Explicit scans and
     * "Re-check" still work: `refresh()` deliberately does not consult this.
     */
    private lazyAllowed(): boolean {
        try {
            return lazyLookupAllowed();
        } catch (err) {
            // Settings unreadable means the plugin is barely up; the safe
            // answer to "may I spend rate limit unprompted" is no.
            return false;
        }
    }

    /**
     * Queue a lazy lookup for one member.
     *
     * A no-op when lazy lookups are not allowed: looking members up because
     * they scrolled past is a choice the user makes, not a default we impose,
     * and every lookup spends the shared rate limit.
     */
    request(id: string): void {
        if (!isSnowflake(id)) return;
        if (!this.lazyAllowed()) return;
        if (!this.wants(id)) return;

        this.queued.add(id);
        // Quietly, then one notification for the whole tick. This is called
        // once per rendered row, so notifying per id would mean a member list
        // of a hundred rows running a hundred notifications across every
        // subscriber — the same work `requestMany` already coalesces, arriving
        // one id at a time instead.
        this.markPending(id, true);
        this.notifySoon();
        this.schedule();
    }

    requestMany(ids: string[]): void {
        if (!ids?.length) return;
        if (!this.lazyAllowed()) return;

        let added = false;
        for (const id of ids) {
            if (!isSnowflake(id)) continue;
            if (!this.wants(id)) continue;
            this.queued.add(id);
            this.markPending(id, true);
            added = true;
        }
        if (added) {
            this.notify();
            this.schedule();
        }
    }

    /** Whether a fresh lookup for this id would tell us anything new. */
    private wants(id: string): boolean {
        if (this.queued.has(id) || this.inflight.has(id)) return false;
        const entry = this.entries.get(id);
        if (!entry) return true;
        if (entry.pending) return false;
        if (entry.error) return Date.now() - entry.at > ERROR_TTL_MS;
        return this.get(id) === undefined;
    }

    private markPending(id: string, quiet = false): void {
        const existing = this.entries.get(id);
        this.entries.set(id, {
            report: existing?.report,
            error: undefined,
            pending: true,
            at: Date.now(),
        });
        if (!quiet) this.notify();
    }

    private schedule(): void {
        // A full batch is worth sending now; anything less waits for the idle
        // gap, which is what turns a scrolled member list into one request.
        if (this.queued.size >= BATCH) {
            this.clearFlushTimer();
            void this.flush();
            return;
        }
        if (this.flushTimer !== null) return;
        this.flushTimer = setTimeout(() => {
            this.flushTimer = null;
            void this.flush();
        }, FLUSH_IDLE_MS);
    }

    private clearFlushTimer(): void {
        if (this.flushTimer !== null) {
            clearTimeout(this.flushTimer);
            this.flushTimer = null;
        }
    }

    private async flush(): Promise<void> {
        // One batch in flight at a time. The client limits itself, but issuing
        // several overlapping scans would make the queue's 100-id cap
        // meaningless.
        if (this.flushing) return;
        if (!this.queued.size) return;

        const batch: string[] = [];
        for (const id of this.queued) {
            batch.push(id);
            if (batch.length >= BATCH) break;
        }
        for (const id of batch) {
            this.queued.delete(id);
            this.inflight.add(id);
        }

        const gen = this.generation;
        this.flushing = true;
        try {
            const client = this.client();
            const reports = await client.scanMembers(batch, {
                signal: this.signal(),
                onPartial: partial => {
                    if (gen === this.generation) this.put(partial);
                },
            });
            if (gen !== this.generation) return;
            // `scanMembers` promises a report per input id, but this module is
            // the one that has to be right if it ever does not.
            const found: MemberReport[] = [];
            for (const id of batch) {
                const report = reports?.get?.(id);
                found.push(report ?? emptyReport(id, "The lookup returned nothing for this member."));
            }
            this.put(found);
        } catch (err) {
            if (gen !== this.generation) return;
            const message = describeError(err);
            this.put(batch.map(id => emptyReport(id, message)));
        } finally {
            this.flushing = false;
            for (const id of batch) this.inflight.delete(id);
        }

        if (this.queued.size && gen === this.generation) this.schedule();
    }

    /** Force a fresh lookup for one member, ignoring every cache in the way. */
    async refresh(id: string): Promise<MemberReport> {
        if (!isSnowflake(id)) {
            return emptyReport(String(id), "That is not a Discord user id.");
        }

        this.queued.delete(id);
        this.markPending(id);

        let client: LookupBackend;
        try {
            client = this.client();
        } catch (err) {
            const report = emptyReport(id, describeError(err));
            this.put([report]);
            return report;
        }

        // The client caches both lookup hops and exposes no per-id eviction, so
        // the only way to make a re-check actually re-check is to drop its
        // caches wholesale. That costs nothing held here — this store keeps the
        // reports — it only means the next lookup of some other member pays for
        // a request it might have skipped.
        try {
            client.purgeCache();
        } catch (err) {
            /* a client without a cache to purge is fine */
        }

        // Same generation guard the batch flush uses: a purge or a plugin stop
        // that lands while this single lookup is in flight must not have its
        // result written back afterwards. The report is still returned to the
        // caller that asked for it — the modal that is open should show what it
        // fetched — it just does not repopulate a cache someone cleared.
        const gen = this.generation;
        this.inflight.add(id);
        try {
            const reports = await client.scanMembers([id], { signal: this.signal() });
            const report = reports?.get?.(id)
                ?? emptyReport(id, "The lookup returned nothing for this member.");
            if (gen === this.generation) this.put([report]);
            return report;
        } catch (err) {
            const report = emptyReport(id, describeError(err));
            if (gen === this.generation) this.put([report]);
            return report;
        } finally {
            this.inflight.delete(id);
        }
    }

    // ------------------------------------------------------------------
    // lifecycle
    // ------------------------------------------------------------------

    purge(): void {
        // Bumping the generation and dropping the in-flight batch is the whole
        // point: without it a flush that was already running writes what it
        // found back into the cache the user just cleared, so "Purge cached
        // findings" would silently undo itself a second or two later. Leaving
        // `inflight` populated would also make `wants()` refuse fresh lookups
        // for those ids until the stale batch finished.
        this.generation++;
        this.clearFlushTimer();
        try {
            this.controller?.abort();
        } catch (err) {
            /* already aborted */
        }
        this.controller = null;
        this.entries.clear();
        this.queued.clear();
        this.inflight.clear();
        try {
            this._client?.purgeCache();
        } catch (err) {
            /* nothing to purge */
        }
        this.notify();
    }

    stats(): StoreStats {
        let cached = 0;
        let pending = 0;
        let findings = 0;
        let stale = 0;
        const now = Date.now();
        for (const [id, entry] of Array.from(this.entries)) {
            if (entry.pending) pending++;
            if (!entry.report) continue;
            // Counted through `get`, so the retention ceiling is enforced here
            // as well: the number the status panel prints is what the plugin
            // would actually serve, and an entry the sweeper has not reached
            // yet is neither served nor counted. `get` drops it on the way past.
            if (!this.get(id)) continue;
            cached++;
            // "Findings" is the same threshold the marks use: NO DETECTIONS and
            // UNKNOWN are answers, but they are not findings, and counting them
            // here would make the number meaningless.
            if (reportVerdict(entry.report) >= Verdict.INFO) findings++;
            // Counted, not hidden. A finding older than the terms' window is
            // still served — it is the ceiling that decides what is dropped —
            // but it is a weaker claim than a fresh one and the count is how
            // the status panel says so at a glance.
            if (isStale(entry.report, now)) stale++;
        }
        return { cached, pending, findings, stale };
    }

    subscribe(fn: () => void): () => void {
        if (typeof fn !== "function") return () => { };
        this.listeners.add(fn);
        return () => { this.listeners.delete(fn); };
    }

    /**
     * One notification for everything that happened this tick.
     *
     * A zero-delay timer, not a microtask: the point is to land after the
     * render pass that produced the calls, so a hundred rows asking to be
     * looked up cost one pass over the subscribers rather than a hundred.
     */
    private notifySoon(): void {
        if (this.notifyTimer !== null) return;
        this.notifyTimer = setTimeout(() => {
            this.notifyTimer = null;
            this.notify();
        }, 0);
    }

    private notify(): void {
        // A direct notification subsumes any coalesced one still waiting.
        if (this.notifyTimer !== null) {
            clearTimeout(this.notifyTimer);
            this.notifyTimer = null;
        }
        for (const fn of Array.from(this.listeners)) {
            try {
                fn();
            } catch (err) {
                // A broken subscriber must not stop the others being told.
            }
        }
    }

    private ensureSweep(): void {
        if (this.sweepTimer !== null) return;
        this.sweepTimer = setInterval(() => this.sweep(), SWEEP_INTERVAL_MS);
    }

    private sweep(): void {
        const now = Date.now();
        // The configured TTL is advisory; the ceiling is not. Read through
        // `clientOptions()` rather than off a named key, because which key
        // holds the TTL depends on the backend and a hard-coded `cacheTtl`
        // would silently apply Rotector's number to an Okappiki cache — and
        // because that is also where `retainBeyondTerms` turns the TTL into the
        // configured retention window rather than the hour a cache wanted.
        const ceiling = ceilingMs();
        const configured = Number(configuredCacheTtlSeconds()) * 1000;
        const ttl = Math.max(0, Math.min(Number.isFinite(configured) ? configured : ceiling, ceiling));

        let changed = false;
        for (const [id, entry] of Array.from(this.entries)) {
            if (entry.pending) continue;
            const born = entry.report?.fetchedAt || entry.at;
            const limit = entry.error ? ERROR_TTL_MS : ttl;
            if (now - born > limit) {
                this.entries.delete(id);
                changed = true;
            }
        }

        if (!this.entries.size && this.sweepTimer !== null) {
            clearInterval(this.sweepTimer);
            this.sweepTimer = null;
        }
        if (changed) this.notify();
    }

    /**
     * Abort in-flight work and clear the timers.
     *
     * The held reports go too. They are Rotector's data, kept only to serve a
     * surface that is about to stop existing; keeping them past the plugin
     * being switched off would be retention with no purpose behind it. That
     * includes the *client's* copies, which is why this purges before it drops
     * the reference — see below.
     */
    stop(): void {
        this.generation++;
        this.clearFlushTimer();
        if (this.sweepTimer !== null) {
            clearInterval(this.sweepTimer);
            this.sweepTimer = null;
        }
        try {
            this.controller?.abort();
        } catch (err) {
            /* already aborted */
        }
        this.controller = null;
        this.queued.clear();
        this.inflight.clear();
        this.entries.clear();
        // Purged before the reference goes, exactly as `purge()` does it, and
        // for a reason that is not tidiness. The backend client holds the raw
        // upstream payloads it was answered with — an Okappiki body embeds a
        // Rotector verdict, so both backends' caches sit under the same
        // retention ceiling — and it keeps its own interval running to
        // expire them. A live `setInterval` is a garbage-collection root, so a
        // client that is only dereferenced is never collected: dropping it
        // without purging leaves Rotector's data *and* a running timer behind a
        // plugin the user switched off, which is the one thing this module's
        // retention promise says cannot happen.
        try {
            this._client?.purgeCache();
        } catch (err) {
            /* a client without a cache to purge is fine */
        }
        this._client = null;
        this._clientKey = "";
        this.notify();
    }
}

export const reportStore = new ReportStore();

// --------------------------------------------------------------------------
// React plumbing
// --------------------------------------------------------------------------

/**
 * Subscribe to one member's entry.
 *
 * Deliberately built from `useState` + `useEffect` rather than
 * `useSyncExternalStore`: React here is whichever version Discord shipped, and
 * a hook that is missing is not a degraded render, it is a thrown error inside
 * a member-list row.
 */
export function useReport(id: string | undefined): { report?: MemberReport; pending: boolean; error?: string; } {
    const [, setToken] = useState(() => entryToken(id ? reportStore.entry(id) : undefined));

    useEffect(() => {
        if (!id) return;
        let current = entryToken(reportStore.entry(id));
        // The store may have answered between render and effect.
        setToken(current);
        const unsubscribe = reportStore.subscribe(() => {
            const next = entryToken(reportStore.entry(id));
            if (next === current) return;
            current = next;
            setToken(next);
        });
        return unsubscribe;
    }, [id]);

    if (!id) return { pending: false };

    // `get` first, and re-read the entry after: `get` re-checks the retention
    // ceiling and *deletes* the entry when it has expired, so an entry captured
    // beforehand is a detached object, and a member whose report just aged out
    // would report the dropped entry's error alongside no report at all.
    const report = reportStore.get(id);
    const entry = reportStore.entry(id);
    return {
        report,
        pending: entry?.pending === true,
        error: entry?.error,
    };
}

export function useStoreStats(): StoreStats {
    const [stats, setStats] = useState<StoreStats>(() => reportStore.stats());

    useEffect(() => {
        setStats(reportStore.stats());
        return reportStore.subscribe(() => setStats(reportStore.stats()));
    }, []);

    return stats;
}
