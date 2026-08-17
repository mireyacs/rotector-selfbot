/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The scan history, as one modal: what has been scanned recently, and what each
 * of those scans found.
 *
 * This is the only surface in the plugin that shows findings which outlived the
 * window they were produced in, so it carries the retention argument in full
 * rather than in a footnote. Three things are said plainly and are not to be
 * softened into a single tidy line:
 *
 *  - the records are on disk, in this client, and not only in memory;
 *  - each one is deleted 23 hours after its scan started, because Rotector's
 *    Terms of Use forbid keeping their responses beyond 24 hours, and because a
 *    flag status changes — yesterday's finding may be wrong today;
 *  - the clean rows were never stored at all. A scan of ten thousand members is
 *    almost entirely NO DETECTIONS; those members were counted and their reports
 *    were dropped, and the count is printed so nobody mistakes a short list for
 *    a short scan.
 *
 * The ledger is the one from ScanModal — same columns, same findings-first
 * order, same row-opens-the-report gesture — rebuilt here rather than imported,
 * because that component reads a live job out of the queue and this one reads a
 * record off disk. Two callers, one design, no shared state.
 */

import ErrorBoundary from "@components/ErrorBoundary";
import { copyWithToast } from "@utils/discord";
import { Modal, openModal, ScrollerThin, useEffect, useMemo, useState, UserStore } from "@webpack/common";

import { backendAttribution, backendLabel } from "../api/backend";
import { APPEAL_LINKS, PARTIES, recommendationLabel, triage, verdictFloor as recommendationTier } from "../api/triage";
import { isStale, MemberReport, reportVerdict, sortedAccounts, staleNote, staleTag, worstAccount } from "../api/types";
import { ALL_VERDICTS, categoryName, flagName, Verdict, verdictLabel, verdictMeaning, verdictName } from "../api/verdict";
import {
    historyRetentionNotice,
    recordExpiresAt,
    ScanRecord,
    scanHistory,
    useHistoryStats,
    useHistoryStatus,
    useScanHistory,
} from "../features/history";
import { policyFromSettings, retainBeyondTerms, retentionHours, settings, verdictFloor as configuredFloor } from "../settings";
import { Body, Btn, cl, Label, Meter, Micro, Node, VerdictLine } from "./primitives";
import { openReport } from "./ReportModal";

/** how many rows of one record's ledger are drawn before "Show more" (ScanModal's number) */
const PAGE_SIZE = 250;

/** how often the "expires in" figures are redrawn, so a deadline visibly approaches */
const CLOCK_MS = 30_000;

// --------------------------------------------------------------------------
// small helpers — this file is loaded without type checking, so a sibling
// module that failed to evaluate hands back `undefined` rather than failing to
// build, and every call across a module boundary is guarded accordingly
// --------------------------------------------------------------------------

function attempt<T>(fn: () => T, fallback: T): T {
    try {
        const value = fn();
        return value === undefined ? fallback : value;
    } catch {
        return fallback;
    }
}

/** Read one plugin setting. `settings.store` throws before the plugin is registered. */
function option<T>(key: string, fallback: T): T {
    return attempt(() => {
        const store = (settings as any)?.store as Record<string, unknown> | undefined;
        if (store == null) return fallback;
        const value = store[key];
        return (value === undefined || value === null ? fallback : value) as T;
    }, fallback);
}

/** The lowest verdict that counts as a finding. Defaults to INFO, as the TUI does. */
function findingFloor(): Verdict {
    return attempt(() => {
        const floor = typeof configuredFloor === "function" ? configuredFloor() : undefined;
        return typeof floor === "number" ? (floor as Verdict) : Verdict.INFO;
    }, Verdict.INFO);
}

function count(n: number): string {
    return attempt(() => Number(n || 0).toLocaleString(), String(n || 0));
}

/**
 * How long a record lives, in words: "23 hours" by default.
 *
 * The ceiling moves when `retainBeyondTerms` is switched on, so the sentences
 * that quote it read it rather than assert it. The default answer is the same
 * one this window has always printed, which is the point: nothing about the
 * ordinary case changes.
 */
function recordLifespan(): string {
    if (!attempt(() => retainBeyondTerms(), false)) return "23 hours";
    const hours = Math.max(1, Math.round(attempt(() => retentionHours(), 168)));
    return hours >= 48 ? `${Math.round(hours / 24)} days` : `${hours} hours`;
}

/** How many of a record's stored findings are past the terms' 24-hour window. */
function staleIn(reports: MemberReport[] | undefined): number {
    let stale = 0;
    for (const report of reports ?? []) {
        if (attempt(() => isStale(report), false)) stale++;
    }
    return stale;
}

/** `95` -> `"1m 35s"`. The same shape rsb/eta.py's format_duration produces. */
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

/** "14 minutes ago", give or take. Absolute time is printed beside it. */
function ago(at: number, now: number): string {
    const delta = Math.max(0, now - Number(at || 0)) / 1000;
    if (delta < 45) return "just now";
    return `${formatDuration(delta)} ago`;
}

function absolute(at: number): string {
    return attempt(() => new Date(Number(at)).toLocaleString(), String(at));
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

function rowVerdict(report: MemberReport): Verdict {
    return attempt(() => reportVerdict(report), Verdict.UNKNOWN);
}

function isUnanswered(report: MemberReport): boolean {
    return Boolean(report?.error);
}

/**
 * The appeal routes a backend's findings owe the people they name.
 *
 * Rotector's terms require their data to carry a route to rotector.com so the
 * person named can appeal. An Okappiki record holds three services' findings, so
 * naming only Rotector's door would accuse somebody on two other services'
 * say-so and then send them to the wrong place. Copied from ScanModal's footer
 * because it is the same obligation, and it applies to a stored finding exactly
 * as it applies to a fresh one.
 */
function appealSentence(backends: string[]): string {
    const wanted = new Set<string>();
    for (const backend of backends) {
        const parties = backend === "okappiki" ? PARTIES : (["rotector"] as readonly string[]);
        for (const party of parties) wanted.add(party);
    }
    if (!wanted.size) wanted.add("rotector");

    const urls: string[] = [];
    for (const party of Array.from(wanted)) {
        const url = attempt(() => APPEAL_LINKS[party], undefined as unknown as string);
        if (url && !urls.includes(url)) urls.push(url.replace(/^https?:\/\//, ""));
    }
    if (!urls.length) urls.push("rotector.com");
    return `Anyone listed here can appeal at ${urls.join(", ")}`;
}

/** Which services answered for the records on screen, so all of them get credited. */
function backendsOf(records: ScanRecord[]): string[] {
    const names: string[] = [];
    for (const record of records) {
        const name = String(record?.backend || "rotector");
        if (!names.includes(name)) names.push(name);
    }
    return names.length ? names : ["rotector"];
}

/** One member, in the shape rsb/tui/app.py's `_member_summary` produces. */
function memberSummary(report: MemberReport): string {
    const id = report.discordId;
    const verdict = rowVerdict(report);
    const lines: string[] = [
        `${memberName(id)} (${memberHandle(id)})`,
        `Discord ID: ${id}`,
        // The meaning travels with the word, always: a pasted bare
        // "NO DETECTIONS" reads as a clean result, and presenting an unflagged
        // account that way is the one thing Rotector's terms forbid outright.
        `Verdict: ${verdictLabel(verdict)} - ${verdictMeaning(verdict)}`,
    ];
    // A stored finding is the one most likely to have outlived the answer it
    // describes, so the age goes with the verdict into the clipboard too.
    const stale = attempt(() => staleNote(report), "");
    if (stale) lines.push(stale);
    const accounts = attempt(() => sortedAccounts(report), [] as ReturnType<typeof sortedAccounts>);
    if (report.error) {
        lines.push(accounts.length
            ? `Lookup incomplete: ${report.error} - what follows may not be everything.`
            : `Lookup failed: ${report.error}`);
    }
    for (const account of accounts) {
        const category = account.category ? ` / ${categoryName(account.category)}` : "";
        lines.push(`  Roblox ${account.username} (${account.userId}) - ${flagName(account.flagType)}${category}`);
        lines.push(`    https://www.roblox.com/users/${account.userId}/profile`);
        for (const [name, detail] of Object.entries(account.reasons ?? {})) {
            const message = String((detail as any)?.message ?? "").replace(/\n/g, " / ");
            lines.push(`    ${name}: ${message}`);
        }
    }
    return lines.join("\n");
}

// --------------------------------------------------------------------------
// the modal
// --------------------------------------------------------------------------

export function openHistory(): void {
    try {
        openModal(props => <HistoryModal modalProps={props} />);
    } catch (err) {
        console.error("[RotectorScan] could not open the scan history", err);
    }
}

function HistoryModal({ modalProps }: { modalProps: any; }) {
    const records = useScanHistory();
    const stats = useHistoryStats();
    const status = useHistoryStatus();

    const [openId, setOpenId] = useState<string | undefined>(undefined);
    const [confirmPurge, setConfirmPurge] = useState(false);
    const [confirmDelete, setConfirmDelete] = useState<string | undefined>(undefined);

    // One clock for every "N ago" and "expires in N" on screen. The ceiling is a
    // deadline rather than an event, so a window left open has to watch it move.
    const [now, setNow] = useState(() => Date.now());
    useEffect(() => {
        const timer = setInterval(() => setNow(Date.now()), CLOCK_MS);
        return () => clearInterval(timer);
    }, []);

    const open = openId ? records.find(record => record.id === openId) : undefined;

    // A record that expired, or was deleted, while its ledger was on screen
    // takes the reader back to the list rather than leaving an empty detail
    // pane behind. The reports are gone; the window says so by moving.
    useEffect(() => {
        if (openId && !open) setOpenId(undefined);
    }, [openId, open]);

    const backends = backendsOf(open ? [open] : records);

    const retention = (
        <>
            {/* The live sentence rather than the constant: with
                `retainBeyondTerms` on, "deleted 23 hours after the scan
                started" would be telling the reader something that is not
                happening. */}
            <Micro>{historyRetentionNotice()}</Micro>
            <Micro>
                Records are kept in this plugin's own database on this computer, and nowhere else.
                Deleting one, or purging them all, deletes it from disk.
            </Micro>
        </>
    );

    const footer = (
        <div className={cl("foot")}>
            {/* Each backend's own line: an Okappiki record carries answers from
                three services and owes all three the credit and the appeal
                route. Rotector's terms require the link either way. */}
            {backends.map(name => <Micro key={name}>{backendAttribution(name)}</Micro>)}
            <Micro>{appealSentence(backends)}</Micro>
            {retention}
        </div>
    );

    return (
        <Modal
            {...modalProps}
            size="lg"
            title={<span className={`rsb ${cl("modal-title")}`}>Scan history</span>}
        >
            <ErrorBoundary>
                {/* `rsb--field` paints the two-value ground; a transparent
                    plugin surface would put #ffffff ink and the five verdict
                    hexes straight onto Discord's grey. */}
                <div className={`rsb rsb--field ${cl("scan")}`}>
                    {!status.on && (
                        <div className={cl("error")}>
                            <Label>Not being saved</Label>
                            <Body measure>{status.reason}</Body>
                            <Micro>
                                Scans still run and still report; they are simply not written down, so
                                anything below is only what this session has seen.
                            </Micro>
                        </div>
                    )}

                    {open
                        ? (
                            <RecordDetail
                                record={open}
                                now={now}
                                onBack={() => setOpenId(undefined)}
                                onDelete={() => setConfirmDelete(open.id)}
                            />
                        )
                        : (
                            <RecordList
                                records={records}
                                stats={stats}
                                hydrated={status.hydrated}
                                now={now}
                                onOpen={setOpenId}
                                onDelete={setConfirmDelete}
                            />
                        )}

                    {/* Deletion is not undoable and the data cannot be fetched
                        again from a cache that no longer holds it, so both
                        destructive actions ask first, in the frame rather than
                        in a second modal stacked on this one. */}
                    {confirmDelete && (
                        <div className={cl("warn")}>
                            <Label>Delete this scan</Label>
                            <Body measure>
                                The record and every finding in it go from disk now. Nothing else is
                                touched, and the scan can be run again — it will ask the backend the same
                                questions and spend the same budget doing it.
                            </Body>
                            <div className={cl("actions")}>
                                <Btn
                                    onClick={() => {
                                        const id = confirmDelete;
                                        setConfirmDelete(undefined);
                                        if (openId === id) setOpenId(undefined);
                                        attempt(() => { void scanHistory.remove(id); }, undefined);
                                    }}
                                >
                                    Delete it
                                </Btn>
                                <Btn quiet onClick={() => setConfirmDelete(undefined)}>Keep it</Btn>
                            </div>
                        </div>
                    )}

                    {confirmPurge && (
                        <div className={cl("warn")}>
                            <Label>Delete every stored scan</Label>
                            <Body measure>
                                {`All ${count(stats.scans)} record(s) and every finding in them are deleted from `}
                                this computer. This plugin keeps its history in a database of its own, so
                                nothing else the client stores is affected — and nothing here can be
                                recovered afterwards.
                            </Body>
                            <div className={cl("actions")}>
                                <Btn
                                    onClick={() => {
                                        setConfirmPurge(false);
                                        setOpenId(undefined);
                                        attempt(() => { void scanHistory.purge(); }, undefined);
                                    }}
                                >
                                    Delete everything
                                </Btn>
                                <Btn quiet onClick={() => setConfirmPurge(false)}>Cancel</Btn>
                            </div>
                        </div>
                    )}

                    {!confirmPurge && (
                        <div className={cl("actions")}>
                            <Btn
                                quiet
                                disabled={records.length === 0}
                                onClick={() => setConfirmPurge(true)}
                            >
                                Delete every stored scan
                            </Btn>
                        </div>
                    )}

                    {footer}
                </div>
            </ErrorBoundary>
        </Modal>
    );
}

// --------------------------------------------------------------------------
// the list of past scans
// --------------------------------------------------------------------------

function RecordList({ records, stats, hydrated, now, onOpen, onDelete }: {
    records: ScanRecord[];
    stats: { scans: number; members: number; findings: number; oldest?: number; stale?: number; };
    hydrated: boolean;
    now: number;
    onOpen(id: string): void;
    onDelete(id: string): void;
}) {
    return (
        <Node label="Past scans" count={records.length}>
            <div className={cl("stack")}>
                <Body measure>
                    {`Every scan this plugin has run recently, newest first. A record is deleted ${recordLifespan()} `}
                    after its scan started, whether or not this window is open: Rotector's Terms of Use
                    forbid keeping their responses beyond 24 hours, and a flag status changes — a
                    finding from yesterday can be wrong today, which is why an old record is worth
                    less than a fresh scan rather than the same.
                </Body>
                <Micro>
                    {`${count(stats.scans)} scan(s) held  ${count(stats.members)} member(s) answered  `}
                    {`${count(stats.findings)} finding(s)`}
                    {stats.stale ? `  ${count(stats.stale)} stale` : ""}
                </Micro>
                {/* Zero unless the ceiling has been lifted, and unmissable when
                    it is not: a stored finding older than the window describes
                    the day it was answered, and this list is where somebody
                    goes looking for evidence about today. */}
                {Boolean(stats.stale) && (
                    <div className={cl("stale")} data-verdict={verdictName(Verdict.CAUTION)}>
                        <span className={cl("verdict")} data-verdict={verdictName(Verdict.CAUTION)}>
                            {`${count(stats.stale ?? 0)} STALE`}
                        </span>
                        <Body measure>
                            {`${count(stats.stale ?? 0)} of the ${count(stats.findings)} stored finding(s) are older than `}
                            24 hours — past the window Rotector's Terms of Use allow. Each one is marked
                            with its age where it is listed. Re-check before acting on those: flag types
                            change and appeals succeed.
                        </Body>
                    </div>
                )}

                {records.length === 0
                    ? (
                        <Body>
                            {hydrated
                                ? `No scans are stored. One is written down when a scan finishes, and it is deleted ${recordLifespan()} after it started.`
                                : "Reading the stored scans…"}
                        </Body>
                    )
                    : (
                        <ScrollerThin className={cl("scroll")}>
                            <div className={cl("jobs")}>
                                {records.map(record => (
                                    <RecordRow
                                        key={record.id}
                                        record={record}
                                        now={now}
                                        onOpen={onOpen}
                                        onDelete={onDelete}
                                    />
                                ))}
                            </div>
                        </ScrollerThin>
                    )}
            </div>
        </Node>
    );
}

function RecordRow({ record, now, onOpen, onDelete }: {
    record: ScanRecord;
    now: number;
    onOpen(id: string): void;
    onDelete(id: string): void;
}) {
    const left = Math.max(0, recordExpiresAt(record) - now) / 1000;
    const stale = staleIn(record.reports);

    return (
        <div className={cl("job")}>
            <div className={cl("job__hd")}>
                <span className={cl("job__name")}>{record.sourceLabel}</span>
                <span className={cl("job__state")}>{backendLabel(record.backend).toUpperCase()}</span>
            </div>
            <div className={cl("job__meta")}>
                <Micro>
                    {`${ago(record.startedAt, now)}  ${absolute(record.startedAt)}`}
                </Micro>
                <Micro>
                    {`${count(record.scanned)} answered  ${count(record.findings)} finding(s)`}
                    {record.unanswered ? `  ${count(record.unanswered)} unanswered` : ""}
                    {record.omitted ? `  ${count(record.omitted)} clean row(s) not stored` : ""}
                    {record.truncated ? `  ${count(record.truncated)} finding(s) not stored` : ""}
                </Micro>
                {/* The deadline, on the row rather than in the footer: a reader
                    deciding whether to act on a record needs to know how much of
                    its life is left. */}
                <Micro>{`deleted in ${formatDuration(left)}`}</Micro>
                {stale > 0 && (
                    <Micro>
                        <span className={cl("verdict")} data-verdict={verdictName(Verdict.CAUTION)}>
                            {`${count(stale)} STALE`}
                        </span>
                        {" — older than 24 hours; each row says how old, and a re-check is due before acting."}
                    </Micro>
                )}
                {record.coverageNote && <Micro>{record.coverageNote}</Micro>}
                {record.unanswered > 0 && (
                    <Micro>
                        A lookup that did not answer is not a clean result — those members were not
                        checked.
                    </Micro>
                )}
            </div>
            <div className={cl("actions")}>
                <Btn quiet onClick={() => onOpen(record.id)}>Show findings</Btn>
                <Btn quiet onClick={() => onDelete(record.id)}>Delete</Btn>
            </div>
        </div>
    );
}

// --------------------------------------------------------------------------
// one record's findings
// --------------------------------------------------------------------------

function RecordDetail({ record, now, onBack, onDelete }: {
    record: ScanRecord;
    now: number;
    onBack(): void;
    onDelete(): void;
}) {
    const [findingsOnly, setFindingsOnly] = useState(true);
    const [limit, setLimit] = useState(PAGE_SIZE);

    const showTriage = Boolean(option("showTriage", true));
    const floor = findingFloor();
    const held = Array.isArray(record.reports) ? record.reports : [];

    const listed = useMemo(() => {
        const rows = findingsOnly
            // An unanswered lookup stays listed under the findings filter: a
            // database that did not answer is a fact about the evidence, and
            // dropping it here would let an outage read as a clean scan.
            ? held.filter(report => rowVerdict(report) >= floor || isUnanswered(report))
            : held;
        return [...rows].sort((a, b) => {
            const delta = rowVerdict(b) - rowVerdict(a);
            if (delta !== 0) return delta;
            return memberName(a.discordId).toLowerCase().localeCompare(memberName(b.discordId).toLowerCase());
        });
    }, [held, findingsOnly, floor]);

    const hidden = held.length - listed.length;
    const page = listed.slice(0, limit);

    const tally = useMemo(() => {
        const counts: Record<number, number> = {};
        for (const verdict of ALL_VERDICTS) counts[verdict] = 0;
        for (const report of held) {
            counts[rowVerdict(report)] = (counts[rowVerdict(report)] ?? 0) + 1;
        }
        return counts;
    }, [held]);

    const left = Math.max(0, recordExpiresAt(record) - now) / 1000;
    const stale = staleIn(held);

    function copyFindings() {
        const head = [
            `RotectorScan - ${record.sourceLabel}`,
            `backend: ${backendLabel(record.backend)}`,
            `scan started: ${absolute(record.startedAt)}`,
            `${count(record.scanned)} answered, ${count(listed.length)} listed`,
            `filter: ${findingsOnly ? "findings" : "all"} (${count(Math.max(0, hidden))} hidden)`,
        ];
        if (record.omitted) {
            head.push(`${count(record.omitted)} member(s) carried no finding and were counted rather than stored.`);
        }
        if (record.truncated) {
            head.push(`${count(record.truncated)} further finding(s) were not stored: the record was full.`);
        }
        if (stale) {
            head.push(`${count(stale)} stored finding(s) are older than 24 hours - each one is dated below, and a re-check is due before acting on it.`);
        }
        if (record.coverageNote) head.push(record.coverageNote);
        if (record.unanswered) {
            head.push(`${count(record.unanswered)} member(s) could not be checked in full - not the same as a clean result.`);
        }

        const text = [
            head.join("\n"),
            "",
            listed.map(memberSummary).join("\n\n"),
            "",
            backendAttribution(record.backend),
            appealSentence([record.backend]),
            historyRetentionNotice(),
        ].join("\n");

        attempt(() => copyWithToast(text, `Copied ${count(listed.length)} member(s).`), undefined);
    }

    return (
        <>
            <Node label="Scan">
                <div className={cl("stack")}>
                    <div>
                        <Label>{record.sourceLabel}</Label>
                        <Body>
                            {`${count(record.scanned)} member(s) answered against ${backendLabel(record.backend)}, `}
                            {ago(record.startedAt, now)}
                            {record.finishedAt
                                ? `, taking ${formatDuration(Math.max(0, record.finishedAt - record.startedAt) / 1000)}`
                                : ""}
                            .
                        </Body>
                    </div>
                    <Micro>{`started ${absolute(record.startedAt)}  deleted in ${formatDuration(left)}`}</Micro>
                    {record.coverageNote && <Body measure>{record.coverageNote}</Body>}

                    {/* What a record from beyond the window is worth, said where
                        its findings are read rather than in the footer. */}
                    {stale > 0 && (
                        <div className={cl("stale")} data-verdict={verdictName(Verdict.CAUTION)}>
                            <span className={cl("verdict")} data-verdict={verdictName(Verdict.CAUTION)}>
                                {`${count(stale)} STALE`}
                            </span>
                            <Body measure>
                                {`${count(stale)} of the ${count(held.length)} stored finding(s) were answered more than `}
                                24 hours ago — past the window Rotector's Terms of Use allow. Each row
                                below carries its age. Opening one re-checks the member against the
                                backend, which is the answer to act on.
                            </Body>
                        </div>
                    )}

                    <div className={cl("tally")}>
                        {[...ALL_VERDICTS].reverse().map(verdict => (
                            tally[verdict]
                                ? (
                                    <div className={cl("tally__row")} key={verdictName(verdict)}>
                                        <Meter verdict={verdict} />
                                        <VerdictLine verdict={verdict} meaning={false} />
                                        <span className={cl("num")}>{count(tally[verdict])}</span>
                                    </div>
                                )
                                : null
                        ))}
                    </div>

                    {/* What the record does not contain, stated where the counts
                        are read rather than in a footnote. A stored scan that
                        looks short because most of it was thrown away must say
                        so, or the reader will take the list for the scan. */}
                    {record.omitted > 0 && (
                        <Body measure>
                            {`${count(record.omitted)} of the ${count(record.scanned)} member(s) answered carried no `}
                            finding, so their reports were counted and not stored. A scan of a large
                            server is almost entirely NO DETECTIONS, and writing all of it down would
                            put megabytes on disk to record that nothing was found. NO DETECTIONS is
                            not a clean bill of health either way — it means nothing has been detected
                            yet.
                        </Body>
                    )}
                    {record.truncated > 0 && (
                        <Micro>
                            {`${count(record.truncated)} further finding(s) were not stored: one record holds a `}
                            limited number and the worst were kept. Run the scan again to see them all.
                        </Micro>
                    )}
                    {record.unanswered > 0 && (
                        <Micro>
                            {`${count(record.unanswered)} member(s) could not be checked in full. A lookup that `}
                            did not answer is not a clean result.
                        </Micro>
                    )}

                    <div className={cl("actions")}>
                        <Btn quiet onClick={onBack}>Back to the list</Btn>
                        <Btn disabled={listed.length === 0} onClick={copyFindings}>Copy findings</Btn>
                        <Btn quiet onClick={onDelete}>Delete this scan</Btn>
                    </div>
                </div>
            </Node>

            <Node label="Findings" count={held.length}>
                <div className={cl("stack")}>
                    <div className={cl("actions")}>
                        <Btn quiet={!findingsOnly} onClick={() => setFindingsOnly(true)}>Findings</Btn>
                        <Btn quiet={findingsOnly} onClick={() => setFindingsOnly(false)}>Everything stored</Btn>
                    </div>
                    {/* Always printed, hidden count included, even at zero: the
                        TUI's rule is that nothing is dropped silently. */}
                    <Micro>
                        filter: {findingsOnly ? "findings" : "all stored"} ({count(Math.max(0, hidden))} hidden)
                        {listed.length > page.length ? `  showing ${count(page.length)} of ${count(listed.length)}` : ""}
                    </Micro>

                    {page.length === 0
                        ? (
                            <Body>
                                {held.length === 0
                                    ? "No member in this scan carried a finding. That is not a clean bill of health — see the counts above for what was actually answered."
                                    : "Nothing matches this filter."}
                            </Body>
                        )
                        : (
                            <ScrollerThin className={cl("scroll")}>
                                <div className={cl("ledger__wrap")}>
                                    <table className={cl("ledger")}>
                                        <thead>
                                            <tr>
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
                                                    guildId={record.guildId}
                                                    backend={record.backend}
                                                    showTriage={showTriage}
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

                    <Micro>
                        A row opens the live report, which re-checks the member against the backend. What
                        is stored here is what was true when the scan ran: the flags, the categories and
                        the first few lines of each reason. The full evidence is not kept — the live
                        report fetches it.
                    </Micro>
                </div>
            </Node>
        </>
    );
}

/**
 * One ledger row.
 *
 * The same shape ScanModal draws, minus the selection column: an action is
 * planned against a live scan, and a stored record is a reading rather than a
 * worklist. Clicking it opens the report modal, which does its own lookup — so
 * nobody kicks somebody on the strength of a record from yesterday without
 * seeing today's answer first.
 */
function LedgerRow({ report, guildId, backend, showTriage }: {
    report: MemberReport;
    guildId?: string;
    /** the service this record's scan actually asked, which stays true whatever
        the backend setting says now */
    backend?: string;
    showTriage: boolean;
}) {
    const id = report.discordId;
    const verdict = rowVerdict(report);
    const worst = attempt(() => worstAccount(report), null);
    const accounts = report.accounts ?? [];

    // The TUI shows one account — the worst, tie-broken on confidence — plus a
    // "+N" overflow marker, rather than a list that would not fit.
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
            <th scope="row">
                <span>{memberName(id)}</span>
                <span className={cl("row__handle")}>{memberHandle(id)}</span>
            </th>
            <td className={cl("ledger__verdict")}>
                <Meter verdict={verdict} />
                <VerdictLine verdict={verdict} meaning={false} />
                {/* Same treatment as the scan ledger's: under the verdict it
                    qualifies, in the CAUTION hue, blank inside the window. */}
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
