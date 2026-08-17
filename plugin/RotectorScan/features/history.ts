/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The scan history: what was scanned, when, and what was found — kept across a
 * restart of the client instead of only for as long as a modal stays open.
 *
 * Everything else in this plugin holds findings in memory only, which is
 * *stricter* than Rotector's Terms of Use require. The terms forbid retaining
 * their responses beyond 24 hours; they do not forbid writing them down inside
 * that window. By default this module takes the ground the terms actually leave
 * and not an inch more, so the rule is enforced three times over rather than
 * trusted once:
 *
 *  - on write — a record is stamped with `startedAt` and nothing that has
 *    already passed the ceiling is ever written;
 *  - on read — `list()`, `get()` and the hooks re-check every record's age and
 *    drop what is past it, exactly as store.ts's `get` does for its in-memory
 *    entries, so a sweep that has not run yet can never make us serve stale
 *    data;
 *  - on a timer — the sweep deletes expired records from IndexedDB as well as
 *    from the mirror, so the *file on disk* stops holding them too. A record
 *    that is merely unreachable is still retained, and retention is what the
 *    terms are about.
 *
 * The ceiling is whatever store.ts's is — the same number, reached through the
 * same function, so there is exactly one place in the plugin it can be wrong.
 * It is 23 hours by default and one setting moves it: `retainBeyondTerms`, off
 * by default, with `retentionHours` saying how far. That setting is a decision
 * the operator makes deliberately, and it changes only how long a record lives.
 * It does not change what is written down, it does not make an old finding read
 * as a current one, and it does not silence anything: every report past the
 * 24-hour window is labelled stale with its age wherever it is listed — see
 * `isStale` and `staleNote` in api/types.ts. `historyEnabled` and `historyLimit`
 * still cannot raise the ceiling; they only make the history smaller.
 *
 * The ceiling is read fresh at each of the four enforcement points rather than
 * captured at load, so lowering it — switching the flag back off — starts
 * deleting over-age records at the next read or sweep instead of at the next
 * restart.
 *
 * The database is this plugin's own (`RotectorScanHistory`), not Vencord's
 * shared `VencordData` store. That is deliberate: "purge everything" has to be
 * able to delete a whole database without touching a single key belonging to
 * anything else the client keeps.
 *
 * What is *not* stored is as much of the design as what is. A ten-thousand
 * member scan is almost entirely NO DETECTIONS, and writing all of it down would
 * put megabytes into IndexedDB to record that nothing was found — so the clean
 * rows are counted and dropped, and the record says how many it dropped. A
 * history that quietly lost rows would be worse than no history at all.
 */

import * as DataStore from "@api/DataStore";
import { Logger } from "@utils/Logger";
import { useEffect, useState } from "@webpack/common";

import { MAX_CACHE_TTL_MS } from "../api/rotector";
import { isStale, MemberReport, reportVerdict } from "../api/types";
import { Verdict } from "../api/verdict";
import { retainBeyondTerms, retentionCeilingMs, retentionHours, settings } from "../settings";

const logger = new Logger("RotectorScan");

/**
 * Our own database and object store.
 *
 * `createStore` is idb-keyval's, vendored into Equicord's DataStore API. Naming
 * both halves ourselves is what keeps `purge()` honest: it clears this store and
 * only this store.
 */
const DB_NAME = "RotectorScanHistory";
const STORE_NAME = "RotectorScanHistoryStore";

/** every record's key is this plus its id, so a stray key cannot look like one */
const KEY_PREFIX = "scan:";

/**
 * The default retention ceiling, in milliseconds.
 *
 * Imported from the client, with a literal fallback for the same reason store.ts
 * keeps one: the plugin is loaded without type checking, so a sibling module
 * that failed to evaluate hands us `undefined` rather than failing to build, and
 * `undefined` here would mean *no* ceiling.
 */
const DEFAULT_CEILING_MS = typeof MAX_CACHE_TTL_MS === "number" && MAX_CACHE_TTL_MS > 0
    ? MAX_CACHE_TTL_MS
    : 23 * 3600 * 1000;

/**
 * The live retention ceiling: 23 hours, or whatever `retentionHours` says once
 * `retainBeyondTerms` has been switched on.
 *
 * A function rather than a constant so the setting takes effect without a
 * reload, and so the four enforcement points below — write, read, sweep,
 * hydrate — cannot drift apart. Anything unreadable falls back to the terms'
 * own 23 hours, because the permissive direction is the one that breaks the
 * promise.
 */
function ceilingMs(): number {
    try {
        const ms = retentionCeilingMs();
        return Number.isFinite(ms) && ms > 0 ? ms : DEFAULT_CEILING_MS;
    } catch {
        return DEFAULT_CEILING_MS;
    }
}

/** how often the sweeper deletes what has aged out, from the mirror and from disk */
const SWEEP_INTERVAL_MS = 5 * 60_000;

/**
 * How long we wait for IndexedDB to open before deciding it is not going to.
 *
 * `indexedDB.open` does not fail when another tab holds a version change open,
 * when the browser is in a private session that refuses persistence, or when the
 * origin is out of quota — it simply never fires. A promise that never settles
 * would leave `ready()` pending forever and every caller awaiting it, so the
 * open is raced against a clock and a timeout means "history off this session".
 */
const OPEN_TIMEOUT_MS = 8000;

/** the shipped cap on retained scans; `historyLimit` may lower or raise it within reason */
export const HISTORY_LIMIT_DEFAULT = 25;

/** nobody needs a thousand scans inside one retention window, and quota is finite */
const HISTORY_LIMIT_MAX = 200;

/**
 * How many member reports one record may hold.
 *
 * Only findings are stored, so this is reached by a scan of a genuinely bad
 * server rather than by a normal one. The worst findings are kept and the rest
 * are counted into `truncated`, because a record that silently held the first
 * thousand rows in arrival order would be a record that dropped the THREAT to
 * make room for an INFO.
 */
const MAX_STORED_REPORTS = 1000;

/** the bar for "a finding": the same one the marks, the ledger and store.stats() use */
const FINDING_FLOOR: Verdict = Verdict.INFO;

/**
 * How much of each account's evidence a record keeps.
 *
 * Evidence is free text and it is by far the largest thing in a report — a
 * handful of accounts can carry a hundred lines between them. A stored record
 * exists to say *who* was flagged, for *what*, and by *which* service; the full
 * evidence is one click away, because opening a row opens the live report and
 * that re-asks the backend. So the reason names, their messages and the first
 * few evidence lines are kept and the rest is left behind, which is the
 * difference between a history that costs kilobytes and one that costs
 * megabytes.
 */
const MAX_STORED_REASONS = 8;
const MAX_STORED_EVIDENCE = 3;

/**
 * The sentence every surface that shows a record has to carry, in the default
 * state: the ceiling has not been lifted and records live 23 hours.
 *
 * Exported so the modal and anything else that lists history print the same
 * words, rather than each writing their own softer version of them. Prefer
 * `historyRetentionNotice()` below, which says this when it is true and says
 * what is actually happening when it is not; this constant remains the fallback
 * and the wording the function reuses.
 */
export const HISTORY_RETENTION_NOTICE =
    "Past scans are stored on this computer and deleted 23 hours after the scan started — " +
    "Rotector's terms forbid keeping their responses beyond 24 hours, and flag statuses change.";

/**
 * The same sentence, for whichever ceiling is actually in force.
 *
 * A surface that printed the constant above while `retainBeyondTerms` was on
 * would be telling the reader records are deleted at 23 hours when they are
 * not, so every surface that lists history calls this instead. The lifted case
 * names the choice as a choice, gives the real span, and says that anything
 * past the terms' window is labelled stale — because a record that outlived the
 * window is exactly the one whose flag status may have moved.
 */
export function historyRetentionNotice(): string {
    if (!attempt(() => retainBeyondTerms(), false)) return HISTORY_RETENTION_NOTICE;

    const hours = Math.max(1, Math.round(attempt(() => retentionHours(), 168)));
    const span = hours >= 48 ? `${Math.round(hours / 24)} days` : `${hours} hours`;
    return (
        `Past scans are stored on this computer and kept for ${span} after the scan started — ` +
        "past the 24-hour window Rotector's Terms of Use allow, because you switched that on. " +
        "Every finding older than 24 hours is marked stale with its age: flag types change and " +
        "appeals succeed, so re-check before acting on one."
    );
}

// --------------------------------------------------------------------------
// the record
// --------------------------------------------------------------------------

export interface ScanRecord {
    id: string;
    /** when the scan began; the retention ceiling is measured from here */
    startedAt: number;
    finishedAt?: number;
    /** which service answered: "rotector" | "okappiki" */
    backend: string;
    sourceKind: string;
    sourceLabel: string;
    guildId?: string;
    /** members the scan got an answer for, findings or not */
    scanned: number;
    /** of those, how many were at or above INFO */
    findings: number;
    /** of those, how many came back with an error rather than an answer */
    unanswered: number;
    /** the collect note: what this list of members was and was not */
    coverageNote?: string;
    /**
     * Answered members whose report carried no finding and was therefore not
     * written down. They are counted here and nowhere else, and the UI prints
     * the number — a record has to say what it left out.
     */
    omitted: number;
    /** findings dropped because the record hit `MAX_STORED_REPORTS` */
    truncated: number;
    /** when this record was last written */
    savedAt: number;
    /** the findings themselves; never the clean rows */
    reports: MemberReport[];
}

/**
 * What a caller actually has to supply.
 *
 * `save` mints the id if there is none, drops and counts the clean rows into
 * `omitted`, truncates into `truncated` and stamps `savedAt` — so a scan hands
 * over what it knows and does not have to know this module's bookkeeping. A
 * record read back out of `list()` or `get()` always has all of it.
 */
export type NewScanRecord =
    Omit<ScanRecord, "id" | "omitted" | "truncated" | "savedAt">
    & { id?: string; omitted?: number; truncated?: number; savedAt?: number; };

export interface HistoryStats {
    scans: number;
    /** members answered across every retained scan */
    members: number;
    findings: number;
    /** `startedAt` of the oldest retained scan, for "expires in …" */
    oldest?: number;
    /**
     * How many stored findings are past the terms' 24-hour window.
     *
     * Zero unless `retainBeyondTerms` is on, because nothing lives that long
     * otherwise. It exists so a surface can say how much of what is on disk is
     * a claim about yesterday rather than about now, without opening records
     * one at a time to find out.
     */
    stale: number;
}

export interface HistoryStatus {
    /** whether writes are reaching disk */
    on: boolean;
    /** why not, in words a person can act on; empty when `on` */
    reason: string;
    /** whether the first read of IndexedDB has completed */
    hydrated: boolean;
}

// --------------------------------------------------------------------------
// small helpers
// --------------------------------------------------------------------------

function attempt<T>(fn: () => T, fallback: T): T {
    try {
        const value = fn();
        return value === undefined ? fallback : value;
    } catch {
        return fallback;
    }
}

function describeError(err: any): string {
    if (!err) return "an unknown error";
    if (typeof err === "string") return err;
    const name = typeof err.name === "string" ? err.name : "";
    const message = typeof err.message === "string" ? err.message : String(err);
    if (name && message && !message.includes(name)) return `${name}: ${message}`;
    return message || name || "an unknown error";
}

/** A short, unique-enough id. Randomness only has to survive one retention window. */
export function newScanId(): string {
    const time = Date.now().toString(36);
    const noise = Math.floor(Math.random() * 0xffffff).toString(36);
    return `${time}-${noise}`;
}

/**
 * Read one plugin setting.
 *
 * `settings.store` throws until the plugin has been registered, and this module
 * is constructed at import time, so every read is guarded and falls back to the
 * documented default. A settings read is never allowed to be the thing that
 * breaks a scan's bookkeeping.
 */
function option<T>(key: string, fallback: T): T {
    return attempt(() => {
        const store = (settings as any)?.store as Record<string, unknown> | undefined;
        if (store == null) return fallback;
        const value = store[key];
        return (value === undefined || value === null ? fallback : value) as T;
    }, fallback);
}

/** How many scans may be retained. A cap, never a licence to keep them longer. */
export function historyLimit(): number {
    const raw = Number(option("historyLimit", HISTORY_LIMIT_DEFAULT));
    if (!Number.isFinite(raw)) return HISTORY_LIMIT_DEFAULT;
    return Math.max(1, Math.min(HISTORY_LIMIT_MAX, Math.floor(raw)));
}

/** Whether new scans are written down at all. */
export function historyEnabled(): boolean {
    return option("historyEnabled", true) !== false;
}

/** When a record stops being readable. Never later than the ceiling allows. */
export function recordExpiresAt(record: ScanRecord | undefined): number {
    if (!record) return 0;
    return Number(record.startedAt) + ceilingMs();
}

function isExpired(record: ScanRecord | undefined, now: number): boolean {
    if (!record) return true;
    const started = Number(record.startedAt);
    // A record with no readable birthday cannot be proved to be inside the
    // window, so it is treated as outside it. Refusing to serve what we cannot
    // date is the only answer a retention rule allows.
    if (!Number.isFinite(started) || started <= 0) return true;
    return now - started > ceilingMs();
}

function verdictOf(report: MemberReport): Verdict {
    return attempt(() => reportVerdict(report), Verdict.UNKNOWN);
}

/** Whether this report is worth writing down: a finding, or a lookup that failed. */
function isWorthKeeping(report: MemberReport): boolean {
    if (!report || typeof report.discordId !== "string" || !report.discordId) return false;
    if (report.error) return true;
    return verdictOf(report) >= FINDING_FLOOR;
}

function text(value: any): string {
    return typeof value === "string" ? value : value == null ? "" : String(value);
}

function maybeNumber(value: any): number | null {
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
}

/**
 * One Roblox account, reduced to plain values.
 *
 * Rebuilt field by field rather than spread, for two reasons that are both about
 * not trusting the shape we were handed. The first is size — see
 * `MAX_STORED_EVIDENCE`. The second is that IndexedDB stores by structured
 * clone, which throws on anything it cannot copy: one unexpected function or
 * host object anywhere in a payload would turn a whole scan's history into a
 * DataCloneError. A record built only out of strings, numbers and plain objects
 * cannot fail that way.
 */
function slimAccount(account: any): any {
    const reasons: Record<string, any> = {};
    let kept = 0;
    for (const [name, detail] of Object.entries((account?.reasons ?? {}) as Record<string, any>)) {
        if (kept >= MAX_STORED_REASONS) break;
        kept++;
        const evidence = Array.isArray(detail?.evidence)
            ? detail.evidence.slice(0, MAX_STORED_EVIDENCE).map(text)
            : [];
        reasons[String(name)] = {
            message: text(detail?.message),
            confidence: maybeNumber(detail?.confidence),
            evidence,
        };
    }
    return {
        userId: maybeNumber(account?.userId) ?? 0,
        username: text(account?.username),
        detectedAt: maybeNumber(account?.detectedAt),
        sources: Array.isArray(account?.sources)
            ? account.sources.map((s: any) => maybeNumber(s) ?? 0)
            : [],
        flagType: maybeNumber(account?.flagType),
        category: maybeNumber(account?.category),
        confidence: maybeNumber(account?.confidence),
        reasons,
        lastUpdated: maybeNumber(account?.lastUpdated),
    };
}

function slimServer(server: any): any {
    return {
        serverId: text(server?.serverId),
        serverName: text(server?.serverName),
        joinedAt: maybeNumber(server?.joinedAt),
        firstSeenAt: maybeNumber(server?.firstSeenAt),
        isTase: Boolean(server?.isTase),
        inGracePeriod: Boolean(server?.inGracePeriod),
    };
}

function slimSignal(signal: any): any {
    return {
        source: text(signal?.source),
        flagged: Boolean(signal?.flagged),
        verdict: maybeNumber(signal?.verdict) ?? Verdict.UNKNOWN,
        reason: text(signal?.reason),
        score: maybeNumber(signal?.score),
        robloxId: maybeNumber(signal?.robloxId),
        robloxUsername: text(signal?.robloxUsername),
        flagType: maybeNumber(signal?.flagType),
    };
}

/**
 * One report, reduced to the fields this module promises to hold.
 *
 * Copied rather than referenced: the caller's array belongs to a scan that may
 * still be running, so a stored reference would be a snapshot of an object
 * somebody else is still writing to. `fetchedAt` is defaulted to the scan's own
 * start so that a report with no timestamp still has a birthday the ceiling can
 * be measured against.
 */
function cloneReport(report: MemberReport, startedAt: number): MemberReport {
    const fetchedAt = Number((report as any)?.fetchedAt);
    return {
        discordId: String(report.discordId),
        accounts: Array.isArray(report.accounts) ? report.accounts.map(slimAccount) : [],
        servers: Array.isArray(report.servers) ? report.servers.map(slimServer) : [],
        error: report.error == null ? null : text(report.error),
        signals: Array.isArray(report.signals) ? report.signals.map(slimSignal) : [],
        fetchedAt: Number.isFinite(fetchedAt) && fetchedAt > 0 ? fetchedAt : startedAt,
    };
}

/**
 * Reports that are still inside the ceiling.
 *
 * The record's own age is checked first and is normally the binding one, but a
 * report carries its own `fetchedAt` and a record could in principle hold one
 * older than itself. Both are checked, because "nothing may be readable past the
 * ceiling" is a statement about findings and not only about records.
 */
function liveReports(record: ScanRecord, now: number): MemberReport[] {
    const reports = Array.isArray(record.reports) ? record.reports : [];
    const ceiling = ceilingMs();
    return reports.filter(report => {
        const born = Number(report?.fetchedAt) || Number(record.startedAt);
        return Number.isFinite(born) && now - born <= ceiling;
    });
}

/**
 * A record with its expired reports removed.
 *
 * Returns the same object when nothing had to go, so the common read path does
 * not allocate a copy of every record on every render.
 */
function pruneReports(record: ScanRecord, now: number): ScanRecord {
    const live = liveReports(record, now);
    const held = Array.isArray(record.reports) ? record.reports : [];
    if (live.length === held.length) return record;
    return { ...record, reports: live };
}

// --------------------------------------------------------------------------
// sanitising, in both directions
// --------------------------------------------------------------------------

function numberOr(value: any, fallback: number): number {
    const n = Number(value);
    return Number.isFinite(n) && n >= 0 ? Math.floor(n) : fallback;
}

/**
 * Turn whatever the caller handed us into a record we are prepared to store.
 *
 * This is where the clean rows are dropped and counted. It is also where the
 * counts are recomputed when the caller did not supply them, so a record is
 * never internally inconsistent about how many members it is describing.
 */
function sanitizeForSave(input: any): ScanRecord | null {
    if (!input || typeof input !== "object") return null;

    const now = Date.now();
    const startedRaw = Number(input.startedAt);
    // A fresh save with no readable start is stamped now: the scan being written
    // down has just happened, so "now" shortens its life rather than extending
    // it. A record read back off *disk* gets no such benefit — see fromStored.
    const startedAt = Number.isFinite(startedRaw) && startedRaw > 0
        ? Math.min(startedRaw, now)
        : now;

    const incoming: MemberReport[] = Array.isArray(input.reports) ? input.reports : [];

    let omitted = 0;
    const keep: MemberReport[] = [];
    for (const report of incoming) {
        if (!report || typeof report.discordId !== "string" || !report.discordId) continue;
        if (!isWorthKeeping(report)) {
            omitted++;
            continue;
        }
        keep.push(cloneReport(report, startedAt));
    }

    // Worst first, so a truncation drops the least severe rather than whatever
    // happened to arrive last.
    keep.sort((a, b) => verdictOf(b) - verdictOf(a));

    let truncated = numberOr(input.truncated, 0);
    if (keep.length > MAX_STORED_REPORTS) {
        truncated += keep.length - MAX_STORED_REPORTS;
        keep.length = MAX_STORED_REPORTS;
    }

    const findings = numberOr(
        input.findings,
        keep.filter(report => verdictOf(report) >= FINDING_FLOOR).length
    );
    const unanswered = numberOr(input.unanswered, keep.filter(report => Boolean(report.error)).length);
    const scanned = numberOr(input.scanned, keep.length + omitted);

    const finishedRaw = Number(input.finishedAt);

    return {
        id: typeof input.id === "string" && input.id ? input.id : newScanId(),
        startedAt,
        finishedAt: Number.isFinite(finishedRaw) && finishedRaw > 0 ? finishedRaw : undefined,
        backend: typeof input.backend === "string" && input.backend ? input.backend : "rotector",
        sourceKind: typeof input.sourceKind === "string" ? input.sourceKind : "",
        sourceLabel: typeof input.sourceLabel === "string" && input.sourceLabel
            ? input.sourceLabel
            : "members",
        guildId: typeof input.guildId === "string" && input.guildId ? input.guildId : undefined,
        scanned,
        findings,
        unanswered,
        coverageNote: typeof input.coverageNote === "string" && input.coverageNote
            ? input.coverageNote
            : undefined,
        // The caller may have trimmed the clean rows itself before handing them
        // over, in which case its own count is the true one; ours would be zero
        // because there was nothing left to drop.
        omitted: Math.max(numberOr(input.omitted, 0), omitted),
        truncated,
        savedAt: now,
        reports: keep,
    };
}

/**
 * Turn a value read out of IndexedDB back into a record, or refuse it.
 *
 * Stricter than the save path in exactly one place, and it is the important
 * one: a stored record with no usable `startedAt` is discarded rather than
 * re-dated. Anything else would let a corrupted timestamp reset the retention
 * clock on data that is already old.
 */
function fromStored(value: any): ScanRecord | null {
    if (!value || typeof value !== "object") return null;
    const startedAt = Number(value.startedAt);
    if (!Number.isFinite(startedAt) || startedAt <= 0) return null;
    if (typeof value.id !== "string" || !value.id) return null;

    const reports: MemberReport[] = Array.isArray(value.reports)
        ? value.reports.filter((report: any) => report && typeof report.discordId === "string")
        : [];

    return {
        id: value.id,
        startedAt,
        finishedAt: Number.isFinite(Number(value.finishedAt)) ? Number(value.finishedAt) : undefined,
        backend: typeof value.backend === "string" ? value.backend : "rotector",
        sourceKind: typeof value.sourceKind === "string" ? value.sourceKind : "",
        sourceLabel: typeof value.sourceLabel === "string" ? value.sourceLabel : "members",
        guildId: typeof value.guildId === "string" ? value.guildId : undefined,
        scanned: numberOr(value.scanned, reports.length),
        findings: numberOr(value.findings, 0),
        unanswered: numberOr(value.unanswered, 0),
        coverageNote: typeof value.coverageNote === "string" ? value.coverageNote : undefined,
        omitted: numberOr(value.omitted, 0),
        truncated: numberOr(value.truncated, 0),
        savedAt: numberOr(value.savedAt, startedAt),
        reports,
    };
}

// --------------------------------------------------------------------------
// the IndexedDB half, which is allowed to fail
// --------------------------------------------------------------------------

/**
 * Race a promise against a clock.
 *
 * Used for every database call, not only the open: a transaction against a
 * store the browser has decided to block can hang as easily as an open can, and
 * a hung write inside a scan is exactly the failure this module promises never
 * to cause.
 */
function withTimeout<T>(promise: Promise<T>, ms: number, what: string): Promise<T> {
    return new Promise<T>((resolve, reject) => {
        let settled = false;
        const timer = setTimeout(() => {
            if (settled) return;
            settled = true;
            reject(new Error(`${what} did not answer within ${Math.round(ms / 1000)}s`));
        }, ms);
        promise.then(
            value => {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                resolve(value);
            },
            err => {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                reject(err);
            }
        );
    });
}

class ScanHistory {
    /** the in-memory mirror every React read is served from, so nothing awaits a render */
    private mirror = new Map<string, ScanRecord>();
    private listeners = new Set<() => void>();

    /** the idb-keyval store handle; `null` once we have decided we cannot have one */
    private handle: any = undefined;

    private hydrating: Promise<void> | null = null;
    private hydrated = false;

    /** set when a database operation failed; the reason is shown to the user */
    private problem = "";

    private sweepTimer: any = null;
    /** set while a coalesced notification is waiting for the end of the tick */
    private notifyTimer: any = null;

    /**
     * Bumped by `stop()` and by `purge()`. A hydrate or a write that was already
     * in flight checks this before it touches the mirror, so a disabled plugin
     * cannot repopulate what it just cleared — the same guard store.ts uses, and
     * for the same reason.
     */
    private generation = 0;

    // ------------------------------------------------------------------
    // the store handle
    // ------------------------------------------------------------------

    /**
     * The object store, or `null` when there cannot be one.
     *
     * Built lazily rather than at import: `createStore` calls
     * `indexedDB.open` immediately, and a plugin that opened a database merely
     * by being loaded would be doing work for a user who never opened the
     * history. `indexedDB` itself can be missing (a hardened browser profile,
     * a private session), which is a refusal rather than a crash.
     */
    private store(): any {
        if (this.handle !== undefined) return this.handle;
        try {
            if (typeof indexedDB === "undefined" || indexedDB == null) {
                this.fail("This client has no IndexedDB, so past scans cannot be stored.");
                this.handle = null;
                return null;
            }
            if (typeof (DataStore as any)?.createStore !== "function") {
                this.fail("The host client's DataStore API is not available.");
                this.handle = null;
                return null;
            }
            this.handle = (DataStore as any).createStore(DB_NAME, STORE_NAME);
        } catch (err) {
            this.fail(`The scan history database could not be opened (${describeError(err)}).`);
            this.handle = null;
        }
        return this.handle;
    }

    /**
     * Record that persistence is not working, once.
     *
     * "History off this session" rather than a thrown error: a scan that
     * finished must not fail because a database refused a write, and a user who
     * is out of disk quota needs a sentence rather than a stack trace.
     */
    private fail(reason: string): void {
        if (this.problem === reason) return;
        this.problem = reason;
        logger.warn(`scan history disabled: ${reason}`);
        this.notify();
    }

    /** Run one database operation, converting any failure into "history off". */
    private async run<T>(what: string, fn: (store: any) => Promise<T>): Promise<T | undefined> {
        const store = this.store();
        if (!store) return undefined;
        try {
            return await withTimeout(Promise.resolve(fn(store)), OPEN_TIMEOUT_MS, what);
        } catch (err) {
            this.fail(`${what} failed: ${describeError(err)}. Past scans are not being saved this session.`);
            return undefined;
        }
    }

    // ------------------------------------------------------------------
    // hydrate
    // ------------------------------------------------------------------

    /**
     * Read the database into the mirror, once.
     *
     * Safe to call from anywhere and as often as anyone likes; the promise is
     * cached. Nothing in the read path *awaits* this — `list()` answers out of
     * the mirror immediately and is simply empty until the read lands, at which
     * point subscribers are told. That is the difference between a hook that
     * renders and a hook that suspends a Discord modal on a promise.
     */
    ready(): Promise<void> {
        if (this.hydrated) return Promise.resolve();
        if (this.hydrating) return this.hydrating;

        const gen = this.generation;
        this.hydrating = (async () => {
            const rows = await this.run("Reading the scan history", store =>
                (DataStore as any).entries(store));

            if (gen !== this.generation) return;
            this.hydrated = true;

            if (!Array.isArray(rows)) {
                // Either the database is unavailable — `run` has already said so
                // — or it is empty. Either way there is nothing to mirror.
                this.notify();
                return;
            }

            const now = Date.now();
            const expired: string[] = [];
            for (const row of rows) {
                const key = Array.isArray(row) ? row[0] : undefined;
                const value = Array.isArray(row) ? row[1] : undefined;
                if (typeof key !== "string" || !key.startsWith(KEY_PREFIX)) continue;
                const record = fromStored(value);
                // An unreadable or out-of-date record is deleted rather than
                // skipped. Leaving it on disk would be retaining a response we
                // have already decided we may not serve.
                if (!record || isExpired(record, now)) {
                    expired.push(key);
                    continue;
                }
                this.mirror.set(record.id, record);
            }

            if (expired.length) void this.dropKeys(expired);
            const evicted = this.enforceLimit();
            if (evicted.length) void this.dropKeys(evicted.map(id => KEY_PREFIX + id));
            this.ensureSweep();
            this.notify();
        })();

        return this.hydrating;
    }

    // ------------------------------------------------------------------
    // reads — every one of them re-checks the ceiling
    // ------------------------------------------------------------------

    /**
     * Every retained scan, newest first, already swept.
     *
     * The sweep here is synchronous and unconditional. The interval may not have
     * run yet, may have been cleared by `stop()`, or the client may have been
     * asleep for eight hours with the timer frozen — none of which may result in
     * a record past the ceiling being handed to a caller.
     */
    list(): ScanRecord[] {
        const now = Date.now();
        this.sweepMirror(now);
        const out: ScanRecord[] = [];
        for (const record of Array.from(this.mirror.values())) {
            out.push(pruneReports(record, now));
        }
        out.sort((a, b) => b.startedAt - a.startedAt);
        return out;
    }

    get(id: string): ScanRecord | undefined {
        if (typeof id !== "string" || !id) return undefined;
        const record = this.mirror.get(id);
        if (!record) return undefined;
        const now = Date.now();
        if (isExpired(record, now)) {
            // Dropped on the way past, exactly as store.ts's `get` does. A read
            // is the last place the ceiling can be enforced and it is the place
            // it matters most.
            this.mirror.delete(id);
            void this.dropKeys([KEY_PREFIX + id]);
            this.notifySoon();
            return undefined;
        }
        return pruneReports(record, now);
    }

    stats(): HistoryStats {
        const records = this.list();
        const now = Date.now();
        let members = 0;
        let findings = 0;
        let stale = 0;
        let oldest: number | undefined;
        for (const record of records) {
            members += Number(record.scanned) || 0;
            findings += Number(record.findings) || 0;
            if (oldest === undefined || record.startedAt < oldest) oldest = record.startedAt;
            // The reports have already been pruned to what is inside the
            // ceiling by `list()`, so this counts what is still held and past
            // the terms' window — never what has been dropped.
            const reports = Array.isArray(record.reports) ? record.reports : [];
            for (const report of reports) if (isStale(report, now)) stale++;
        }
        return { scans: records.length, members, findings, oldest, stale };
    }

    status(): HistoryStatus {
        return { on: !this.problem, reason: this.problem, hydrated: this.hydrated };
    }

    // ------------------------------------------------------------------
    // writes
    // ------------------------------------------------------------------

    /**
     * Write one scan down.
     *
     * The mirror is updated first and the disk second, so the surface that
     * asked for the save sees it immediately and a database that is refusing
     * writes costs the user the *persistence* rather than the record. It never
     * rejects: the caller is a scan, and a scan must not fail because a history
     * could not be written.
     */
    async save(record: NewScanRecord | ScanRecord): Promise<void> {
        if (!historyEnabled()) return;

        const clean = sanitizeForSave(record);
        if (!clean) return;

        // Nothing past the ceiling is ever written. A scan that started
        // yesterday and finished now is not a new record, it is an expired one.
        if (isExpired(clean, Date.now())) return;

        const gen = this.generation;
        // Hydrate first so the eviction below sees everything that is already
        // there; without it the first save of a session would evict against an
        // empty mirror and leave the older records on disk unaccounted for.
        await this.ready();
        if (gen !== this.generation) return;

        this.mirror.set(clean.id, clean);
        const evicted = this.enforceLimit();
        this.ensureSweep();
        this.notify();

        await this.run("Saving a scan", store =>
            (DataStore as any).set(KEY_PREFIX + clean.id, clean, store));
        if (evicted.length) await this.dropKeys(evicted.map(id => KEY_PREFIX + id));
    }

    async remove(id: string): Promise<void> {
        if (typeof id !== "string" || !id) return;
        const had = this.mirror.delete(id);
        if (had) this.notify();
        await this.dropKeys([KEY_PREFIX + id]);
    }

    /**
     * Wipe the whole database.
     *
     * `clear` on our own object store, which is why this plugin has one: a
     * user who asks for their scan history to be gone gets exactly that, with
     * nothing else the client keeps touched.
     */
    async purge(): Promise<void> {
        // The generation bump matters here for the same reason it does in
        // store.ts: a hydrate or a save already in flight must not write its
        // results back into the mirror somebody has just emptied.
        this.generation++;
        this.hydrating = null;
        this.hydrated = true;
        this.mirror.clear();
        this.stopSweep();
        this.notify();
        await this.run("Clearing the scan history", store => (DataStore as any).clear(store));
    }

    /**
     * Drop everything past the ceiling, from the mirror and from disk.
     *
     * Returns how many records went, so a caller (the modal, the settings
     * panel) can say so rather than having the number disappear.
     */
    async sweep(): Promise<number> {
        const now = Date.now();
        const gone = this.sweepMirror(now);
        if (gone.length) await this.dropKeys(gone.map(id => KEY_PREFIX + id));
        return gone.length;
    }

    // ------------------------------------------------------------------
    // internals
    // ------------------------------------------------------------------

    /**
     * The synchronous half of the sweep: drop expired records from the mirror.
     *
     * Separate from the disk half because every read calls it, and a read must
     * not await anything. The disk deletions are queued by the caller.
     */
    private sweepMirror(now: number): string[] {
        const gone: string[] = [];
        for (const [id, record] of Array.from(this.mirror)) {
            if (!isExpired(record, now)) continue;
            this.mirror.delete(id);
            gone.push(id);
        }
        if (gone.length) {
            // Queued rather than awaited: `list()` is called from a render.
            void this.dropKeys(gone.map(id => KEY_PREFIX + id));
            if (!this.mirror.size) this.stopSweep();
            this.notifySoon();
        }
        return gone;
    }

    /**
     * Keep at most `historyLimit()` scans, oldest first out.
     *
     * Returns the ids that were evicted so the caller can delete them from
     * disk. Eviction is a cap on how *many* scans are kept and never on how
     * long: the ceiling is the only thing that decides that.
     */
    private enforceLimit(): string[] {
        const limit = historyLimit();
        if (this.mirror.size <= limit) return [];

        const ordered = Array.from(this.mirror.values()).sort((a, b) => a.startedAt - b.startedAt);
        const evicted: string[] = [];
        for (const record of ordered) {
            if (this.mirror.size <= limit) break;
            this.mirror.delete(record.id);
            evicted.push(record.id);
        }
        return evicted;
    }

    private async dropKeys(keys: string[]): Promise<void> {
        if (!keys?.length) return;
        await this.run("Deleting expired scans", store =>
            (DataStore as any).delMany(keys, store));
    }

    private ensureSweep(): void {
        if (this.sweepTimer !== null) return;
        if (!this.mirror.size) return;
        this.sweepTimer = setInterval(() => { void this.sweep(); }, SWEEP_INTERVAL_MS);
    }

    private stopSweep(): void {
        if (this.sweepTimer === null) return;
        clearInterval(this.sweepTimer);
        this.sweepTimer = null;
    }

    subscribe(fn: () => void): () => void {
        if (typeof fn !== "function") return () => { };
        this.listeners.add(fn);
        return () => { this.listeners.delete(fn); };
    }

    /**
     * Notify at the end of the tick rather than now.
     *
     * The read paths sweep, and a read path runs inside a React render — so a
     * notification sent from there would call `setState` on other components
     * while one is rendering, which React refuses. The expiry is real either
     * way; only the telling waits for the render to finish.
     */
    private notifySoon(): void {
        if (this.notifyTimer !== null) return;
        this.notifyTimer = setTimeout(() => {
            this.notifyTimer = null;
            this.notify();
        }, 0);
    }

    private notify(): void {
        if (this.notifyTimer !== null) {
            clearTimeout(this.notifyTimer);
            this.notifyTimer = null;
        }
        for (const fn of Array.from(this.listeners)) {
            try {
                fn();
            } catch {
                // A broken subscriber must not stop the others being told.
            }
        }
    }

    /**
     * Clear the timers and forget the mirror.
     *
     * The database is deliberately *not* cleared: unlike the in-memory report
     * cache, this data is meant to survive a restart, and switching the plugin
     * off for a minute is not the same as asking for the history to be deleted.
     * The ceiling still applies to it while it sits there — the next hydrate
     * drops anything that aged out in the meantime, and a user who wants it gone
     * now has `purge()`.
     */
    stop(): void {
        this.generation++;
        this.stopSweep();
        this.mirror.clear();
        this.hydrating = null;
        this.hydrated = false;
        this.notify();
    }
}

export const scanHistory = new ScanHistory();

// --------------------------------------------------------------------------
// React plumbing
// --------------------------------------------------------------------------

/**
 * Built from `useState` + `useEffect` rather than `useSyncExternalStore`, for
 * the reason store.ts gives: React here is whichever version Discord shipped,
 * and a hook that is missing is not a degraded render, it is a thrown error
 * inside a modal.
 */
function useHistoryValue<T>(read: () => T): T {
    const [value, setValue] = useState<T>(() => attempt(read, undefined as unknown as T));

    useEffect(() => {
        let alive = true;
        const refresh = () => {
            if (alive) setValue(attempt(read, undefined as unknown as T));
        };
        // Hydration is kicked off from the effect and never from the render, so
        // no component ever waits on IndexedDB to draw its first frame.
        void scanHistory.ready().then(refresh, refresh);
        refresh();
        const unsubscribe = scanHistory.subscribe(refresh);
        // A record can expire while a window sits open. The ceiling is a
        // deadline rather than an event, so the hook re-reads on its own clock
        // as well as on the store's notifications.
        const timer = setInterval(refresh, 60_000);
        return () => {
            alive = false;
            clearInterval(timer);
            attempt(() => unsubscribe(), undefined);
        };
    }, []);

    return value;
}

export function useScanHistory(): ScanRecord[] {
    return useHistoryValue(() => scanHistory.list()) ?? [];
}

export function useHistoryStats(): HistoryStats {
    return useHistoryValue(() => scanHistory.stats())
        ?? { scans: 0, members: 0, findings: 0, stale: 0 };
}

export function useHistoryStatus(): HistoryStatus {
    return useHistoryValue(() => scanHistory.status())
        ?? { on: true, reason: "", hydrated: false };
}
