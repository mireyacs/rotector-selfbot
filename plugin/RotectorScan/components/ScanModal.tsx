/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The scan modal: the terminal app's main screen, as one Discord modal.
 *
 * The whole flow lives here on purpose, in the order rsb/tui/app.py puts it:
 * pick a source, see honestly what that source can and cannot reach, start the
 * run, watch the ledger fill, read the summary. Splitting it across screens
 * would let a moderator start a scan without having read the coverage sentence,
 * and the coverage sentence is the most important thing this plugin says.
 *
 * Two behaviours are copied from the TUI rather than reinvented, because they
 * are arguments rather than preferences:
 *
 *  - The results table lists *findings only* by default (app.py:121-137). In a
 *    real server almost every member has nothing against them and listing them
 *    all buries the few that matter.
 *  - Anything hidden is still counted and still announced (app.py:2503-2519,
 *    and the comment at app.py:2356, "still counted in the summary, just not
 *    listed"). The filter line therefore always prints, and always prints the
 *    hidden count, even when it is zero.
 *
 * One more thing lives here because it can only live on a surface: the consent
 * step for the gateway's account-token path. gateway/tokens.ts holds the gate
 * and states the requirement it cannot meet on its own — that path stays off
 * until the user switches it on *and* the UI shows `CONSENT_NOTICE` before the
 * first connection. Automating a user account is against Discord's Terms of
 * Service and can cost somebody their account, so the pre-flight prints the
 * notice verbatim with the button that would connect inside it, and no other
 * path in this file opens a "client" gateway.
 *
 * The run itself belongs to features/jobs.ts rather than to this component, and
 * that is not a refactor for tidiness. The rate limiter is shared per backend
 * because Rotector's terms forbid working around the window; a modal that
 * called `scanMembers` itself would be a second scheduler over one budget, so
 * a scan started here and a scan already queued would each believe they had the
 * whole window. Everything below drives `jobQueue` and reads results back out
 * of `reportStore`, which is the only home a finding gets.
 */

import ErrorBoundary from "@components/ErrorBoundary";
import { copyWithToast } from "@utils/discord";
import { Modal, openModal, ScrollerThin, useEffect, useMemo, useRef, useState, UserStore } from "@webpack/common";

import { backendAttribution, backendFrom, backendLabel, backendWarning } from "../api/backend";
import { APPEAL_LINKS, PARTIES, recommendationLabel, triage, verdictFloor as recommendationTier } from "../api/triage";
import { isStale, MemberReport, reportVerdict, sortedAccounts, staleNote, staleTag, worstAccount } from "../api/types";
import { ALL_VERDICTS, categoryName, flagName, Verdict, verdictLabel, verdictMeaning, verdictName } from "../api/verdict";
import { historyEnabled, scanHistory } from "../features/history";
import { jobElapsed, jobQueue, STATE_LABEL, useJob, useJobs } from "../features/jobs";
import type { Job } from "../features/jobs";
import { collect, Collected, currentGuildId, gatewayAvailability, guildRoles, hydrateMembers, listSources, MemberSource, warmGuild } from "../members";
import { policyFromSettings, retainBeyondTerms, retentionHours, retentionNotice, settings, verdictFloor as configuredFloor } from "../settings";
import { reportStore } from "../store";
import { openExportMenu } from "./ExportMenu";
import { openHistory } from "./HistoryModal";
import { openModerationDialog } from "./ModerationDialog";
import { Body, Btn, cl, Label, Meter, Micro, Node, VerdictLine } from "./primitives";
import { openReport } from "./ReportModal";
import { VibeControls } from "./VibePlayer";

/**
 * How many rows the ledger draws at once.
 *
 * The TUI pages at 250 for the same reason (app.py:298-300): a five-figure
 * member list redrawn in one go makes the UI crawl. Rows past the cap are not
 * hidden by the filter — they are simply not drawn yet — so they are announced
 * separately from the filter's hidden count, which means something different.
 */
const PAGE_SIZE = 250;

/**
 * How many members "Copy findings" writes out in full.
 *
 * A clipboard entry is something a person pastes into a ticket and reads; past
 * a couple of thousand blocks it stops being that, so the rest is announced as
 * a count rather than silently dropped.
 */
const MAX_COPY_MEMBERS = 2000;

/**
 * How often the ledger re-reads the store, so an expiry is noticed without one.
 *
 * The 23-hour ceiling itself is not applied here and deliberately never was:
 * `reportStore.get` re-checks it on every read and drops what is past it, so
 * this window has no copy of a finding to keep past its deadline. This timer
 * only makes sure the drop is *seen* by a window left open overnight.
 */
const RETENTION_RECHECK_MS = 60_000;

// --------------------------------------------------------------------------
// small helpers, all of which must survive a store or a sibling module that is
// not ready yet — this file is loaded without type checking, so a missing
// export is `undefined` at runtime rather than a build error
// --------------------------------------------------------------------------

function attempt<T>(fn: () => T, fallback: T): T {
    try {
        const value = fn();
        return value === undefined ? fallback : value;
    } catch {
        return fallback;
    }
}

/**
 * The loader hands every module a CommonJS `require`; the declaration only keeps
 * the identifier from looking undeclared to a reader.
 */
declare const require: (specifier: string) => any;

let noticeCache: string | undefined;

/**
 * `CONSENT_NOTICE`, the sentence gateway/tokens.ts requires this UI to show
 * before the first connection on the account's own token.
 *
 * Required rather than imported, for the reason members.ts gives for the
 * gateway itself: a module that fails to evaluate throws out of `require`, and
 * a scan modal that would not open because the token module was unhappy is a
 * worse failure than a gateway scrape that is not offered. Returning "" is
 * therefore a real answer, and the caller treats it as one — no notice means no
 * connection, because the notice *is* the consent step and nothing else in this
 * plugin may stand in for it. Nothing here ever touches a token.
 */
function consentNotice(): string {
    if (noticeCache === undefined) {
        noticeCache = attempt(() => String(require("../gateway/tokens")?.CONSENT_NOTICE ?? ""), "");
    }
    return noticeCache;
}

/**
 * Whether a guild collect may open the gateway without being asked first.
 *
 * A bot connection may: a bot application with the Server Members Intent asking
 * Discord for the roster of a server it is in is what bot applications are for,
 * there is no Terms of Service question, and switching the gateway on is the
 * whole of the consent it needs.
 *
 * The account's own token may not. That path is automation of a user account,
 * which Discord's Terms prohibit and which can lose somebody their account, so
 * it waits for the button beside `CONSENT_NOTICE` in the pre-flight. This is the
 * difference between a setting the user chose once and a connection they are
 * about to make now.
 */
function gatewayAutoAllowed(): boolean {
    const state = attempt(() => gatewayAvailability(), { ready: false, mode: "off", problem: "" });
    return Boolean(state?.ready && state.mode === "bot");
}

/** Read one plugin setting. `settings.store` throws before registration. */
function option<T>(key: string, fallback: T): T {
    return attempt(() => {
        const store = settings?.store as Record<string, unknown> | undefined;
        const value = store?.[key];
        return (value === undefined ? fallback : value) as T;
    }, fallback);
}

/** The lowest verdict that counts as a finding. Defaults to INFO, as the TUI does. */
function findingFloor(): Verdict {
    return attempt(() => {
        const floor = typeof configuredFloor === "function" ? configuredFloor() : undefined;
        return typeof floor === "number" ? (floor as Verdict) : Verdict.INFO;
    }, Verdict.INFO);
}

/** `95` -> `"1m 35s"`. A transliteration of format_duration in rsb/eta.py. */
function formatDuration(seconds: number | null | undefined): string {
    if (seconds == null || !isFinite(seconds)) return "?";
    let s = Math.max(0, Number(seconds));
    if (s < 1) return "<1s";
    if (s < 60) return `${Math.round(s)}s`;
    s = Math.round(s);
    const minutes = Math.floor(s / 60);
    const secs = s % 60;
    if (minutes < 60) return `${minutes}m ${String(secs).padStart(2, "0")}s`;
    const hours = Math.floor(minutes / 60);
    return `${hours}h ${String(minutes % 60).padStart(2, "0")}m`;
}

function count(n: number): string {
    return attempt(() => n.toLocaleString(), String(n));
}

// --------------------------------------------------------------------------
// how long findings are being kept, in this window's own words
//
// Three sentences in this file used to assert the 24-hour rule as a fact about
// the run. It is a fact about Rotector's terms; whether the run followed it is
// what `retainBeyondTerms` decides, and a window that told the reader results
// were dropped within a day while the caches were told to hold them for a week
// would be the plugin misreporting itself. Each helper below returns exactly
// today's wording while the setting is off.
// --------------------------------------------------------------------------

/** The configured window in hours, floored at one and only meaningful when lifted. */
function keptHours(): number {
    return Math.max(1, Math.round(attempt(() => retentionHours(), 168)));
}

/** "at most 24 hours", or "168 hours by choice" once the ceiling has been lifted. */
function keptFor(): string {
    if (!attempt(() => retainBeyondTerms(), false)) return "at most 24 hours";
    const hours = keptHours();
    return hours >= 48 ? `${Math.round(hours / 24)} days by choice` : `${hours} hours by choice`;
}

/** "23-hour", or the lifted window, for "under the same N ceiling". */
function ceilingLabel(): string {
    return attempt(() => retainBeyondTerms(), false) ? `${keptHours()}-hour` : "23-hour";
}

/**
 * The footer's retention sentence, for whichever ceiling is in force.
 *
 * `retentionNotice()` is the shared wording and it is used for the lifted case
 * verbatim, so the status panel, the exports and this footer cannot describe
 * that choice differently. It is *not* used for the default case, because its
 * default sentence says "held in memory only" and this window's does not: a
 * finished scan is also written to a database on this computer, and that is the
 * one clause a footer here may not lose. Both are under the one ceiling, which
 * is the part the reader actually asked about.
 */
function retentionLine(): string {
    const lifted = attempt(() => retainBeyondTerms(), false);
    const head = lifted
        ? attempt(
            () => retentionNotice(),
            `Findings are being kept for ${keptHours()} hours by choice, past the 24-hour window ` +
            "Rotector's Terms of Use allow. Anything older than 24 hours is labelled stale with " +
            "its age: flag types change and appeals succeed, so re-check before acting on one."
        )
        : "Findings are dropped within 24 hours — Rotector's terms forbid keeping their data " +
          "longer, and flag statuses change.";
    const tail = lifted
        ? " This ledger reads from memory; a finished scan is also written to this plugin's own " +
          `database on this computer, and that record is kept to the same ${ceilingLabel()} ceiling, ` +
          "measured from when the scan started."
        : " This ledger reads from memory; a finished scan is also written to this plugin's own " +
          "database on this computer, and that record is deleted 23 hours after the scan started.";
    return head + tail;
}

/**
 * The appeal routes a backend's findings owe the people they name.
 *
 * Rotector's terms require that their data carries a link to rotector.com so
 * the affected person can appeal. An Okappiki scan is three services' findings
 * in one envelope, and this ledger lists all three, so naming only Rotector's
 * door would accuse somebody on two other services' say-so and then send them
 * to the wrong place.
 */
function appealSentence(backend: string): string {
    const parties = backend === "okappiki" ? PARTIES : (["rotector"] as readonly string[]);
    const urls: string[] = [];
    for (const party of parties) {
        const url = attempt(() => APPEAL_LINKS[party], undefined as unknown as string);
        if (url && !urls.includes(url)) urls.push(url.replace(/^https?:\/\//, ""));
    }
    // Rotector's route is required whatever went wrong reading the table.
    if (!urls.length) urls.push("rotector.com");
    return `Anyone listed here can appeal at ${urls.join(", ")}`;
}

/** Which service this window is about to ask, or is reading the answers of. */
function currentBackend(): string {
    return attempt(() => backendFrom(settings?.store as any), "rotector");
}

/**
 * What `Collected.total` is actually counting, in words.
 *
 * Only the guild source denominates against `GuildMemberCountStore` — the real
 * population, of which this client holds a fraction. Every other source comes
 * back complete, and `members.ts` sets `total = ids.length` for them. Printing
 * "N of N in the server" for a role or a friends list would therefore claim
 * full coverage of a server that was barely sampled, which is the exact wrong
 * denominator this plugin exists to avoid. When the two numbers describe
 * different things they get different words.
 */
function coverage(source: MemberSource | null, collected: Collected | null): string {
    if (!collected?.total) return "";
    const total = count(collected.total);
    switch (source?.kind) {
        case "guild":
            // The only case where `known` and `total` can genuinely differ —
            // and the only one with two paths behind it. A gateway scrape and a
            // read of this client's cache are not the same claim, so they do
            // not get the same sentence: one says what was enumerated, the
            // other says what happened to be loaded.
            return collected.via === "gateway"
                ? ` — ${count(collected.known)} of ${total} members found by the gateway`
                : ` — ${count(collected.known)} of ${total} members loaded in this client`;
        case "role":
            return ` — the role's full roster of ${total}`;
        case "friends":
            return ` — all ${total} friends`;
        case "requests":
            return ` — all ${total} incoming requests`;
        case "gdm":
        case "channel":
            return ` — all ${total} recipients`;
        default:
            return "";
    }
}

/** The member's own name, or their id when the client has never seen them. */
function memberName(id: string): string {
    return attempt(() => {
        const user = (UserStore as any)?.getUser?.(id);
        return user?.globalName || user?.username || id;
    }, id);
}

function memberHandle(id: string): string {
    return attempt(() => {
        const user = (UserStore as any)?.getUser?.(id);
        return user?.username ? `@${user.username}` : `id ${id}`;
    }, `id ${id}`);
}

/**
 * The verdict a report reads as in the ledger.
 *
 * A report that errored has no verdict at all, and calling that UNKNOWN would
 * be the flattening triage.ts refuses to do — so the ledger keeps it visible
 * under the findings filter and the summary counts it as unanswered.
 */
function rowVerdict(report: MemberReport): Verdict {
    return attempt(() => reportVerdict(report), Verdict.UNKNOWN);
}

function isUnanswered(report: MemberReport): boolean {
    return Boolean(report?.error);
}

// --------------------------------------------------------------------------
// the plain-text summary the footer copies out
// --------------------------------------------------------------------------

/** One member, in the shape rsb/tui/app.py's `_member_summary` produces. */
function memberSummary(report: MemberReport): string {
    const id = report.discordId;
    const lines: string[] = [
        `${memberName(id)} (${memberHandle(id)})`,
        `Discord ID: ${id}`,
        // The meaning travels with the word, always. A pasted bare
        // "NO DETECTIONS" reads as a clean result, and presenting an unflagged
        // account that way is the one thing Rotector's terms forbid outright —
        // the screen never does it, so the clipboard must not either.
        `Verdict: ${verdictLabel(rowVerdict(report))} - ${verdictMeaning(rowVerdict(report))}`,
    ];
    // The age travels with the verdict for the same reason the meaning does: a
    // pasted block that reads as current, from an answer three days old, is the
    // misreading the whole staleness feature exists to prevent.
    const stale = attempt(() => staleNote(report), "");
    if (stale) lines.push(stale);
    // A report can carry accounts *and* an error: the Discord hop answered and
    // the Roblox flag hop did not. Saying so before the accounts is the whole
    // point — what follows is not the complete picture, and a pasted block that
    // read as complete would be the one misreading this plugin must not cause.
    const accounts = attempt(() => sortedAccounts(report), [] as ReturnType<typeof sortedAccounts>);
    if (report.error) {
        lines.push(accounts.length
            ? `Lookup incomplete: ${report.error} - what follows may not be everything.`
            : `Lookup failed: ${report.error}`);
    }

    // Worst first, by verdict then confidence. Sorting on the raw flag type
    // would be sorting on an id: type 8 (Redacted) and type 6 (Past Offender)
    // are numerically above type 2 (Confirmed) and mean far less, so the block
    // would lead with the type Rotector documents as "not an inappropriate
    // user" and bury the actionable one.
    for (const account of accounts) {
        const category = account.category ? ` / ${categoryName(account.category)}` : "";
        lines.push(`  Roblox ${account.username} (${account.userId}) - ${flagName(account.flagType)}${category}`);
        lines.push(`    https://www.roblox.com/users/${account.userId}/profile`);
        for (const [name, detail] of Object.entries(account.reasons ?? {})) {
            const message = String((detail as any)?.message ?? "").replace(/\n/g, " / ");
            lines.push(`    ${name}: ${message}`);
        }
    }
    if (report.servers?.length) {
        lines.push(`Tracked servers: ${report.servers.slice(0, 8).map(s => s.serverName).join(", ")}`);
    }
    return lines.join("\n");
}

// --------------------------------------------------------------------------
// writing a finished scan down
// --------------------------------------------------------------------------

/**
 * Jobs already written to the history this session.
 *
 * A finished job is offered to `save()` from two places — this window's own
 * effect, and the queue subscriber index.tsx keeps alive so a scan that finishes
 * with no window open is still recorded — and React re-runs effects. The record
 * id is derived from the job id as well, so even if both got through, the second
 * write would overwrite the first rather than produce a second copy of one scan.
 * This set is the cheap half of that; the deterministic id is the half that is
 * actually load-bearing.
 */
const savedJobs = new Set<string>();

/**
 * The coverage note for a job, remembered by whoever started it.
 *
 * `Collected.note` is the one sentence that says what a list of members was and
 * was not, and it is the property of the pre-flight rather than of the job — the
 * queue holds ids, counts and timings, and deliberately holds nothing that came
 * out of a lookup. A stored record without it would be a coverage figure with
 * its caveat stripped off, which is the figure lying, so the note is parked here
 * when the scan is enqueued and picked up again when it is written down.
 */
const scanNotes = new Map<string, string>();

/**
 * Write one finished job into the scan history.
 *
 * Everything the job knows plus every report the store still holds for its ids.
 * The clean rows go over too and are dropped by `save()` itself, which counts
 * them into the record's `omitted` — a record that quietly held only the
 * findings without saying how many members carried none would read as a much
 * worse server than the one that was scanned.
 *
 * Never throws and never rejects. A scan that finished must not fail because a
 * database refused a write.
 */
export function recordJobInHistory(job: Job | undefined): void {
    try {
        if (!job || !job.id) return;
        // Only a run that reached an end. A failed job produced no answers worth
        // keeping, and a running one is not finished being wrong yet.
        if (job.state !== "done" && job.state !== "stopped") return;
        if (savedJobs.has(job.id)) return;
        if (!attempt(() => historyEnabled(), true)) return;
        savedJobs.add(job.id);

        const reports: MemberReport[] = [];
        for (const id of job.ids ?? []) {
            const held = attempt(() => reportStore.get(id), undefined);
            if (held) reports.push(held);
        }

        const note = scanNotes.get(job.id);
        scanNotes.delete(job.id);

        void scanHistory.save({
            // Derived from the job id rather than minted fresh: two writes of one
            // scan have to land on one record.
            id: `job-${job.id}`,
            // `startedAt` is what the 23-hour ceiling is measured from, so it has
            // to be the moment the questions were actually asked. A job that was
            // stopped in the queue before it ever ran falls back to when it was
            // queued, which is earlier and therefore shortens its life rather
            // than extending it.
            startedAt: job.startedAt ?? job.queuedAt,
            finishedAt: job.finishedAt,
            backend: job.backend,
            sourceKind: job.source?.kind ?? "",
            sourceLabel: job.label ?? job.source?.label ?? "members",
            guildId: job.source?.guildId,
            scanned: job.found,
            findings: job.findings,
            unanswered: job.unanswered,
            coverageNote: note,
            reports,
        });
    } catch (error) {
        // The history is a convenience; the scan is the point.
        console.warn("[RotectorScan] could not write a scan to the history", error);
    }
}

/**
 * Offer every finished job in the queue to the history.
 *
 * Called from index.tsx's queue subscriber, because a scan outlives the window
 * that started it: the common case is a ten-thousand-member scan finishing
 * half an hour after somebody closed the modal, and a history that only recorded
 * the scans somebody happened to be watching would be a history of nothing.
 */
export function recordFinishedJobs(): void {
    for (const job of attempt(() => jobQueue.list() ?? [], [] as Job[])) {
        recordJobInHistory(job);
    }
}

/**
 * Forget what has been written, for a plugin that is being switched off.
 *
 * `jobQueue.reset()` throws the jobs away, so the ids in here would otherwise
 * refer to nothing for as long as the client stayed open — and if the plugin is
 * enabled again and a job id repeats, a real scan would be silently skipped.
 * This clears the bookkeeping only; the records themselves are on disk, under
 * their own ceiling, and are not this function's to delete.
 */
export function resetHistoryRecorder(): void {
    savedJobs.clear();
    scanNotes.clear();
}

// --------------------------------------------------------------------------
// the modal
// --------------------------------------------------------------------------

export function openScan(preset?: MemberSource): void {
    openModal(props => <ScanModal modalProps={props} preset={preset} />);
}

/**
 * The same window, opened on the queue.
 *
 * A scan outlives the modal that started it — the queue owns the run — so there
 * has to be a way back to one that is already going, and it is the only surface
 * on which a scan can be stopped.
 */
export function openScanQueue(): void {
    openModal(props => <ScanModal modalProps={props} startOn="queue" />);
}

type Phase = "pick" | "preflight" | "run" | "queue";

function ScanModal({ modalProps, preset, startOn }: {
    modalProps: any;
    preset?: MemberSource;
    startOn?: Phase;
}) {
    const [phase, setPhase] = useState<Phase>(startOn ?? (preset ? "preflight" : "pick"));
    const [source, setSource] = useState<MemberSource | undefined>(preset);
    const [collected, setCollected] = useState<Collected | undefined>(undefined);
    const [collecting, setCollecting] = useState(false);
    const [warming, setWarming] = useState(false);
    const [roleOpen, setRoleOpen] = useState<string | undefined>(undefined);
    const [error, setError] = useState<string | undefined>(undefined);

    const [now, setNow] = useState(0);

    const [findingsOnly, setFindingsOnly] = useState(true);
    const [limit, setLimit] = useState(PAGE_SIZE);
    const [version, setVersion] = useState(0);
    const [selected, setSelected] = useState<Set<string>>(new Set());

    /**
     * The job this window is watching, held as an id and never as a Job.
     *
     * A `Job` is a snapshot: the queue replaces the object on every progress
     * tick so a React list can compare by identity, so a Job kept in a variable
     * stops being true almost immediately. The id is the handle.
     */
    const [jobId, setJobId] = useState<string | undefined>(undefined);
    const job = useJob(jobId);
    const jobs = useJobs();

    /**
     * An existing job for the same source and backend, waiting on an answer.
     *
     * `onRescan: "ask"` cannot be answered from inside the queue — jobs.ts
     * takes the conservative half of the question on its own — so the choice is
     * offered here and the answer is passed back explicitly.
     */
    const [duplicate, setDuplicate] = useState<Job | undefined>(undefined);

    /** progress of a gateway scrape, which is minutes of work before a scan starts */
    const [gather, setGather] = useState<{ found: number; total: number; stage: string; } | undefined>(undefined);
    const gatherRef = useRef<AbortController | undefined>(undefined);

    // The scan holds no findings of its own.
    //
    // Rotector's terms forbid keeping their responses past 24 hours, and a
    // Discord client stays open for days — so a second copy of the results
    // living in this component would outlive the ceiling store.ts enforces,
    // would survive "Purge cached findings" and `reportStore.stop()`, and would
    // go on displaying and copying findings the footer promises were dropped.
    //
    // What is kept instead is the job's own list of ids and its own counts of
    // what it learned. The reports themselves are read back out of
    // `reportStore`, which sweeps on a timer, re-checks the ceiling on every
    // read, and is emptied by both purge and stop. A row whose report the store
    // no longer holds simply stops being drawn — and the job's counts, which
    // are numbers rather than findings, still say what was answered.

    const aliveRef = useRef(true);

    const guildId = useMemo(() => attempt(() => currentGuildId(), undefined), []);
    const sources = useMemo(() => {
        const found = attempt(() => listSources(guildId) ?? [], [] as MemberSource[]);
        // "Scan a role" is the only complete enumeration Discord will give a
        // client, so it must always be offered when there is a server to ask
        // about — even if listSources did not think to name it.
        if (guildId && !found.some(s => s?.kind === "role")) {
            return [...found, {
                kind: "role" as const,
                guildId,
                label: "A role in this server",
                hint: "The complete list of everyone holding one role.",
            }];
        }
        return found;
    }, [guildId]);

    // Closing the window no longer stops the scan, and that is the point of the
    // queue: a ten-thousand-member scan takes half an hour and nobody should
    // have to keep a modal open for it. The queue is where a running scan is
    // stopped, and it is reachable from the server menu and `/rotector queue`.
    useEffect(() => {
        aliveRef.current = true;
        return () => {
            aliveRef.current = false;
            // The one thing that does not survive the window: a gateway scrape
            // that has not produced a list yet. It belongs to this pre-flight
            // rather than to the queue, it holds a socket open on somebody's
            // account, and there would be nothing left to stop it from.
            attempt(() => gatherRef.current?.abort(), undefined);
        };
    }, []);

    // One clock, ticking only while a scan runs, so a slow stage never reads as
    // a hang (the TUI shows the same elapsed counter for the same reason).
    const anythingRunning = jobs.some(entry => entry?.state === "running");
    useEffect(() => {
        if (!anythingRunning) return;
        const timer = setInterval(() => setNow(Date.now()), 1000);
        return () => clearInterval(timer);
    }, [anythingRunning]);

    // The ledger reads through to the store, so it has to be told when the
    // store changes: a sweep that expires a report, "Purge cached findings",
    // or `reportStore.stop()` must all empty this table rather than leaving a
    // stale copy on screen.
    useEffect(() => {
        let unsubscribe: (() => void) | undefined;
        try {
            unsubscribe = reportStore.subscribe(() => {
                if (aliveRef.current) setVersion(v => v + 1);
            });
        } catch {
            // A store that cannot be subscribed to is still read on every
            // render, and the timer below keeps those renders happening.
        }
        return () => {
            attempt(() => unsubscribe?.(), undefined);
        };
    }, []);

    // Write the scan down the moment it ends, honouring `historyEnabled`.
    //
    // Here as well as in index.tsx's queue subscriber, and deliberately: this
    // one runs while the results are freshest and is the only caller that knows
    // the coverage note, and that one catches the scans that finish after the
    // window has been closed. Both land on one record — the id is derived from
    // the job id — so the pair is a redundancy rather than a duplication.
    useEffect(() => {
        if (!job) return;
        if (job.state !== "done" && job.state !== "stopped") return;
        attempt(() => recordJobInHistory(job), undefined);
    }, [job?.id, job?.state]);

    // ...and re-read on a timer regardless, because the retention ceiling is a
    // deadline rather than an event. A window left open overnight must drop its
    // findings whether or not anything else in the plugin is still running.
    useEffect(() => {
        const timer = setInterval(() => {
            if (!aliveRef.current) return;
            setVersion(v => v + 1);
        }, RETENTION_RECHECK_MS);
        return () => clearInterval(timer);
    }, []);

    async function collectFor(next: MemberSource, options?: { warm?: boolean; gateway?: boolean; }) {
        setSource(next);
        setPhase("preflight");
        setError(undefined);
        setCollecting(true);
        setDuplicate(undefined);
        setGather(undefined);
        if (options?.warm) setWarming(true);

        // A guild source uses the gateway whenever the user has switched it on,
        // because that is what switching it on means: a server scan that read
        // the sidebar cache while a working bot connection sat unused would
        // report a sample as though the setting had done something. It is
        // minutes of work, so the progress is live and "Stop reading" is beside
        // it.
        //
        // The account's own token is the exception, and it is not a preference.
        // gateway/tokens.ts states the requirement its own gate cannot enforce:
        // that path stays off until the user switches it on *and* the UI shows
        // CONSENT_NOTICE before the first connection. So a "client" gateway is
        // never opened on the way into this screen — the pre-flight prints the
        // notice with the button that starts it, and that button is the only
        // caller that passes `gateway: true`.
        const useGateway = options?.gateway === undefined
            ? gatewayAutoAllowed()
            : options.gateway !== false;

        const controller = attempt(() => new AbortController(), undefined as any);
        gatherRef.current = controller;

        try {
            if (options?.warm && next.guildId) {
                await warmGuild(next.guildId);
            }
            const max = Number(option("maxMembers", 0)) || 0;
            const result = await collect(next, {
                skipBots: Boolean(option("skipBots", true)),
                max: max > 0 ? max : undefined,
                useGateway,
                signal: controller?.signal,
                onProgress: p => {
                    if (aliveRef.current) {
                        setGather({
                            found: p?.found ?? 0,
                            total: p?.total ?? 0,
                            stage: String(p?.stage ?? ""),
                        });
                    }
                },
            });
            if (!aliveRef.current) return;
            setCollected(result);

            // A role roster arrives as bare ids — the REST endpoint hands back
            // snowflakes and nothing else — so without this the ledger prints
            // rows of digits for exactly the source the picker pushes people
            // towards. This hydrates a list already held; it cannot discover
            // anyone, and it no-ops for ids UserStore already knows.
            if (next.guildId && result.ids.length) {
                attempt(() => hydrateMembers(next.guildId!, result.ids), undefined);
            }
        } catch (err: any) {
            if (!aliveRef.current) return;
            setCollected(undefined);
            setError(String(err?.message ?? err));
        } finally {
            if (gatherRef.current === controller) gatherRef.current = undefined;
            if (aliveRef.current) {
                setCollecting(false);
                setWarming(false);
                setGather(undefined);
            }
        }
    }

    // Collect straight away when the modal was opened on a preset source (the
    // context-menu entries and the /rotector command both do that).
    useEffect(() => {
        if (preset) void collectFor(preset);
    }, []);

    /**
     * Hand the ids to the queue.
     *
     * Nothing is scanned here. `jobQueue.enqueue` owns the run, the abort, the
     * shared per-backend limiter and the writes into `reportStore`; this
     * function decides what the user meant when the same source is already
     * queued and then gets out of the way.
     */
    function startScan(rescan?: "reuse" | "rescan") {
        const ids = collected?.ids ?? [];
        if (!ids.length) return;

        const backend = currentBackend();

        // `onRescan: "ask"` is a question only a surface can ask, so it is
        // asked here rather than guessed at inside the queue.
        if (!rescan && String(option("onRescan", "ask")) === "ask") {
            const existing = attempt(() => jobQueue.duplicateOf(source!, backend, false), undefined);
            if (existing) {
                setDuplicate(existing);
                return;
            }
        }

        setDuplicate(undefined);
        setError(undefined);
        setLimit(PAGE_SIZE);
        setSelected(new Set());
        setNow(Date.now());

        try {
            const started = jobQueue.enqueue(
                source!,
                ids,
                `${source?.label ?? "members"} \u00b7 ${backendLabel(backend)}`,
                rescan ? { rescan } : undefined
            );
            // The coverage sentence belongs to this pre-flight and the queue
            // never sees it, so it is handed to the recorder here — before the
            // scan can possibly finish — rather than read off a job that has no
            // field for it.
            if (started?.id && collected?.note) scanNotes.set(started.id, collected.note);
            setJobId(started?.id);
            setPhase("run");
        } catch (err: any) {
            setError(String(err?.message ?? err));
        }
    }

    /** Show the results of a job that is already in the queue. */
    function watchJob(id: string) {
        setJobId(id);
        setLimit(PAGE_SIZE);
        setSelected(new Set());
        setPhase("run");
    }

    // ---- derived reading of the results -----------------------------------

    const showTriage = Boolean(option("showTriage", true));
    const floor = findingFloor();

    /**
     * Which service answered for the results on screen.
     *
     * Read off the job rather than off settings, because a finished scan is
     * still showing the answers of whichever backend ran it — changing the
     * setting afterwards must not restate an Okappiki scan's attribution as
     * Rotector's, or the other way round.
     */
    const scanBackend = job?.backend ?? currentBackend();

    /**
     * The ids this window is reporting on: the job's own list.
     *
     * The queue owns them, so they survive the modal being closed and reopened
     * and they are the same list every other surface reads through.
     */
    const jobIds = job?.ids ?? [];

    /**
     * The scan's results, read back out of the store on every version bump.
     *
     * `reportStore.get` is the single gate: it re-checks Rotector's 23h ceiling
     * on every read and drops what is past it, and the store's sweeper applies
     * the configured TTL under that. So a report that has aged out, been purged
     * from the settings panel, or been dropped by `reportStore.stop()` is not
     * here to be listed, counted or copied — which is what the footer promises.
     *
     * A member the scan has not reached yet is simply absent, which is why the
     * job's own counts are printed alongside the table rather than derived from
     * it: `job.unanswered` outlives the short-lived error entries the store
     * keeps, so an outage goes on being reported after its rows have gone.
     */
    const all = useMemo(() => {
        const rows: MemberReport[] = [];
        for (const id of jobIds) {
            const held = attempt(() => reportStore.get(id), undefined);
            if (held) rows.push(held);
        }
        return rows;
    }, [version, job?.id, job?.found, job?.state, jobIds.length]);

    /** How many members this scan has an answer for, findings or not. */
    const scanned = job?.found ?? 0;
    /** Results the store has since dropped — expired, purged or stopped. */
    const dropped = Math.max(0, scanned - all.length);

    const tally = useMemo(() => {
        const counts: Record<number, number> = {};
        for (const verdict of ALL_VERDICTS) counts[verdict] = 0;
        let unanswered = 0;
        for (const report of all) {
            counts[rowVerdict(report)] = (counts[rowVerdict(report)] ?? 0) + 1;
            if (isUnanswered(report)) unanswered++;
        }
        // The job counted every lookup that did not answer, including the ones
        // whose rows the store has since expired. That number is ours rather
        // than Rotector's — a count of our own failures — so it may outlive
        // them, and it has to: an outage that stopped being visible would read
        // as a scan that found nothing.
        return { counts, unanswered: Math.max(unanswered, job?.unanswered ?? 0) };
    }, [all, job?.unanswered]);

    const listed = useMemo(() => {
        const rows = findingsOnly
            // An unanswered lookup stays listed even under the findings filter:
            // a database that did not answer is a fact about the evidence, and
            // dropping it here would let an outage read as a clean scan.
            ? all.filter(report => rowVerdict(report) >= floor || isUnanswered(report))
            : all;
        return [...rows].sort((a, b) => {
            const delta = rowVerdict(b) - rowVerdict(a);
            if (delta !== 0) return delta;
            return memberName(a.discordId).toLowerCase().localeCompare(memberName(b.discordId).toLowerCase());
        });
    }, [all, findingsOnly, floor]);

    const hidden = all.length - listed.length;
    const page = listed.slice(0, limit);

    /**
     * How many of the results on screen are past the terms' 24-hour window.
     *
     * Counted over everything held rather than over the listed rows, because
     * the summary is a statement about the scan and not about the filter. It is
     * zero in the ordinary case: nothing survives 24 hours unless the operator
     * lifted the ceiling, or the window has been left open that long.
     */
    const staleCount = useMemo(
        () => all.reduce((total, report) => total + (attempt(() => isStale(report), false) ? 1 : 0), 0),
        [all, version]
    );

    function copyFindings() {
        const sourceLabel = source?.label ?? "members";
        const head = [
            `RotectorScan - ${sourceLabel}`,
            `backend: ${backendLabel(scanBackend)}`,
            `${count(scanned)} scanned, ${count(listed.length)} listed`,
            `filter: ${findingsOnly ? "findings" : "all"} (${count(Math.max(0, hidden))} hidden)`,
        ];
        if (dropped > 0) {
            head.push(`${count(dropped)} result(s) already dropped from memory and not included - findings are kept for ${keptFor()}.`);
        }
        if (staleCount > 0) {
            head.push(`${count(staleCount)} of the listed finding(s) are older than 24 hours and are marked stale below - re-check before acting on those.`);
        }
        // Same denominator care as the on-screen line: only a guild scan can
        // say "of the server". Hand-rolling the sentence here is how the wrong
        // claim got back into the clipboard after the screen was fixed.
        const reach = coverage(source, collected).replace(/^\s*—\s*/, "");
        if (reach) head.push(`coverage: ${reach}`);
        if (collected?.note) head.push(collected.note);
        if (tally.unanswered) head.push(`${count(tally.unanswered)} member(s) could not be checked in full - not the same as a clean result.`);

        const bodies = listed.slice(0, MAX_COPY_MEMBERS).map(memberSummary);
        if (listed.length > MAX_COPY_MEMBERS) {
            bodies.push(`... and ${count(listed.length - MAX_COPY_MEMBERS)} more not included.`);
        }

        const text = [
            head.join("\n"),
            "",
            bodies.join("\n\n"),
            "",
            // The backend's own attribution, not Rotector's fixed line: an
            // Okappiki scan carries answers from three services and has to
            // credit all three and name all three appeal routes.
            backendAttribution(scanBackend),
            appealSentence(scanBackend),
            // Its own sentence rather than the footer's: a clipboard entry has
            // left the plugin, so what it has to say is what was true of the
            // data and that nothing here can sweep the copy. The default
            // wording is untouched; the lifted one comes from the shared
            // notice, so it cannot describe the choice differently.
            (attempt(() => retainBeyondTerms(), false)
                ? attempt(
                    () => retentionNotice(),
                    `Findings were kept for ${keptHours()} hours by choice, past the 24 hours Rotector's terms allow.`
                )
                : "Findings are dropped within 24 hours - Rotector's terms forbid keeping their data longer, and flag statuses change."
            ) + " This copy is yours to delete; the plugin cannot.",
        ].join("\n");

        attempt(() => copyWithToast(text, `Copied ${count(listed.length)} member(s).`), undefined);
    }

    // ---- the pieces -------------------------------------------------------

    const picker = (
        <Node label="Source" count={sources.length}>
            <Body measure>
                Pick what to check. Every source below states what it can actually reach — the
                client only knows the members Discord has sent it, which is rarely all of them.
            </Body>
            <ScrollerThin className={cl("scroll")}>
                <div className={cl("sources")}>
                    {sources.length === 0 && (
                        <Micro>No source is available here. Open a server, or a group DM, and try again.</Micro>
                    )}
                    {sources.map((entry, index) => {
                        // A role source without an id is the "pick a role" entry
                        // rather than a scannable list; it expands into the guild's
                        // roles instead of collecting.
                        const isRolePicker = entry.kind === "role" && !entry.id;
                        const key = `${entry.kind}:${entry.id ?? entry.guildId ?? index}`;
                        const roleGuild = entry.guildId ?? guildId;
                        const expanded = isRolePicker && roleOpen === key && Boolean(roleGuild);
                        return (
                            <div key={key} className={cl("source")}>
                                <button
                                    type="button"
                                    className={cl("source__hit")}
                                    onClick={() => {
                                        if (isRolePicker) {
                                            setRoleOpen(roleOpen === key ? undefined : key);
                                        } else {
                                            void collectFor(entry);
                                        }
                                    }}
                                >
                                    <span className={cl("source__label")}>{entry.label}</span>
                                    {entry.hint && <span className={cl("source__meta")}>{entry.hint}</span>}
                                </button>
                                {expanded && (
                                    <div className={cl("roles")}>
                                        <Body measure>
                                            A role is the one list Discord will hand over in full: the client asks for
                                            every holder of the role and gets all of them back. Scanning the server
                                            instead only reaches the members the sidebar has already loaded.
                                        </Body>
                                        {attempt(
                                            () => guildRoles(roleGuild as string) ?? [],
                                            [] as { id: string; name: string; memberCount?: number; }[]
                                        ).map(role => (
                                            <button
                                                type="button"
                                                key={role.id}
                                                className={cl("role")}
                                                onClick={() => void collectFor({
                                                    kind: "role",
                                                    id: role.id,
                                                    guildId: roleGuild,
                                                    label: `Role: ${role.name}`,
                                                })}
                                            >
                                                <span>{role.name}</span>
                                                <span className={cl("num")}>
                                                    {role.memberCount == null ? "" : count(role.memberCount)}
                                                </span>
                                            </button>
                                        ))}
                                    </div>
                                )}
                            </div>
                        );
                    })}
                </div>
            </ScrollerThin>
        </Node>
    );

    const client = attempt(() => reportStore.client(), undefined as any);
    const estimate = collected && client
        ? attempt(() => client.estimateSeconds(collected.ids.length), null)
        : null;

    /**
     * The sentence this backend needs before a scan, when it has one.
     *
     * Okappiki's is not a footnote: it is the difference between 218 requests
     * and 10,000, each of the 10,000 costing Okappiki a live Rotector lookup
     * out of their budget, and it has to be read before the button rather than
     * discovered from the clock afterwards.
     */
    const warning = attempt(() => backendWarning(currentBackend()), null);

    const gateway = attempt(() => gatewayAvailability(), { ready: false, mode: "off", problem: "" });
    // Offered only when the gateway is on and this list did *not* come off it —
    // a scrape that failed, or one that was stopped. When it did, the button
    // would be an invitation to do the same work twice.
    const canScrape = Boolean(
        source?.kind === "guild" && source.guildId && gateway.ready && collected?.via !== "gateway"
    );

    /**
     * Whether the connection that button would open is the one that carries a
     * risk to the user's account, and the notice that has to be read first.
     *
     * "client" means the user's own account opens a second gateway session,
     * which is automation of a user account and against Discord's Terms of
     * Service. gateway/tokens.ts gates the token on the setting; the setting
     * cannot show anybody anything, so the consent step is here: the notice is
     * printed verbatim with the button inside it, before anything connects.
     *
     * A missing notice withdraws the offer rather than softening it. The text is
     * the consent; without it there is nothing to consent to, and a bot token
     * does the same job with no Terms problem.
     */
    const consent = consentNotice();
    const needsConsent = canScrape && gateway.mode === "client";

    const preflight = (
        <Node label="Pre-flight">
            <div className={cl("stack")}>
                <div>
                    <Label>{source?.label ?? "source"}</Label>
                    {collecting
                        ? (
                            <Body>
                                {gather
                                    ? `${gather.stage || "Reading the member list"} — ${count(gather.found)}`
                                        + `${gather.total ? ` of ${count(gather.total)}` : ""} found`
                                    : warming
                                        ? "Asking Discord for more of the member list…"
                                        : "Collecting members…"}
                            </Body>
                        )
                        : (
                            <Body>
                                {count(collected?.ids.length ?? 0)} member{(collected?.ids.length ?? 0) === 1 ? "" : "s"} to check
                                {coverage(source, collected)}
                                {estimate == null ? "" : `, roughly ${formatDuration(estimate)}`}
                                {` — against ${backendLabel(currentBackend())}`}
                            </Body>
                        )}
                </div>

                {collected?.note && <Body measure>{collected.note}</Body>}

                {Boolean(collected?.skippedBots) && (
                    <Micro>{count(collected!.skippedBots)} bot account(s) skipped.</Micro>
                )}

                {/* The backend's own warning, printed before the button rather
                    than after the wait it explains. */}
                {warning && <Body measure>{warning}</Body>}

                {source?.kind === "guild" && collected?.via !== "gateway" && (
                    <Body measure>
                        Discord only sends a server's member list as the sidebar is scrolled, so a
                        fresh client knows a fraction of a large server. "Load more members" scrolls
                        it for you, a little at a time; it will not reach everyone, and scanning a
                        role instead will.
                        {gateway.ready
                            ? needsConsent
                                // The button that does it is inside the consent block below, and
                                // its label is not this sentence's to quote.
                                ? " Reading the full member list opens this plugin's own gateway "
                                  + "connection and enumerates the server properly. It does that on "
                                  + "your own account, which is what the notice below is about."
                                : " “Read the full member list” opens this plugin's own gateway "
                                  + "connection and enumerates the server properly, which is the only "
                                  + "thing here that does."
                            : " The gateway member scanner would read the real list, but it is not "
                              + "switched on: see the plugin's settings."}
                    </Body>
                )}

                {collected?.via === "gateway" && (
                    <Micro>
                        {collected.gatewayMode === "bot"
                            ? "Read by a bot connection with the Server Members Intent."
                            : "Read by your own account's gateway connection — automation of a user "
                              + "account, which Discord's Terms of Service prohibit."}
                    </Micro>
                )}

                {/* onRescan = "ask", asked. The queue cannot ask, so it takes
                    the conservative half on its own and the choice lives here. */}
                {duplicate && (
                    <div className={cl("warn")}>
                        <Label>Already scanned</Label>
                        <Body measure>
                            {`This source is already in the queue against ${backendLabel(duplicate.backend)}, `}
                            {STATE_LABEL[duplicate.state].toLowerCase()}
                            {`, with ${count(duplicate.found)} member(s) answered. Reusing it spends nothing; `}
                            scanning again asks the same questions a second time and spends the same
                            budget doing it.
                        </Body>
                        <div className={cl("actions")}>
                            <Btn onClick={() => { setDuplicate(undefined); watchJob(duplicate.id); }}>
                                Show what it found
                            </Btn>
                            <Btn quiet onClick={() => startScan("rescan")}>Scan again anyway</Btn>
                            <Btn quiet onClick={() => setDuplicate(undefined)}>Cancel</Btn>
                        </div>
                    </div>
                )}

                {/* The consent step for the account-token gateway, printed
                    before anything connects and carrying the button that would
                    connect. The notice is CONSENT_NOTICE verbatim — it is the
                    app's own README paragraph and it is not softened here,
                    because the risk it describes is the reader's account. */}
                {needsConsent && (
                    <div className={cl("warn")}>
                        <Label>Read this before it connects</Label>
                        {consent
                            ? (
                                <>
                                    <Body measure>{consent}</Body>
                                    <Micro>
                                        Nothing has connected yet. Pressing the button below is what opens
                                        the connection, on your account, and it is the only thing here that
                                        does.
                                    </Micro>
                                    <div className={cl("actions")}>
                                        <Btn
                                            disabled={collecting}
                                            onClick={() => void collectFor(source!, { gateway: true })}
                                        >
                                            I accept the risk — read the full member list
                                        </Btn>
                                        <Btn
                                            quiet
                                            disabled={collecting}
                                            onClick={() => void collectFor(source!, { warm: true, gateway: false })}
                                        >
                                            No — load more members instead
                                        </Btn>
                                    </div>
                                </>
                            )
                            : (
                                <Body measure>
                                    This connection has to show its consent notice first, and the notice
                                    could not be loaded from the plugin's gateway module — so the
                                    connection is not offered. Scan a role for a complete list, or switch
                                    the gateway to a bot token in the plugin's settings: a bot with the
                                    Server Members Intent reads the same roster with no Terms of Service
                                    problem.
                                </Body>
                            )}
                    </div>
                )}

                <div className={cl("actions")}>
                    <Btn
                        disabled={collecting || !(collected?.ids.length)}
                        onClick={() => startScan()}
                    >
                        Start scan
                    </Btn>
                    {canScrape && !needsConsent && (
                        <Btn
                            quiet
                            disabled={collecting}
                            onClick={() => void collectFor(source!, { gateway: true })}
                        >
                            Read the full member list
                        </Btn>
                    )}
                    {collecting && gather && (
                        <Btn quiet onClick={() => attempt(() => gatherRef.current?.abort(), undefined)}>
                            Stop reading
                        </Btn>
                    )}
                    {/* Warming the sidebar is the fallback for a client that
                        cannot ask the gateway. When the gateway is on it has
                        already read the real list, and offering to scroll the
                        sidebar as well would be offering a worse copy of what
                        just happened. */}
                    {source?.kind === "guild" && source.guildId && !gateway.ready && (
                        <Btn quiet disabled={collecting} onClick={() => void collectFor(source, { warm: true, gateway: false })}>
                            Load more members
                        </Btn>
                    )}
                    <Btn quiet disabled={collecting} onClick={() => { setPhase("pick"); setCollected(undefined); }}>
                        Back
                    </Btn>
                </div>
            </div>
        </Node>
    );

    // ---- the queue --------------------------------------------------------

    /**
     * How many running scans are drawing on each backend's budget.
     *
     * Two scans on one backend is not a fault, but it is why both look half as
     * fast, and saying so beats leaving somebody to wonder whether the plugin
     * has stalled. The budget is shared because Rotector's terms forbid getting
     * around the window — a limiter per scan would simply multiply the request
     * rate by however many scans were queued.
     */
    const sharing = attempt(() => jobQueue.sharing(), {} as Record<string, number>);
    const shared = Object.keys(sharing).filter(name => (sharing[name] ?? 0) > 1);

    const queue = (
        <Node label="Queue" count={jobs.length}>
            <div className={cl("stack")}>
                <Micro>{attempt(() => jobQueue.summary(), "")}</Micro>
                {shared.length > 0 && (
                    <Body measure>
                        {shared.map(name => `${sharing[name]} scans are sharing ${backendLabel(name)}'s rate limit`).join("; ")}
                        {". That is why each of them is going at a fraction of the speed: the limit "}
                        is per service, not per scan, and splitting it is the only honest way to run
                        two at once.
                    </Body>
                )}
                {jobs.length === 0
                    ? <Micro>No scans queued.</Micro>
                    : (
                        <div className={cl("jobs")}>
                            {jobs.map(entry => (
                                <div
                                    key={entry.id}
                                    className={cl("job", entry.id === jobId && "job--current")}
                                >
                                    <div className={cl("job__hd")}>
                                        <span className={cl("job__name")}>{entry.label}</span>
                                        <span className={cl("job__state")}>
                                            {entry.stopping ? "STOPPING" : STATE_LABEL[entry.state]}
                                        </span>
                                    </div>
                                    <div className={cl("job__meta")}>
                                        <Micro>
                                            {`${count(entry.progress?.done ?? 0)}/${count(entry.progress?.total ?? entry.ids.length)}`}
                                            {`  ${count(entry.findings)} finding${entry.findings === 1 ? "" : "s"}`}
                                            {entry.unanswered ? `  ${count(entry.unanswered)} unanswered` : ""}
                                            {`  ${formatDuration(attempt(() => jobElapsed(entry), 0))}`}
                                        </Micro>
                                        {entry.error && <Micro>{entry.error}</Micro>}
                                    </div>
                                    <div className={cl("actions")}>
                                        <Btn quiet onClick={() => watchJob(entry.id)}>Show</Btn>
                                        {entry.state === "running" || entry.state === "queued"
                                            ? (
                                                <Btn quiet onClick={() => attempt(() => jobQueue.stop(entry.id), undefined)}>
                                                    Stop
                                                </Btn>
                                            )
                                            : (
                                                <Btn quiet onClick={() => attempt(() => jobQueue.remove(entry.id), undefined)}>
                                                    Remove
                                                </Btn>
                                            )}
                                    </div>
                                </div>
                            ))}
                        </div>
                    )}
                <div className={cl("actions")}>
                    <Btn quiet onClick={() => { setPhase("pick"); setCollected(undefined); }}>New scan</Btn>
                    <Btn quiet onClick={() => attempt(() => jobQueue.clearFinished(), 0)}>Clear finished</Btn>
                    <Btn quiet onClick={() => attempt(() => jobQueue.stopAll(), undefined)}>Stop all</Btn>
                </div>
            </div>
        </Node>
    );

    // ---- the run ----------------------------------------------------------

    const elapsed = attempt(() => jobElapsed(job), 0);
    const done = job?.progress?.done ?? 0;
    const total = job?.progress?.total || jobIds.length || 0;
    const pct = total > 0 ? Math.min(100, Math.max(0, (done / total) * 100)) : 0;
    const running = job?.state === "running" || job?.state === "queued";

    const runner = (
        <Node label="Run">
            <div className={cl("stack")}>
                <div
                    className={cl("bar")}
                    role="progressbar"
                    aria-valuemin={0}
                    aria-valuemax={total || 100}
                    aria-valuenow={done}
                >
                    <div className={cl("bar__fill")} style={{ width: `${pct}%` }} />
                </div>
                <Micro>
                    {(job?.stopping ? "Stopping" : job?.progress?.stage ?? "Working") + "  "}
                    {count(done)}/{count(total)}
                    {"  elapsed "}{formatDuration(elapsed)}
                    {job?.etaSeconds == null ? "" : `  of about ${formatDuration(job.etaSeconds)}`}
                    {`  via ${backendLabel(scanBackend)}`}
                </Micro>
                <div className={cl("actions")}>
                    {running
                        ? (
                            <Btn onClick={() => attempt(() => jobQueue.stop(job!.id), undefined)}>Stop</Btn>
                        )
                        : (
                            <Btn quiet onClick={() => { setPhase("pick"); setCollected(undefined); }}>
                                New scan
                            </Btn>
                        )}
                    <Btn quiet onClick={() => setPhase("queue")}>Queue ({jobs.length})</Btn>
                </div>
                {job?.state === "stopped" && (
                    <Micro>
                        Stopped after {count(job.found)} of {count(total)}. What was already checked is
                        listed below; the rest was not checked and is not clean.
                    </Micro>
                )}
                {job?.state === "failed" && (
                    <Micro>{job.error || "The scan failed."}</Micro>
                )}
                {/* "Complete" is about the run, never about the members: a scan
                    that finished with lookups that did not answer has not
                    cleared anybody, so it says so on the same line. */}
                {job?.state === "done" && (
                    <Micro>
                        {tally.unanswered
                            ? `Scan complete — ${count(tally.unanswered)} member(s) could not be checked in full. That is not a clean result.`
                            : "Scan complete."}
                    </Micro>
                )}
                {/* A half-hour scan is something somebody sits through; the
                    player returns null unless vibe mode is switched on. */}
                <VibeControls />
            </div>
        </Node>
    );

    // ---- selection, for the moderation action -----------------------------

    /**
     * Which listed members an action would apply to.
     *
     * Nothing is selected by default and "select all" only ever covers the rows
     * currently listed, so a filter is a real limit on what can be actioned
     * rather than a view over a larger hidden set. The dialog re-checks every
     * one of them against the policy anyway — this is a shortlist, not a
     * decision.
     */
    const chosen = useMemo(
        () => listed.filter(report => selected.has(report.discordId)),
        [listed, selected]
    );

    function toggleSelected(id: string) {
        setSelected(current => {
            const next = new Set(current);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    }

    /**
     * The guild an action would be taken in.
     *
     * A kick or a ban is a guild operation, so a scan of a friends list or a
     * group DM offers none: there is nowhere to take it. The job's own source
     * is the authority, because the settings and the open channel can both have
     * moved on since the scan ran.
     */
    const actionGuildId = job?.source?.guildId ?? source?.guildId;

    function act(kind: "kick" | "ban") {
        if (!actionGuildId || !chosen.length) return;
        attempt(() => openModerationDialog(kind, chosen, actionGuildId), undefined);
    }

    const ledger = (
        <Node label="Results" count={all.length}>
            <div className={cl("stack")}>
                <div className={cl("actions")}>
                    <Btn quiet={!findingsOnly} onClick={() => setFindingsOnly(true)}>Findings</Btn>
                    <Btn quiet={findingsOnly} onClick={() => setFindingsOnly(false)}>Everything</Btn>
                    {actionGuildId && page.length > 0 && (
                        <Btn
                            quiet
                            onClick={() => setSelected(current =>
                                current.size >= listed.length
                                    ? new Set()
                                    : new Set(listed.map(report => report.discordId)))}
                        >
                            {selected.size >= listed.length && listed.length > 0 ? "Select none" : "Select listed"}
                        </Btn>
                    )}
                </div>
                {/* Always printed, hidden count included, even at zero: the TUI's
                    rule is that nothing is dropped silently (app.py:2503-2519). */}
                <Micro>
                    filter: {findingsOnly ? "findings" : "all"} ({count(Math.max(0, hidden))} hidden)
                    {listed.length > page.length ? `  showing ${count(page.length)} of ${count(listed.length)}` : ""}
                </Micro>

                {page.length === 0
                    ? (
                        <Body>
                            {all.length === 0
                                ? scanned === 0
                                    ? "Nothing checked yet."
                                    : `These findings have been dropped from memory — they are kept for ${keptFor()}, and this scan's results have since expired or been purged. Run the scan again to re-check, or open Past scans: a record of it may still be stored, under the same ${ceilingLabel()} ceiling.`
                                : "No member in this scan carries a finding. That is not a clean bill of health — see the summary below for what was actually answered."}
                        </Body>
                    )
                    : (
                        <ScrollerThin className={cl("scroll")}>
                            <div className={cl("ledger__wrap")}>
                                <table className={cl("ledger")}>
                                    <thead>
                                        <tr>
                                            {actionGuildId && <th scope="col" className={cl("ledger__pick")}>Act</th>}
                                            <th scope="col">Member</th>
                                            <th scope="col">Verdict</th>
                                            <th scope="col">Flag</th>
                                            <th scope="col">Category</th>
                                            <th scope="col">Roblox</th>
                                            {showTriage && <th scope="col">Triage</th>}
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {page.map(report => (
                                            <LedgerRow
                                                key={report.discordId}
                                                report={report}
                                                guildId={actionGuildId}
                                                backend={scanBackend}
                                                showTriage={showTriage}
                                                selectable={Boolean(actionGuildId)}
                                                selected={selected.has(report.discordId)}
                                                onToggle={toggleSelected}
                                            />
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                        </ScrollerThin>
                    )}

                {listed.length > page.length && (
                    <div className={cl("actions")}>
                        <Btn quiet onClick={() => setLimit(l => l + PAGE_SIZE)}>Show more</Btn>
                    </div>
                )}
            </div>
        </Node>
    );

    const summary = (
        <Node label="Summary">
            <div className={cl("stack")}>
                <div className={cl("tally")}>
                    {[...ALL_VERDICTS].reverse().map(verdict => (
                        tally.counts[verdict]
                            ? (
                                <div className={cl("tally__row")} key={verdictName(verdict)}>
                                    <Meter verdict={verdict} />
                                    <VerdictLine verdict={verdict} meaning={false} />
                                    <span className={cl("num")}>{count(tally.counts[verdict])}</span>
                                </div>
                            )
                            : null
                    ))}
                    {all.length === 0 && <Micro>{scanned === 0 ? "no results yet" : "no results held"}</Micro>}
                </div>
                {dropped > 0 && (
                    <Micro>
                        {count(dropped)} of {count(scanned)} result(s) have been dropped from memory and are
                        no longer counted below — findings are kept for {keptFor()}.
                    </Micro>
                )}
                {/* Beside the unanswered count, and for the same reason it is
                    there: both are statements about what these numbers are not.
                    A stale row is a claim about the day it was answered, and
                    flag types change and appeals succeed inside a day. */}
                {staleCount > 0 && (
                    <div className={cl("stale")} data-verdict={verdictName(Verdict.CAUTION)}>
                        <span className={cl("verdict")} data-verdict={verdictName(Verdict.CAUTION)}>
                            {`${count(staleCount)} STALE`}
                        </span>
                        <Body measure>
                            {`${count(staleCount)} of the ${count(all.length)} result(s) held are older than 24 hours — `}
                            past the window Rotector's Terms of Use allow, and each row says how old it
                            is. Re-check before acting on one: flag types change and appeals succeed.
                        </Body>
                    </div>
                )}
                <Body measure>
                    {count(scanned)} member{scanned === 1 ? "" : "s"} checked
                    {coverage(source, collected)}
                    {". "}
                    {tally.unanswered
                        ? `${count(tally.unanswered)} could not be checked in full; a lookup that did not answer is not a clean result.`
                        : "Every member listed was answered."}
                </Body>

                {/* The caveat has to travel with the number after the run, not
                    only before it: the summary is what gets read and screenshotted,
                    and a coverage figure without its note is the figure lying. */}
                {collected?.note && <Body measure>{collected.note}</Body>}
                <Micro>
                    NO DETECTIONS means nothing has been detected yet. It is not a clean bill of health,
                    and it is never a background check.
                </Micro>
            </div>
        </Node>
    );

    const footer = (
        <div className={cl("foot")}>
            <div className={cl("actions")}>
                <Btn disabled={all.length === 0} onClick={copyFindings}>Copy findings</Btn>
                <Btn
                    quiet
                    disabled={all.length === 0}
                    onClick={() => attempt(() => openExportMenu(
                        all,
                        source?.label ?? job?.label ?? "members",
                        { guildId: actionGuildId, note: collected?.note, scanned }
                    ), undefined)}
                >
                    Export
                </Btn>
                {/* A scan outlives the window that ran it, and now outlives the
                    session too. This is the way back to what has already been
                    found, and it is here rather than buried in settings. */}
                <Btn quiet onClick={() => attempt(() => openHistory(), undefined)}>Past scans</Btn>
                {actionGuildId && (
                    <>
                        <Btn quiet disabled={chosen.length === 0} onClick={() => act("kick")}>
                            {`Kick${chosen.length ? ` ${count(chosen.length)}` : ""}…`}
                        </Btn>
                        <Btn quiet disabled={chosen.length === 0} onClick={() => act("ban")}>
                            {`Ban${chosen.length ? ` ${count(chosen.length)}` : ""}…`}
                        </Btn>
                    </>
                )}
            </div>
            {actionGuildId && (
                <Micro>
                    Only Rotector flag types Flagged and Confirmed are documented as safe to act on.
                    Anything else is an override, and the dialog names it as one before it sends.
                </Micro>
            )}
            {/* The backend's own line, because an Okappiki scan carries answers
                from three services and owes all three the credit and the appeal
                route. Rotector's terms require the link either way. */}
            <Micro>{backendAttribution(scanBackend)}</Micro>
            <Micro>{appealSentence(scanBackend)}</Micro>
            {/* Not "memory only" any more, and the difference is worth the extra
                clause: a finished scan is written to this plugin's own database
                on this computer so it survives a restart. The deadline is the
                same one for both, and it is read fresh from the setting rather
                than asserted — `retainBeyondTerms` moves it, and a footer that
                went on promising a 24-hour drop while the caches held findings
                for a week would be the plugin misreporting itself. */}
            <Micro>{retentionLine()}</Micro>
            <Micro>
                Exported files are yours to delete: a plugin cannot sweep your downloads folder, so
                every file states its own expiry instead.
            </Micro>
            <VibeControls compact />
        </div>
    );

    return (
        <Modal
            {...modalProps}
            size="lg"
            title={<span className={`rsb ${cl("modal-title")}`}>Rotector scan</span>}
        >
            <ErrorBoundary>
                {/*
                  * `rsb--field` paints the two-value ground. A plugin-owned
                  * surface that stays transparent puts #ffffff ink and the five
                  * verdict hexes straight onto Discord's grey, which is the
                  * grey fill the design language bans outright.
                  */}
                <div className={`rsb rsb--field ${cl("scan")}`}>
                    {error && (
                        <div className={cl("error")}>
                            <Label>Failed</Label>
                            <Body measure>{error}</Body>
                            <Micro>
                                If the request never left the browser, the host client is blocking the
                                connection rather than the API refusing it. Open this plugin's settings
                                and use the connectivity panel — on Vencord that needs a permission grant
                                and a full restart; on Equicord desktop no action is needed.
                            </Micro>
                        </div>
                    )}
                    {phase === "pick" && (
                        <>
                            {picker}
                            {jobs.length > 0 && queue}
                        </>
                    )}
                    {phase === "preflight" && preflight}
                    {phase === "queue" && queue}
                    {/* A job removed from the queue takes its ledger with it —
                        the reports were never this component's to hold — so the
                        window falls back to the queue rather than drawing a run
                        with nothing behind it. */}
                    {phase === "run" && (job
                        ? (
                            <>
                                {runner}
                                {ledger}
                                {summary}
                            </>
                        )
                        : queue)}
                    {footer}
                </div>
            </ErrorBoundary>
        </Modal>
    );
}

// --------------------------------------------------------------------------
// one ledger row
// --------------------------------------------------------------------------

function LedgerRow({ report, guildId, backend, showTriage, selectable, selected, onToggle }: {
    report: MemberReport;
    guildId?: string;
    /** the service this row's scan actually asked, which stays true even if the
        backend setting is changed before somebody clicks the row */
    backend?: string;
    showTriage: boolean;
    selectable?: boolean;
    selected?: boolean;
    onToggle?(id: string): void;
}) {
    const id = report.discordId;
    const verdict = rowVerdict(report);
    const worst = attempt(() => worstAccount(report), null);
    const accounts = report.accounts ?? [];

    // The TUI shows one account — the worst, tie-broken on confidence — plus a
    // "+N" overflow marker, rather than a list that would not fit (app.py:2377).
    const extra = accounts.length > 1 ? ` +${accounts.length - 1}` : "";
    const roblox = worst ? `${worst.username}${extra}` : "-";

    const call = showTriage
        ? attempt(() => triage(report, attempt(() => policyFromSettings(), undefined as any)), undefined)
        : undefined;

    // The Discord hop can answer while the Roblox flag hop fails, which leaves a
    // report carrying both accounts and an error. Neither half may be dropped:
    // hiding the accounts would bury a real finding, and printing the flag on
    // its own would present an interrupted lookup as a finished one.
    const failed = Boolean(report.error);
    const partial = failed && accounts.length > 0;

    /** "STALE 3d", or "" for anything answered inside the terms' window. */
    const tag = attempt(() => staleTag(report), "");

    const open = () => attempt(() => openReport(id, guildId, backend), undefined);

    return (
        <tr
            className={cl("row")}
            role="button"
            tabIndex={0}
            onClick={open}
            onKeyDown={(e: any) => {
                if (e?.key === "Enter" || e?.key === " ") {
                    e.preventDefault?.();
                    open();
                }
            }}
        >
            {selectable && (
                // The cell stops the click reaching the row, which opens the
                // report: ticking a box to plan an action and opening a modal
                // are different intentions and must not share one gesture.
                <td
                    className={cl("ledger__pick")}
                    onClick={(e: any) => e?.stopPropagation?.()}
                >
                    <label className={`rsb ${cl("check")}`}>
                        <input
                            type="checkbox"
                            className={cl("check__box")}
                            checked={Boolean(selected)}
                            onChange={() => onToggle?.(id)}
                        />
                        <span className={cl("check__text")}>{`select ${memberName(id)}`}</span>
                    </label>
                </td>
            )}
            <th scope="row">
                <span>{memberName(id)}</span>
                <span className={cl("row__handle")}>{memberHandle(id)}</span>
            </th>
            <td className={cl("ledger__verdict")}>
                <Meter verdict={verdict} />
                <VerdictLine verdict={verdict} meaning={false} />
                {/* Under the verdict rather than in a column of its own: it
                    qualifies the verdict, and the mark itself keeps its hue
                    because staleness is not a sixth tier. Blank for everything
                    inside the window, so a normal ledger is unchanged. */}
                {tag && (
                    <span
                        className={cl("stale-tag")}
                        data-verdict={verdictName(Verdict.CAUTION)}
                        title={attempt(() => staleNote(report), "")}
                    >
                        {tag}
                    </span>
                )}
            </td>
            <td>
                {failed && !partial
                    ? "did not answer"
                    : (
                        <>
                            <span>{flagName(worst ? worst.flagType : null)}</span>
                            {partial && <span className={cl("row__handle")}>incomplete lookup</span>}
                        </>
                    )}
            </td>
            <td>{(worst && categoryName(worst.category)) || "-"}</td>
            <td>
                <span>{roblox}</span>
                {partial && <span className={cl("row__handle")}>may not be all of them</span>}
            </td>
            {showTriage && (
                <td>
                    {call
                        ? (
                            <>
                                <span
                                    className={cl("rec")}
                                    data-verdict={verdictName(attempt(() => recommendationTier(call.recommendation), Verdict.UNKNOWN))}
                                >
                                    {recommendationLabel(call.recommendation)}
                                </span>
                                {/* the rule id is the audit handle; it is never hidden */}
                                <span className={cl("row__handle")}>rule {call.rule}</span>
                            </>
                        )
                        : "-"}
                </td>
            )}
        </tr>
    );
}
