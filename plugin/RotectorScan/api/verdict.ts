/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Flag-type semantics from the Rotector API, mapped onto UI verdicts.
 * A literal transliteration of rsb/verdict.py. Reference: https://roscoe.rotector.com/docs
 *
 * Two rules from Rotector's Terms of Use shape this module and must not be
 * relaxed:
 *
 *  - Rule 3 — a user whose flag type is `Unflagged` must NOT be presented as
 *    "Safe". It only means nothing has been detected *yet*. Hence the
 *    NO_DETECTIONS verdict name.
 *  - The docs' own warning — do not treat every non-zero flag type as
 *    inappropriate. Only types 1 (Flagged) and 2 (Confirmed) are safe to action
 *    automatically; 0, 3, 4, 6 and 8 are explicitly not actionable.
 */

export const ATTRIBUTION = "Data: Rotector (https://rotector.com) - appeals at rotector.com";

export const enum FlagType {
    UNFLAGGED = 0,
    FLAGGED = 1,
    CONFIRMED = 2,
    QUEUED = 3,
    PROVISIONAL = 4,
    MIXED = 5,
    PAST_OFFENDER = 6,
    REDACTED = 8,
}

export interface FlagMeta {
    /** display name */
    name: string;
    /** documented by Rotector as safe to action on its own */
    actionable: boolean;
    description: string;
}

/** flag type -> (display name, actionable, description) */
export const FLAG_META: Record<number, FlagMeta> = {
    0: { name: "Unflagged", actionable: false, description: "No violations detected yet" },
    1: { name: "Flagged", actionable: true, description: "System-detected violations with high accuracy" },
    2: { name: "Confirmed", actionable: true, description: "Violations manually verified by a moderator" },
    3: { name: "Queued", actionable: false, description: "Submitted for review, not an inappropriate user" },
    4: { name: "Provisional Flag", actionable: false, description: "Flagged but reasons hidden pending human review" },
    5: { name: "Mixed", actionable: false, description: "Some inappropriate activity but insufficient evidence" },
    6: { name: "Past Offender", actionable: false, description: "Previously flagged but has since cleared violations" },
    8: { name: "Redacted", actionable: false, description: "Reasons and evidence withheld under a privacy restriction" },
};

export const CATEGORY_NAMES: Record<number, string> = {
    1: "CSAM",
    2: "Sexual",
    3: "Kink",
    4: "Raceplay",
    5: "Condo",
    6: "Other",
};

/** verification source for a Discord <-> Roblox connection */
export const SOURCE_NAMES: Record<number, string> = {
    0: "Deprecated",
    1: "Unknown",
    2: "Profile",
};

/** Ordered severity, so `worstVerdict()` picks the worst of several accounts. */
export const enum Verdict {
    UNKNOWN = 0,
    NO_DETECTIONS = 1,
    INFO = 2,
    CAUTION = 3,
    THREAT = 4,
}

/**
 * The enum member name, used as the `data-verdict` attribute in markup — the
 * same convention rsb/htmlrender.py emits. Sucrase compiles `const enum` to a
 * plain object with no reverse mapping, so the names live here explicitly.
 */
export const VERDICT_NAMES: Record<Verdict, string> = {
    [Verdict.UNKNOWN]: "UNKNOWN",
    [Verdict.NO_DETECTIONS]: "NO_DETECTIONS",
    [Verdict.INFO]: "INFO",
    [Verdict.CAUTION]: "CAUTION",
    [Verdict.THREAT]: "THREAT",
};

export const ALL_VERDICTS: Verdict[] = [
    Verdict.UNKNOWN,
    Verdict.NO_DETECTIONS,
    Verdict.INFO,
    Verdict.CAUTION,
    Verdict.THREAT,
];

export interface VerdictMeta {
    /** short label, as shown in the ledger */
    label: string;
    /** one-line meaning, shown in tooltips and under the verdict */
    meaning: string;
}

/** verdict -> (short label, one-line meaning) */
export const VERDICT_META: Record<Verdict, VerdictMeta> = {
    [Verdict.UNKNOWN]: {
        label: "UNKNOWN",
        meaning: "No linked Roblox account is known to Rotector - nothing can be said.",
    },
    [Verdict.NO_DETECTIONS]: {
        label: "NO DETECTIONS",
        meaning: "Linked account(s) known but unflagged. Not a clean bill of health.",
    },
    [Verdict.INFO]: {
        label: "INFO",
        meaning: "Queued / past offender / redacted. Informational only, not actionable.",
    },
    [Verdict.CAUTION]: {
        label: "CAUTION",
        meaning: "Mixed or provisional signals. Review manually before acting.",
    },
    [Verdict.THREAT]: {
        label: "THREAT",
        meaning: "Flagged or Confirmed violations. Safe to action per Rotector.",
    },
};

/** flag type -> verdict */
const FLAG_TO_VERDICT: Record<number, Verdict> = {
    0: Verdict.NO_DETECTIONS,
    1: Verdict.THREAT,
    2: Verdict.THREAT,
    3: Verdict.INFO,
    4: Verdict.CAUTION,
    5: Verdict.CAUTION,
    6: Verdict.INFO,
    8: Verdict.INFO,
};

export function flagName(flagType: number | null | undefined): string {
    if (flagType == null) return "Untracked";
    return FLAG_META[flagType]?.name ?? `Unknown(${flagType})`;
}

export function flagDescription(flagType: number | null | undefined): string {
    if (flagType == null) return "";
    return FLAG_META[flagType]?.description ?? "";
}

export function flagIsActionable(flagType: number | null | undefined): boolean {
    if (flagType == null) return false;
    return FLAG_META[flagType]?.actionable ?? false;
}

export function verdictForFlag(flagType: number | null | undefined): Verdict {
    if (flagType == null) return Verdict.UNKNOWN;
    return FLAG_TO_VERDICT[flagType] ?? Verdict.INFO;
}

/**
 * Whether Rotector is asserting *something* about this flag type.
 *
 * Types 1, 2, 4 and 5 — the ones that reach CAUTION or THREAT above. The
 * distinction triage.ts needs is not "actionable or not" but "an accusation or
 * not": Rotector describes type 3 as *not an inappropriate user* and type 6 as
 * having *since cleared*, so those are not weak accusations, they are not
 * accusations, and no amount of corroboration from elsewhere may promote them.
 *
 * Derived from FLAG_TO_VERDICT rather than listed again, so a change to the
 * tiering cannot leave a second table behind disagreeing with it. An
 * unrecognised type falls to INFO and is therefore not suspect: a type added
 * after this was written could as easily mean "cleared" as "confirmed", and
 * guessing would be inventing a claim Rotector has not made.
 */
export function flagIsSuspect(flagType: number | null | undefined): boolean {
    return verdictForFlag(flagType) >= Verdict.CAUTION;
}

export function verdictLabel(verdict: Verdict): string {
    return VERDICT_META[verdict].label;
}

export function verdictMeaning(verdict: Verdict): string {
    return VERDICT_META[verdict].meaning;
}

export function verdictName(verdict: Verdict): string {
    return VERDICT_NAMES[verdict];
}

export function categoryName(category: number | null | undefined): string | null {
    if (category == null) return null;
    return CATEGORY_NAMES[category] ?? `Category ${category}`;
}

export function sourceNames(sources: number[] | null | undefined): string {
    if (!sources?.length) return "unknown";
    return sources.map(s => SOURCE_NAMES[s] ?? String(s)).join(", ");
}

/** The worst of several verdicts. UNKNOWN when there are none. */
export function worstVerdict(verdicts: Verdict[]): Verdict {
    let worst = Verdict.UNKNOWN;
    for (const v of verdicts) if (v > worst) worst = v;
    return worst;
}

// --------------------------------------------------------------------------
// multi-source findings
// --------------------------------------------------------------------------

/**
 * One named source's answer about one Discord account.
 *
 * Rotector answers per *Roblox account*, which is why a report normally carries
 * RobloxAccount objects. A backend that aggregates several services answers per
 * *source* instead, and some of those sources flag a Discord id with no Roblox
 * link attached at all. Those findings have nowhere to live on an account, so
 * they live here, and `reportVerdict()` takes the worst of both.
 *
 * `verdict` is set by whoever builds the signal rather than derived, because
 * what a flag *means* differs per source: only Rotector publishes flag types
 * with documented actionability, so only Rotector's can reach THREAT.
 *
 * `signals` is populated whenever the backend is Okappiki, whose one response
 * carries all three services, and is always empty under Rotector — which is why
 * a report holding any signal is by itself proof of which backend answered.
 *
 * An earlier version of this comment said the aggregating backend was
 * unreachable from a renderer because it sent no CORS headers. That was wrong,
 * and the mistake is worth recording: the probe behind it used an *invalid*
 * `action`, and okappiki.com's error path returns before it writes the headers
 * its valid actions do send. Both `vencord_full_check` and `vencord_check_flag`
 * answer with `access-control-allow-origin: *`.
 */
export interface Signal {
    /** the service that said it: "rotector", "okappiki", "mococo" */
    source: string;
    flagged: boolean;
    verdict: Verdict;
    /** the source's own words, unedited — shown rather than parsed */
    reason?: string;
    /** mococo publishes a numeric score; the others do not */
    score?: number | null;
    robloxId?: number | null;
    robloxUsername?: string | null;
    flagType?: number | null;
}
