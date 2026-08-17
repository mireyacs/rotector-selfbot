/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The result types a lookup produces, and the derived readings every surface
 * agrees on. Ported from the dataclasses in rsb/rotector.py.
 *
 * These live in their own module rather than in api/rotector.ts because
 * api/triage.ts reads them and api/rotector.ts does not read triage — putting
 * them in the client would make that a cycle.
 *
 * The staleness helpers at the bottom live here for the same reason and one
 * more: they must not import settings.ts. Staleness is a fact about a timestamp
 * and not about configuration — a report answered more than 24 hours ago is
 * stale whether or not the operator switched `retainBeyondTerms` on, and that
 * is deliberate. The setting decides how long a finding is *kept*; it cannot
 * decide how old it is, and a module that read the setting to answer the
 * question could be made to say a two-day-old finding is current.
 */

import { Signal, Verdict, verdictForFlag, worstVerdict } from "./verdict";

export interface TrackedServer {
    serverId: string;
    serverName: string;
    joinedAt: number | null;
    firstSeenAt: number | null;
    isTase: boolean;
    inGracePeriod: boolean;
}

export interface RobloxAccount {
    userId: number;
    username: string;
    detectedAt: number | null;
    /** verification sources for the Discord <-> Roblox link */
    sources: number[];
    // filled in by the second hop
    flagType: number | null;
    category: number | null;
    confidence: number | null;
    /** reason name -> { message, evidence: string[], confidence } — free text, never parsed */
    reasons: Record<string, RotectorReason>;
    lastUpdated: number | null;
}

export interface RotectorReason {
    message?: string;
    evidence?: string[];
    confidence?: number;
    [key: string]: unknown;
}

/**
 * Everything the backend knows about one Discord member.
 *
 * Rotector fills `accounts` and `servers`. A backend that answers per source
 * rather than per Roblox account fills `signals` instead, and may fill both
 * when a source does name an account.
 */
export interface MemberReport {
    discordId: string;
    accounts: RobloxAccount[];
    servers: TrackedServer[];
    error: string | null;
    /** per-source findings; always empty under the Rotector backend */
    signals: Signal[];
    /** epoch ms this report was produced, for the 24h retention ceiling */
    fetchedAt: number;
}

export function emptyReport(discordId: string, error: string | null = null): MemberReport {
    return {
        discordId,
        accounts: [],
        servers: [],
        error,
        signals: [],
        fetchedAt: Date.now(),
    };
}

export function accountVerdict(account: RobloxAccount): Verdict {
    return verdictForFlag(account.flagType);
}

export function profileUrl(account: RobloxAccount): string {
    return `https://www.roblox.com/users/${account.userId}/profile`;
}

/** The worst verdict across every account and every signal. UNKNOWN when empty. */
export function reportVerdict(report: MemberReport): Verdict {
    const found = [
        ...report.accounts.map(accountVerdict),
        ...report.signals.map(s => s.verdict),
    ];
    if (!found.length) return Verdict.UNKNOWN;
    return worstVerdict(found);
}

/** The account the member is judged on: worst verdict, then highest confidence. */
export function worstAccount(report: MemberReport): RobloxAccount | null {
    if (!report.accounts.length) return null;
    return report.accounts.reduce((best, acc) => {
        const bv = accountVerdict(best);
        const av = accountVerdict(acc);
        if (av !== bv) return av > bv ? acc : best;
        return (acc.confidence ?? 0) > (best.confidence ?? 0) ? acc : best;
    });
}

/** Only the sources that actually said something, worst first. */
export function flaggedSignals(report: MemberReport): Signal[] {
    return report.signals.filter(s => s.flagged).sort((a, b) => b.verdict - a.verdict);
}

/** Accounts worst-first, the order the report modal lists them in. */
export function sortedAccounts(report: MemberReport): RobloxAccount[] {
    return [...report.accounts].sort((a, b) => {
        const d = accountVerdict(b) - accountVerdict(a);
        return d !== 0 ? d : (b.confidence ?? 0) - (a.confidence ?? 0);
    });
}

// --------------------------------------------------------------------------
// staleness
//
// Ported from rsb/rotector.py's is_stale / report_age / describe_age /
// stale_note. Written once, here, so the mark's tooltip, the report modal, the
// ledger, the scan summary, the history list and every export format say the
// same thing about the same finding — a reader who meets a stale row in an
// exported file did not choose to keep the data past the window and is owed the
// reason it might no longer be true.
// --------------------------------------------------------------------------

/**
 * Rotector's Terms of Use #1 allow their responses to be kept for 24 hours, so
 * this is the line past which a finding is no longer something the terms cover
 * — and, more to the point for whoever is reading it, no longer a claim about
 * now.
 */
export const TERMS_WINDOW_MS = 24 * 3600 * 1000;

/** `Date.now()`, unless a caller is pinning the clock for a whole render pass. */
function clock(now?: number): number {
    return typeof now === "number" && isFinite(now) ? now : Date.now();
}

/**
 * How long ago the data behind `report` arrived, in milliseconds.
 *
 * Never negative. A `fetchedAt` in the future is a clock that moved, not a
 * finding from the future, and reporting a negative age would print "-3 hours"
 * somewhere.
 */
export function reportAge(report: MemberReport | null | undefined, now?: number): number {
    const born = Number((report as any)?.fetchedAt);
    if (!isFinite(born) || born <= 0) return 0;
    return Math.max(0, clock(now) - born);
}

/**
 * Whether this finding is older than the terms' 24-hour window.
 *
 * A stale finding is not a wrong one, but it is a weaker claim than a fresh one
 * and has to be shown as such: flag types change, appeals succeed, and an
 * account that cleared its violations yesterday still reads as flagged out of a
 * cache that outlived the window.
 *
 * A report with no readable `fetchedAt` reads as fresh rather than stale, and
 * that is the safe way round here: `emptyReport()` stamps every report on
 * creation, so an unreadable timestamp means a hand-built or malformed object
 * rather than an old one, and labelling those stale would put the word on
 * findings that had just arrived until nobody read it any more.
 */
export function isStale(report: MemberReport | null | undefined, now?: number): boolean {
    const born = Number((report as any)?.fetchedAt);
    if (!isFinite(born) || born <= 0) return false;
    return clock(now) - born >= TERMS_WINDOW_MS;
}

/** An age a person can read: "3 days", "31 hours", "12 minutes". */
export function describeAge(ms: number): string {
    const seconds = Math.max(0, Number(ms) || 0) / 1000;
    if (seconds >= 2 * 86400) return `${Math.floor(seconds / 86400)} days`;
    if (seconds >= 3600) {
        const hours = Math.floor(seconds / 3600);
        return `${hours} hour${hours === 1 ? "" : "s"}`;
    }
    const minutes = Math.floor(seconds / 60);
    return `${minutes} minute${minutes === 1 ? "" : "s"}`;
}

/** The same age where there is only room for a column or two: "3d", "26h", "5m". */
export function shortAge(ms: number): string {
    const seconds = Math.max(0, Number(ms) || 0) / 1000;
    if (seconds >= 86400) return `${Math.floor(seconds / 86400)}d`;
    if (seconds >= 3600) return `${Math.floor(seconds / 3600)}h`;
    return `${Math.floor(seconds / 60)}m`;
}

/**
 * How old this finding is, for display: "31 hours", "2 days".
 *
 * Answers for a fresh report too — a surface that wants to print the age
 * unconditionally should not have to ask twice — so pair it with `isStale` when
 * the point is the warning rather than the number.
 */
export function staleFor(report: MemberReport | null | undefined, now?: number): string {
    return describeAge(reportAge(report, now));
}

/**
 * The sentence every surface uses for a stale finding, or "" when it is fresh.
 *
 * This is the same argument the plugin already makes about "did not answer" and
 * about NO DETECTIONS: show the number, and show what it does not mean. A
 * finding served from beyond the window is shown with its age and with the
 * reason it may no longer be true, everywhere it is shown or written.
 */
export function staleNote(report: MemberReport | null | undefined, now?: number): string {
    if (!isStale(report, now)) return "";
    return (
        `STALE - answered ${staleFor(report, now)} ago, past the 24-hour window ` +
        "Rotector's Terms of Use allow. Flag types change and appeals succeed, so " +
        "re-check before acting on this."
    );
}

/** The same warning where there is room for a few characters: "STALE 2d", or "". */
export function staleTag(report: MemberReport | null | undefined, now?: number): string {
    if (!isStale(report, now)) return "";
    return `STALE ${shortAge(reportAge(report, now))}`;
}
