/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Scan jobs: a queue, a shared budget, and the rules for running several.
 * Ported from rsb/jobs.py.
 *
 * One scan at a time was the terminal app's whole design until jobs arrived,
 * and it is what the first version of this plugin's scan modal did too: one
 * source, one progress bar, one ledger. Running several means each scan owning
 * its own everything and a queue deciding which of them is allowed to be
 * running. Three rules shape this, and the first is not a preference:
 *
 *  - **The budget is shared, per backend.** Rotector's Terms of Use forbid
 *    circumventing the rate limit, and a limiter per job would multiply the
 *    request rate by however many scans somebody queued. That is why this
 *    module builds *one* backend client per backend name and hands it to every
 *    job on that backend, and why it never passes a `limiter` of its own into
 *    ClientOptions: api/ratelimit.ts keys its limiters on the base URL through
 *    `sharedLimiter`, so even a client built somewhere else spends from the same
 *    window. Ten concurrent scans are exactly as polite as one and simply finish
 *    slower. `maxConcurrentJobs` decides how many run at once and is a
 *    concurrency limit only — it is never a reason to raise the rate, and there
 *    is deliberately no code path here that lets it become one.
 *  - **Concurrency is small and deliberate.** Two jobs against one budget
 *    already halve each other; twenty just add scheduling noise and make every
 *    one of them look stalled. The cap is configurable, low, and clamped.
 *  - **Nothing is silently dropped.** A stopped job keeps what it collected,
 *    because a partial scan is still an answer about the members it did reach —
 *    the same reason the app reports coverage as a fraction rather than a
 *    checkmark. And one job failing settles that job and pumps the queue; it
 *    never takes the other scans down with it.
 *
 * Results are not held here. Every report a job produces goes into `store.ts`
 * like every other lookup, under the epoch the job captured when it started, so
 * a purge or a plugin stop halfway through a scan is not quietly undone by the
 * scan finishing. That store is where Rotector's 23h retention ceiling lives and
 * there is deliberately no second cache in this module: a job holds member ids,
 * counts and timings — nothing that came back from the API.
 *
 * The Python version is passive: it never starts anything, and the Textual app
 * asks `runnable()` what it may begin. A renderer has no such driver loop, so
 * this queue runs its own jobs. Everything else — the ordering rules, the
 * duplicate check, the state labels — is ported as it stands.
 */

import { useEffect, useState } from "@webpack/common";

import type { LookupBackend } from "../api/backend";
import { isAbortError } from "../api/ratelimit";
import type { ClientOptions, ScanProgress } from "../api/rotector";
import type { MemberReport } from "../api/types";
import { reportVerdict } from "../api/types";
import { Verdict } from "../api/verdict";
import type { MemberSource } from "../members";
import { clientOptions, settings } from "../settings";
import { reportStore } from "../store";

/**
 * The loader hands every module a CommonJS `require`, so this is a real
 * function at runtime; the declaration only exists to keep the identifier from
 * looking undeclared to a reader. See `loadBackendModule` for why it is used
 * here instead of a plain import.
 */
declare const require: (specifier: string) => any;

export type JobState = "queued" | "running" | "done" | "failed" | "stopped";

/** What the job list shows for each state, in the Python module's words. */
export const STATE_LABEL: Record<JobState, string> = {
    running: "RUNNING",
    queued: "QUEUED",
    done: "DONE",
    failed: "FAILED",
    stopped: "STOPPED",
};

/** Sort rank: running first, then waiting, then everything that has finished. */
const STATE_RANK: Record<JobState, number> = {
    running: 0,
    queued: 1,
    done: 2,
    failed: 2,
    stopped: 2,
};

/**
 * One scan of one source against one backend.
 *
 * A `Job` is a snapshot, not a handle: every change replaces the object so a
 * React list can compare by identity instead of re-rendering every row on every
 * progress tick. Hold the `id` and read through `jobQueue.get(id)`, `useJob(id)`
 * or `useJobs()` — a `Job` kept in a variable stops being true the moment the
 * scan moves on.
 */
export interface Job {
    id: string;
    label: string;
    source: MemberSource;
    /**
     * Which service answers "is this member flagged" for this job. It is part
     * of the job rather than a global because scanning one server against both
     * backends is the feature — the two answers must not land on top of each
     * other — and because the budget is shared per backend, not per queue.
     */
    backend: string;
    state: JobState;
    progress: ScanProgress;
    ids: string[];
    startedAt?: number;
    finishedAt?: number;
    error?: string;
    /** set the moment a stop is asked for, while the scan is still unwinding */
    stopping: boolean;
    /**
     * Members this job has a result for — answered *or* errored. A stopped scan
     * still files the members it never reached, as errors, so `found` on its own
     * is not a count of members checked: `found - unanswered` is.
     */
    found: number;
    /** of those, how many are at or above INFO — the same bar the marks use */
    findings: number;
    /**
     * Of those, how many came back with an error rather than an answer. They are
     * not clean and must never be presented as clean; they are unexamined.
     */
    unanswered: number;
    queuedAt: number;
    /** higher runs sooner; equal priority falls back to the order added */
    priority: number;
    /** pre-scan estimate in seconds, or null when the backend cannot say */
    etaSeconds: number | null;
}

export interface EnqueueOptions {
    /** override the configured backend, for a deliberate second opinion */
    backend?: string;
    /**
     * What to do about an existing job for the same source and backend.
     * Defaults to the `onRescan` setting; see `enqueue`.
     */
    rescan?: "ask" | "reuse" | "rescan";
    /** start above everything already waiting */
    priority?: number;
}

/** Counts for the queue's own header line. */
export interface JobStats {
    total: number;
    running: number;
    queued: number;
    done: number;
    failed: number;
    stopped: number;
    /** members answered across every job in the queue */
    found: number;
    findings: number;
}

interface JobRecord {
    job: Job;
    controller: AbortController | null;
    /** ids already counted, so a final sweep cannot double-count a batch */
    seen: Set<string>;
    /** the report store generation this job started under */
    epoch: number;
    /** insertion order, the tie-break of last resort */
    seq: number;
}

// --------------------------------------------------------------------------
// settings, read the only way settings may be read here
// --------------------------------------------------------------------------

/**
 * One setting, without ever throwing.
 *
 * `settings.store` throws until the plugin is registered and this module is
 * constructed at import time, so every read has to survive that window.
 */
function setting<T>(key: string, fallback: T): T {
    try {
        const store = (settings as any)?.store;
        if (!store) return fallback;
        const value = store[key];
        return value === undefined || value === null ? fallback : value as T;
    } catch {
        return fallback;
    }
}

function num(value: unknown, fallback: number, min: number, max: number): number {
    const parsed = typeof value === "number" ? value : Number(value);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.min(max, Math.max(min, parsed));
}

/** How many jobs may hold a slot at once. Clamped the way rsb/config.py validates it. */
function maxConcurrentJobs(): number {
    return Math.floor(num(setting<number>("maxConcurrentJobs", 2), 2, 1, 8));
}

/**
 * The whole settings record, or an empty one when it cannot be read.
 *
 * `backendOptionsFrom` takes a plain record rather than the settings object, so
 * this is what feeds it. Same defensiveness as `setting`: the store getter
 * throws until the plugin is registered, and an empty record simply yields the
 * measured per-backend defaults.
 */
function storedSettings(): Record<string, any> {
    try {
        return ((settings as any)?.store as Record<string, any>) ?? {};
    } catch {
        return {};
    }
}

/**
 * How the client for one backend should be built.
 *
 * Keyed on the backend the *job* holds, never on the one the settings currently
 * name. Those two disagree more easily than it looks: `EnqueueOptions.backend`
 * overrides the setting outright for a deliberate second opinion, and a job
 * queued against Rotector can sit waiting for a slot while somebody switches the
 * backend setting to Okappiki. Deriving the options from settings a second time
 * would hand that job Okappiki's six-per-second window while it scans Rotector,
 * or Rotector's fifty-per-ten-seconds at Okappiki — a fifty-fold overspend of
 * somebody else's budget, from a settings change the job never saw.
 * `backendOptionsFrom` reads the keys that belong to the backend it is given, so
 * the options and the backend cannot come apart.
 *
 * The Okappiki concurrency ceiling is not restated here either. It has exactly
 * one owner — MAX_CONCURRENCY in api/okappiki.ts, which is a measured knee
 * rather than a preference — and api/backend.ts clamps against that same
 * constant. A third copy of the number in this file is a third place for it to
 * drift upwards, and every request above the knee costs Okappiki a live Rotector
 * lookup out of their budget for no throughput in return.
 *
 * Note what is *not* here either: a `limiter`. api/rotector.ts only accepts one
 * for a caller with a genuinely separate budget, and a job never has one — every
 * job on a host spends from that host's shared window.
 */
function optionsFor(backend: string): ClientOptions {
    const from = loadBackendModule()?.backendOptionsFrom;
    if (typeof from === "function") {
        try {
            return from(backend, storedSettings()) ?? {};
        } catch {
            /* fall through to the settings helper below */
        }
    }
    // api/backend.ts is unavailable. settings.ts's `clientOptions` derives the
    // backend from settings itself, so it is only safe when that is the backend
    // this job actually runs against; for anything else the client's own
    // measured defaults are the honest answer, and they are the conservative one.
    try {
        if (backend === String(setting<string>("backend", "rotector") ?? "rotector")) {
            return clientOptions();
        }
    } catch {
        // Before the plugin is registered; the client's own defaults are right.
    }
    return {};
}

// --------------------------------------------------------------------------
// the backend module
// --------------------------------------------------------------------------

/**
 * `api/backend.ts`, loaded on first use rather than imported at the top.
 *
 * The loader resolves a relative import by evaluating the file, and a module
 * that is not there throws out of `require`. This one is only needed once a
 * scan actually starts, so requiring it lazily means a missing or broken
 * backend module costs the user a readable error on the job that wanted it
 * instead of taking the whole plugin — marks, profiles, reports — down at load.
 * The loader caches modules, so this is one `require` per session.
 */
let backendModule: any = null;
let backendModuleTried = false;

function loadBackendModule(): any {
    if (backendModuleTried) return backendModule;
    backendModuleTried = true;
    try {
        backendModule = require("../api/backend");
    } catch {
        backendModule = null;
    }
    return backendModule;
}

/** The backends a job may run against. */
export function backendNames(): string[] {
    const names = loadBackendModule()?.BACKENDS;
    if (Array.isArray(names) && names.length) return names.map((n: any) => String(n));
    // The literal is the same pair rsb/config.py's BACKENDS holds; it is only
    // reached when api/backend.ts is missing, and every path that uses it still
    // fails loudly when the client itself cannot be built.
    return ["rotector", "okappiki"];
}

export function backendLabel(name: string): string {
    const label = loadBackendModule()?.backendLabel;
    if (typeof label === "function") {
        try {
            return String(label(name));
        } catch {
            /* fall through to the raw name */
        }
    }
    return String(name || "");
}

/**
 * Build the client for one backend.
 *
 * An unrecognised name throws rather than falling back to Rotector: a typo must
 * not silently scan against a service the operator did not choose.
 */
function buildBackendClient(name: string, opts: ClientOptions): LookupBackend {
    const mod = loadBackendModule();
    const build = mod?.buildBackend;
    if (typeof build === "function") return build(name, opts);
    if (name === "rotector") {
        // api/backend.ts is unavailable but the Rotector client is not, and it
        // is the same client the store already uses — so a Rotector scan still
        // works. Nothing else is guessed at.
        return reportStore.client() as unknown as LookupBackend;
    }
    throw new Error(
        `The "${name}" backend is not available in this build of the plugin. `
        + "Choose Rotector in the plugin's settings, or reinstall the plugin folder — "
        + "api/backend.ts is missing."
    );
}

// --------------------------------------------------------------------------
// small helpers
// --------------------------------------------------------------------------

function isSnowflake(id: unknown): id is string {
    return typeof id === "string" && id.length > 0 && id.length < 32 && /^\d+$/.test(id);
}

function uniqueIds(ids: any): string[] {
    const seen = new Set<string>();
    const out: string[] = [];
    for (const raw of Array.isArray(ids) ? ids : []) {
        const id = typeof raw === "string" ? raw : String(raw ?? "");
        if (!isSnowflake(id) || seen.has(id)) continue;
        seen.add(id);
        out.push(id);
    }
    return out;
}

function describeError(err: any): string {
    if (!err) return "The scan failed for an unknown reason.";
    if (typeof err === "string") return err;
    const message = typeof err.message === "string" ? err.message : String(err);
    return message || "The scan failed for an unknown reason.";
}

function newController(): AbortController | null {
    try {
        return typeof AbortController === "undefined" ? null : new AbortController();
    } catch {
        return null;
    }
}

function sourceLabel(source: MemberSource | undefined): string {
    const label = source && typeof source.label === "string" ? source.label.trim() : "";
    return label || "Unnamed source";
}

/** Sources are the same job when they name the same thing. */
function sourceKey(source: MemberSource | undefined): string {
    if (!source) return "";
    return `${source.kind ?? "?"}:${source.guildId ?? ""}:${source.id ?? ""}`;
}

/**
 * Seconds a job has actually spent scanning.
 *
 * Exported because the job list has to print it and only this module knows
 * which end of the clock is live.
 */
export function jobElapsed(job: Job | undefined): number {
    if (!job?.startedAt) return 0;
    const end = job.finishedAt ?? Date.now();
    return Math.max(0, end - job.startedAt) / 1000;
}

/** `true` while this job is still going to spend rate limit. */
export function jobIsActive(job: Job | undefined): boolean {
    return job?.state === "running" || job?.state === "queued";
}

// --------------------------------------------------------------------------
// the queue
// --------------------------------------------------------------------------

export class JobQueue {
    private records: JobRecord[] = [];
    private listeners = new Set<() => void>();
    private seq = 0;
    private notifyTimer: any = null;
    /** the ordered public view, rebuilt only when something actually changed */
    private ordered: Job[] | null = null;
    /** one client per backend name, keyed by the settings it was built from */
    private clients = new Map<string, { key: string; client: LookupBackend; }>();
    /**
     * Clients replaced while a scan was still holding them. See `retire`: they
     * cannot be purged at the moment they are replaced without making that scan
     * re-request members it already has answers for, so they wait here.
     */
    private retired = new Set<LookupBackend>();

    // -- membership --------------------------------------------------------

    /**
     * Add a scan and start it when a slot is free.
     *
     * `onRescan` decides what an existing job for the same source and backend
     * means. `reuse` hands back the job that already exists — including a
     * finished one, whose reports are still in the store — and spends nothing.
     * `rescan` always queues a new one. `ask` cannot ask from here, so it takes
     * the conservative half of the question: an *unfinished* duplicate is
     * returned as-is, because queueing the same pair twice while it is already
     * running is a mis-click rather than an instruction, and the surface that
     * wants to offer the choice should call `duplicateOf` first and pass an
     * explicit `rescan` mode.
     */
    enqueue(source: MemberSource, ids: string[], label?: string, opts: EnqueueOptions = {}): Job {
        const backend = String(opts.backend ?? setting<string>("backend", "rotector") ?? "rotector");
        const mode = String(opts.rescan ?? setting<string>("onRescan", "ask") ?? "ask");

        const existing = this.duplicateOf(source, backend, mode !== "reuse");
        if (existing && mode !== "rescan") return existing;

        const wanted = uniqueIds(ids);
        const record: JobRecord = {
            job: {
                id: `job-${++this.seq}`,
                // The backend is in the label because the whole point is that
                // the same source can be in this list twice.
                label: String(label || `${sourceLabel(source)} · ${backendLabel(backend)}`),
                source,
                backend,
                state: "queued",
                progress: { stage: "Queued", done: 0, total: wanted.length },
                ids: wanted,
                stopping: false,
                found: 0,
                findings: 0,
                unanswered: 0,
                queuedAt: Date.now(),
                priority: Math.floor(num(opts.priority, 0, -1e6, 1e6)),
                etaSeconds: null,
            },
            controller: null,
            seen: new Set<string>(),
            epoch: 0,
            seq: this.seq,
        };
        this.records.push(record);
        this.ordered = null;

        if (!backendNames().includes(backend)) {
            // Refused here rather than at run time, and never resolved to a
            // default: a mistyped backend must not silently scan against a
            // service the operator did not choose.
            this.settle(record, "failed",
                `"${backend}" is not a backend this plugin knows. Choose one of `
                + `${backendNames().join(", ")} in the plugin's settings.`);
            return record.job;
        }

        if (!wanted.length) {
            this.settle(record, "failed",
                "There was nobody to scan — the source produced no member ids. "
                + "That is not the same as a clean result: nothing was checked.");
            return record.job;
        }

        this.patch(record, { etaSeconds: this.estimate(backend, wanted.length) });
        this.notify();
        this.pump();
        return record.job;
    }

    /**
     * An existing job covering this source and backend.
     *
     * The same source on a *different* backend is the feature, so only the pair
     * counts as a duplicate.
     */
    duplicateOf(source: MemberSource, backend: string, unfinishedOnly = true): Job | undefined {
        const key = sourceKey(source);
        if (!key) return undefined;
        for (const record of this.records) {
            if (record.job.backend !== backend) continue;
            if (sourceKey(record.job.source) !== key) continue;
            if (unfinishedOnly && !jobIsActive(record.job)) continue;
            return record.job;
        }
        return undefined;
    }

    get(id: string): Job | undefined {
        return this.find(id)?.job;
    }

    list(): Job[] {
        if (!this.ordered) {
            // Running first, then by priority, then by age — age last so equal
            // priorities keep the order they were added in. A queue that
            // reshuffles itself is one nobody can plan against.
            const sorted = this.records.slice().sort((a, b) => {
                const rank = STATE_RANK[a.job.state] - STATE_RANK[b.job.state];
                if (rank !== 0) return rank;
                if (a.job.priority !== b.job.priority) return b.job.priority - a.job.priority;
                if (a.job.queuedAt !== b.job.queuedAt) return a.job.queuedAt - b.job.queuedAt;
                return a.seq - b.seq;
            });
            this.ordered = sorted.map(record => record.job);
        }
        return this.ordered;
    }

    /**
     * Drop a finished job from the list.
     *
     * Only ever a finished one: a running job is stopped first, so its worker is
     * never left writing into a job the queue has forgotten.
     */
    remove(id: string): boolean {
        const record = this.find(id);
        if (!record || jobIsActive(record.job)) return false;
        const index = this.records.indexOf(record);
        if (index < 0) return false;
        this.records.splice(index, 1);
        this.ordered = null;
        this.notify();
        return true;
    }

    clearFinished(): number {
        const gone = this.records.filter(record => !jobIsActive(record.job));
        for (const record of gone) {
            const index = this.records.indexOf(record);
            if (index >= 0) this.records.splice(index, 1);
        }
        if (gone.length) {
            this.ordered = null;
            this.notify();
        }
        return gone.length;
    }

    // -- stopping ----------------------------------------------------------

    /**
     * Stop one job.
     *
     * A queued job settles immediately; a running one is asked to abort and
     * settles when its scan unwinds, keeping every report it had already
     * published. `stopping` is set at once so the UI can say so rather than
     * looking frozen.
     */
    stop(id: string): void {
        const record = this.find(id);
        if (!record || !jobIsActive(record.job)) return;

        if (record.job.state === "queued") {
            this.settle(record, "stopped");
            this.pump();
            return;
        }

        this.patch(record, { stopping: true });
        this.notify();
        try {
            record.controller?.abort();
        } catch {
            // already aborted, or a runtime without AbortController — the scan
            // then simply runs to completion, which is slow but not wrong.
        }
    }

    stopAll(): void {
        for (const record of this.records.slice()) {
            if (jobIsActive(record.job)) this.stop(record.job.id);
        }
    }

    /**
     * Stop everything and forget it.
     *
     * For the plugin being switched off: the jobs hold member ids and counts
     * derived from Rotector's answers, and keeping either behind a disabled
     * plugin would be retention with nothing left to serve.
     */
    reset(): void {
        this.stopAll();
        this.records = [];
        this.ordered = null;
        this.dropClients();
        this.notify();
    }

    /**
     * Let go of every backend client, purging each one first.
     *
     * `reset` is the plugin being switched off, so leaving a client merely
     * dereferenced would leave Rotector's data and a running sweep timer behind
     * a disabled plugin — the retention the terms forbid, and the reason
     * `purgeCache` is on the shared backend surface at all. `stopAll` has
     * already aborted every running scan by the time this is called, so the
     * usual reason to defer a purge — a live scan that would re-request what it
     * had already been answered — does not apply: those scans are unwinding and
     * will issue nothing further.
     */
    private dropClients(): void {
        for (const entry of Array.from(this.clients.values())) this.purgeClient(entry?.client);
        this.clients.clear();
        for (const client of Array.from(this.retired)) this.purgeClient(client);
        this.retired.clear();
    }

    // -- reporting ---------------------------------------------------------

    stats(): JobStats {
        const stats: JobStats = {
            total: this.records.length,
            running: 0, queued: 0, done: 0, failed: 0, stopped: 0,
            found: 0, findings: 0,
        };
        for (const { job } of this.records) {
            (stats as any)[job.state] = ((stats as any)[job.state] ?? 0) + 1;
            stats.found += job.found;
            stats.findings += job.findings;
        }
        return stats;
    }

    /** `"1 running, 2 queued"`, in the Python module's wording. */
    summary(): string {
        if (!this.records.length) return "No scans queued.";
        const counts = this.stats();
        const parts: string[] = [];
        for (const state of ["running", "queued", "done", "failed", "stopped"] as JobState[]) {
            const n = (counts as any)[state] as number;
            if (n) parts.push(`${n} ${STATE_LABEL[state].toLowerCase()}`);
        }
        return parts.join(", ");
    }

    /**
     * How many running jobs are drawing on each backend's budget.
     *
     * The job list shows this: two scans against one backend is not a problem,
     * but it is the reason both look half as fast, and saying so beats leaving
     * somebody to wonder whether the plugin has stalled.
     */
    sharing(): Record<string, number> {
        const counts: Record<string, number> = {};
        for (const { job } of this.records) {
            if (job.state !== "running") continue;
            counts[job.backend] = (counts[job.backend] ?? 0) + 1;
        }
        return counts;
    }

    /** `true` while anything is running or waiting to run. */
    hasActive(): boolean {
        return this.records.some(record => jobIsActive(record.job));
    }

    /**
     * Put a job at the head of the queue.
     *
     * Priority rather than list position, so the ordering survives anything
     * else being added afterwards. Nothing running is stopped — promoting a job
     * says "next", and taking a slot from a scan mid-flight would throw away
     * work to answer a question about order.
     */
    prioritise(id: string): boolean {
        const record = this.find(id);
        if (!record || !jobIsActive(record.job)) return false;
        let highest = 0;
        for (const other of this.records) {
            if (other !== record) highest = Math.max(highest, other.job.priority);
        }
        this.patch(record, { priority: highest + 1 });
        this.notify();
        return true;
    }

    subscribe(fn: () => void): () => void {
        if (typeof fn !== "function") return () => { };
        this.listeners.add(fn);
        return () => { this.listeners.delete(fn); };
    }

    // -- scheduling --------------------------------------------------------

    private find(id: string): JobRecord | undefined {
        for (const record of this.records) {
            if (record.job.id === id) return record;
        }
        return undefined;
    }

    private runningCount(): number {
        let n = 0;
        for (const { job } of this.records) if (job.state === "running") n++;
        return n;
    }

    /**
     * Start as many waiting jobs as there are free slots.
     *
     * Called after every state change, including from a failing job's `finally`
     * — a scan that throws must not leave the queue holding a slot forever.
     */
    private pump(): void {
        const limit = maxConcurrentJobs();
        for (const job of this.list()) {
            if (this.runningCount() >= limit) return;
            if (job.state !== "queued") continue;
            const record = this.find(job.id);
            if (record) void this.run(record);
        }
    }

    private estimate(backend: string, members: number): number | null {
        try {
            const client = this.clientFor(backend);
            const seconds = client?.estimateSeconds?.(members);
            return typeof seconds === "number" && isFinite(seconds) ? seconds : null;
        } catch {
            // An estimate is a courtesy. The scan itself reports the real
            // failure when it tries to build the same client.
            return null;
        }
    }

    /**
     * The client for one backend, built once and shared by every job on it.
     *
     * Keyed on the settings it was built from so that changing the rate limit or
     * the API key takes effect on the next job without anyone having to
     * remember to invalidate anything. Replacing it does not split the budget:
     * api/ratelimit.ts keys its limiters on the base URL, so the new client
     * picks up the window the old one was spending from — including whatever a
     * still-running job has spent out of it.
     */
    private clientFor(backend: string): LookupBackend {
        const opts = optionsFor(backend);
        let key: string;
        try {
            key = JSON.stringify(opts);
        } catch {
            key = String(opts);
        }
        const cached = this.clients.get(backend);
        if (cached && cached.key === key) return cached.client;
        const client = buildBackendClient(backend, opts);
        this.clients.set(backend, { key, client });
        if (cached) this.retire(backend, cached.client);
        return client;
    }

    /**
     * Let go of a client the queue has replaced.
     *
     * Dropping the reference is not enough on its own. The client holds the raw
     * upstream payloads it was answered with — an Okappiki body embeds a
     * Rotector verdict, so both backends' caches fall under Rotector's 24-hour
     * retention ceiling — and it keeps a sweep interval running to expire them.
     * A live `setInterval` is a garbage-collection root, so a merely
     * dereferenced client is never collected and neither the data nor the timer
     * ever goes away.
     *
     * It is only purged straight away when nothing is scanning on that backend,
     * though. `run` takes its client once and holds it for the length of the
     * scan, so clearing the cache underneath a running one would make it
     * re-request members it had already been answered about — spending somebody
     * else's budget to apply a settings change. Those wait in `retired` until
     * `reset`, which is the point at which nothing is running by construction.
     */
    private retire(backend: string, client: LookupBackend | undefined): void {
        if (!client) return;
        const busy = this.records.some(
            record => record.job.state === "running" && record.job.backend === backend
        );
        if (busy) {
            this.retired.add(client);
            return;
        }
        this.purgeClient(client);
    }

    private purgeClient(client: LookupBackend | undefined): void {
        try {
            client?.purgeCache?.();
        } catch {
            // A client with nothing to purge, or one already torn down, must
            // not stop the others being purged.
        }
    }

    // -- running -----------------------------------------------------------

    private async run(record: JobRecord): Promise<void> {
        record.seen = new Set<string>();
        record.controller = newController();
        // Captured before the first request: if the user purges the cache or
        // disables the plugin mid-scan, `reportStore.put` drops every later
        // write rather than refilling a cache somebody just cleared.
        record.epoch = reportStore.epoch;

        this.patch(record, {
            state: "running",
            startedAt: Date.now(),
            finishedAt: undefined,
            error: undefined,
            stopping: false,
            progress: { stage: "Starting", done: 0, total: record.job.ids.length },
        });
        this.notify();

        try {
            const client = this.clientFor(record.job.backend);
            if (typeof client?.scanMembers !== "function") {
                throw new Error(`The "${record.job.backend}" backend cannot scan members.`);
            }

            const reports = await client.scanMembers(record.job.ids, {
                signal: record.controller?.signal,
                onProgress: (p: ScanProgress) => this.onProgress(record, p),
                onPartial: (partial: MemberReport[]) => this.absorb(record, partial),
            });

            // `onPartial` is a courtesy a backend may not offer, and even the
            // Rotector client fills the last few unanswered ids in only after
            // its final publish. Sweeping the returned map is what makes the
            // job's counts match the store either way.
            if (reports && typeof (reports as any).values === "function") {
                this.absorb(record, Array.from((reports as any).values()));
            }

            const aborted = record.job.stopping || record.controller?.signal?.aborted === true;
            this.settle(record, aborted ? "stopped" : "done");
        } catch (err: any) {
            if (isAbortError(err) || record.job.stopping) this.settle(record, "stopped");
            else this.settle(record, "failed", describeError(err));
        } finally {
            record.controller = null;
            // In `finally` on purpose: one job failing must not strand the
            // queue, so the next waiting scan starts whatever happened here.
            try {
                this.pump();
            } catch {
                // A broken settings read must not leave the queue unable to run.
            }
        }
    }

    private onProgress(record: JobRecord, progress: ScanProgress): void {
        const stage = String(progress?.stage ?? "");
        const done = Math.max(0, Math.floor(num(progress?.done, 0, 0, 1e9)));
        const total = Math.max(0, Math.floor(num(progress?.total, record.job.ids.length, 0, 1e9)));
        this.patch(record, { progress: { stage, done, total } });
        this.notifySoon();
    }

    /**
     * File a batch of reports: into the store, and into this job's counters.
     *
     * The store is the only cache — the retention ceiling and the sweeper live
     * there, and a second copy here would be a second thing to expire. What the
     * job keeps is arithmetic: how many members have an answer, how many of
     * those are findings, and how many came back with an error.
     */
    private absorb(record: JobRecord, reports: MemberReport[] | undefined): void {
        if (!Array.isArray(reports) || !reports.length) return;

        const fresh: MemberReport[] = [];
        let found = 0;
        let findings = 0;
        let unanswered = 0;

        for (const report of reports) {
            if (!report || !isSnowflake(report.discordId)) continue;
            // Seen ids are skipped rather than re-filed: the final sweep over
            // the returned map passes every report a second time, and rewriting
            // ten thousand store entries to learn nothing new is work the
            // subscribers would have to render.
            if (record.seen.has(report.discordId)) continue;
            record.seen.add(report.discordId);
            fresh.push(report);
            found++;
            if (report.error) {
                // An error is not a clean result and is never counted as one.
                unanswered++;
                continue;
            }
            try {
                // The same bar the marks use: NO DETECTIONS and UNKNOWN are
                // answers, but they are not findings.
                if (reportVerdict(report) >= Verdict.INFO) findings++;
            } catch {
                /* a malformed report is not a finding */
            }
        }

        if (fresh.length) {
            try {
                reportStore.put(fresh, record.epoch);
            } catch {
                // The store refusing a write (a purge landing mid-batch) is not
                // a scan failure; the job's own counts still describe what it
                // reached.
            }
        }

        if (found) {
            this.patch(record, {
                found: record.job.found + found,
                findings: record.job.findings + findings,
                unanswered: record.job.unanswered + unanswered,
            });
            this.notifySoon();
        }
    }

    private settle(record: JobRecord, state: JobState, error?: string): void {
        if (!jobIsActive(record.job)) return;
        const stage = state === "done"
            ? "Scan complete"
            : state === "stopped" ? "Scan stopped" : "Scan failed";
        this.patch(record, {
            state,
            error,
            stopping: false,
            finishedAt: Date.now(),
            progress: {
                stage,
                done: record.job.progress?.done ?? record.job.found,
                total: record.job.progress?.total ?? record.job.ids.length,
            },
        });
        this.notify();
    }

    // -- change plumbing ---------------------------------------------------

    /**
     * Replace the public job object rather than mutating it.
     *
     * `list()` hands the same array out until something changes, and each job is
     * a new object only when that job changed, so a React list can compare by
     * identity instead of re-rendering every row on every progress tick.
     */
    private patch(record: JobRecord, changes: Partial<Job>): void {
        record.job = Object.assign({}, record.job, changes);
        this.ordered = null;
    }

    /** One notification for everything that happened this tick. */
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
                // A broken subscriber must not stop the others being told, and
                // must certainly not settle a job.
            }
        }
    }
}

export const jobQueue = new JobQueue();

// --------------------------------------------------------------------------
// React plumbing
// --------------------------------------------------------------------------

/**
 * The queue, re-rendered when it changes.
 *
 * Built from `useState` + `useEffect` rather than `useSyncExternalStore` for the
 * same reason store.ts is: React here is whichever version Discord shipped, and
 * a hook that is missing is a thrown error inside a modal rather than a degraded
 * render.
 */
export function useJobs(): Job[] {
    const [jobs, setJobs] = useState<Job[]>(() => jobQueue.list());

    useEffect(() => {
        // The queue may have moved between render and effect.
        setJobs(jobQueue.list());
        return jobQueue.subscribe(() => setJobs(jobQueue.list()));
    }, []);

    return jobs;
}

/** One job, re-rendered when it changes. `undefined` once it is removed. */
export function useJob(id: string | undefined): Job | undefined {
    const [job, setJob] = useState<Job | undefined>(() => (id ? jobQueue.get(id) : undefined));

    useEffect(() => {
        if (!id) {
            setJob(undefined);
            return;
        }
        setJob(jobQueue.get(id));
        return jobQueue.subscribe(() => setJob(jobQueue.get(id)));
    }, [id]);

    return id ? job : undefined;
}
