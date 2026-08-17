/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Deterministic triage: cautious, or actually ban?
 *
 * A literal transliteration of rsb/triage.py, rules and wording included. The
 * Python module's own explanation, which still governs this file:
 *
 * api/verdict.ts grades *evidence* — what the databases found and how strongly.
 * This module answers the separate question a moderator actually has in front
 * of them: should staff be cautious with this person, or does the evidence
 * support banning them?
 *
 * Deterministic here means literally that: an ordered table of rules, evaluated
 * top to bottom, first match wins, and the result names the rule that fired so
 * a moderator can look it up and disagree with it. No scoring heuristics, no
 * reason-text keyword matching, no thresholds pulled from nowhere.
 *
 * What each party actually publishes
 * ----------------------------------
 *  - Rotector publishes a flagType with *documented actionability*. Its docs
 *    name types 1 and 2 as safe to action and warn against treating every
 *    non-zero type as inappropriate. This is the only field in either backend
 *    carrying a vendor's claim about whether you may act on it.
 *  - mococo (TASE) publishes scoreSum — interaction volume, not severity, with
 *    no published scale. It orders users; it does not grade them.
 *  - Detective Okappiki publishes a boolean and a list of server names.
 *
 * So the table lands where the evidence lands: only Rotector can support a ban
 * on its own, and everything else is a reason for a person to look — with rule
 * R2 as the exception that lets independent corroboration carry a Rotector flag
 * that Rotector itself declined to conclude from.
 *
 * `reason` strings are never parsed. They are free text from three services and
 * they carry server names *chosen by the people being flagged*. Reasons are
 * shown verbatim, and nothing else.
 */

import { MemberReport, worstAccount } from "./types";
import { flagIsActionable, flagIsSuspect, flagName, Signal, Verdict } from "./verdict";

/** the parties an aggregating response can carry, in the order they are reported */
export const PARTIES = ["rotector", "okappiki", "mococo"] as const;
export type Party = (typeof PARTIES)[number];

export const PARTY_LABEL: Record<string, string> = {
    rotector: "Rotector",
    okappiki: "Detective Okappiki",
    mococo: "mococo (TASE)",
};

export const APPEAL_LINKS: Record<string, string> = {
    rotector: "https://rotector.com",
    okappiki: "https://okappiki.com/appeal",
    mococo: "https://moco-co.org",
};

/** Ordered by how much it asks of a moderator. */
export const enum Recommendation {
    /** nothing is known about this member at all. Not a clean result: a blank one */
    NOTHING = 0,
    /** every consulted database answered and none flagged the member */
    NO_FINDING = 1,
    /** nothing flagged, but a database that should have answered did not */
    RECHECK = 2,
    /** flagged only by findings the database itself calls informational. Not an accusation */
    INFORMATIONAL = 3,
    /** flagged by real evidence, but nothing that supports acting on its own */
    CAUTION = 4,
    /** the evidence supports acting */
    BAN = 5,
}

export interface RecommendationMeta {
    label: string;
    meaning: string;
}

/** recommendation -> (label, one-line meaning) */
export const RECOMMENDATION_META: Record<Recommendation, RecommendationMeta> = {
    [Recommendation.NOTHING]: {
        label: "NOTHING KNOWN",
        meaning: "No database has anything on this member. Not the same as nothing found.",
    },
    [Recommendation.NO_FINDING]: {
        label: "NO FINDING",
        meaning: "Every database answered and none flagged them. Not a clean bill of health.",
    },
    [Recommendation.RECHECK]: {
        label: "RECHECK",
        meaning: "A database did not answer, so this member has not actually been checked.",
    },
    [Recommendation.INFORMATIONAL]: {
        label: "INFORMATIONAL",
        meaning: "On record, but for something the database says is not an accusation.",
    },
    [Recommendation.CAUTION]: {
        label: "CAUTION",
        meaning: "Real evidence, but none that supports acting on its own. A person decides.",
    },
    [Recommendation.BAN]: {
        label: "BAN SUPPORTED",
        meaning: "The evidence meets the bar for acting.",
    },
};

export function recommendationLabel(rec: Recommendation): string {
    return RECOMMENDATION_META[rec].label;
}

export function recommendationMeaning(rec: Recommendation): string {
    return RECOMMENDATION_META[rec].meaning;
}

/** The operator's own bar, separate from anything a vendor claims. */
export interface TriagePolicy {
    /**
     * Rule R3: let Okappiki and mococo, agreeing with each other and with no
     * Rotector flag, support a ban on their own. Off by default and
     * deliberately so — neither service publishes a claim that its sighting is
     * safe to act on, so switching this on is the operator's policy call and
     * not something either service told them to make.
     */
    banOnCorroboratedSighting: boolean;
    /**
     * R3's floor on mococo's scoreSum. TASE publishes no scale for this number
     * and it measures interaction volume rather than severity, so there is no
     * correct value. 40 is what the sample records this was built against
     * carried; it is not a vendor threshold.
     */
    minMococoScore: number;
}

export const DEFAULT_POLICY: TriagePolicy = {
    banOnCorroboratedSighting: false,
    minMococoScore: 40,
};

/** One database's answer about one member, or its silence. */
export interface PartyFinding {
    party: string;
    /**
     * did this database answer at all? A database that did not answer and one
     * that answered "not flagged" are different facts, and flattening them
     * would let an outage read as a clean result.
     */
    answered: boolean;
    flagged: boolean;
    /** Rotector only */
    flagType: number | null;
    /** mococo only; interaction volume across detected guilds, not severity */
    score: number | null;
    /** the database's own words, carried through unedited and never parsed */
    reason: string;
    /**
     * does this database have a record of the account at all? "Rotector knows
     * of no linked Roblox account" and "Rotector knows the account and has not
     * flagged it" are both unflagged, and they are not the same fact.
     */
    known: boolean;
}

function makeFinding(party: string, partial: Partial<PartyFinding> = {}): PartyFinding {
    return {
        party,
        answered: false,
        flagged: false,
        flagType: null,
        score: null,
        reason: "",
        known: true,
        ...partial,
    };
}

export function findingLabel(finding: PartyFinding): string {
    return PARTY_LABEL[finding.party] ?? finding.party;
}

/**
 * Whether this flag is an accusation rather than a note.
 *
 * Rotector's types 3, 6 and 8 are flags that Rotector explicitly says are *not*
 * findings against the member — queued for review, since cleared, withheld.
 * Letting those reach CAUTION would matter here in a way it does not elsewhere,
 * because in this project CAUTION is the tier where acting becomes possible
 * behind a second confirmation. A sighting from either unverified database is
 * CAUTION-strength by construction.
 */
export function findingIsMaterial(finding: PartyFinding): boolean {
    if (!finding.flagged) return false;
    if (finding.party === "rotector") return flagIsSuspect(finding.flagType);
    return true;
}

// --------------------------------------------------------------------------
// reading a report
// --------------------------------------------------------------------------

/**
 * One finding per database the active backend actually consulted.
 *
 * Which databases those are is read off the report rather than configured: a
 * report carrying `signals` came from an aggregating backend, which asks all
 * three every time, so all three are expected and a missing one is a database
 * that did not answer. A report carrying only `accounts` came from the Rotector
 * backend, which asks Rotector alone — the other two are not silent, they were
 * never consulted, and reporting them as unanswered would put every clean
 * Rotector scan on RECHECK.
 */
export function findingsFor(report: MemberReport): PartyFinding[] {
    const signals: Signal[] = report.signals ?? [];

    if (signals.length) {
        const bySource = new Map<string, Signal>(signals.map(s => [s.source, s]));
        return PARTIES.map(party => {
            const sig = bySource.get(party);
            return makeFinding(party, {
                answered: sig != null,
                flagged: sig ? Boolean(sig.flagged) : false,
                flagType: sig ? sig.flagType ?? null : null,
                score: sig ? sig.score ?? null : null,
                reason: sig ? sig.reason ?? "" : "",
            });
        });
    }

    // Rotector backend: one database, answering per Roblox account. The worst
    // account is the one the member is judged on, the same account the results
    // ledger and the report modal already name.
    const account = worstAccount(report);
    if (account == null) {
        // Rotector answered; it simply knows of no linked Roblox account. That
        // supports nothing at all, which is not the same as a check coming back
        // empty, so it is recorded as answered-but-unknown rather than clean.
        return [makeFinding("rotector", { answered: !report.error, flagged: false, known: false })];
    }

    // A deliberate divergence from rsb/triage.py, which hardcodes
    // `answered=True` here (rsb/triage.py, the branch after the `account is
    // None` return). The Python is wrong and this is the corrected form: the
    // scan takes two hops, and when the Discord hop succeeds but the Roblox
    // flag hop fails, api/rotector.ts sets `report.error` and leaves every
    // account at `flagType: null`. Reading that as "answered" would make
    // `flagged` false with nothing behind it, and the table would fall all the
    // way through to R8 — "every database answered and none of them flagged
    // this member" — which is a database that did not answer being flattened
    // into "not flagged", the exact failure this module exists to prevent.
    // With `answered: !report.error` a failed flag lookup is recorded as the
    // unanswered check it was, exactly as the no-account branch three lines up
    // already does: R6 RECHECK under a backend where other databases did
    // answer, R0 NOTHING KNOWN under this one, where Rotector is the only
    // database consulted and its silence leaves nothing behind. Never R8.
    return [
        makeFinding("rotector", {
            answered: !report.error,
            flagged: account.flagType != null && account.flagType !== 0,
            flagType: account.flagType,
            reason: Object.keys(account.reasons ?? {}).slice(0, 3).join(", "),
            // A second divergence from rsb/triage.py, for the same reason as
            // the first. `flagType` is null when the Roblox endpoint returned
            // no record for a linked account at all — which is not the same as
            // returning type 0, Unflagged. The verdict layer already reads that
            // as UNKNOWN, "nothing can be said"; hardcoding `known: true` here
            // made triage answer R8, "every database answered and none of them
            // flagged this member", for the same member. The two layers cannot
            // be allowed to disagree about whether anyone was checked, and of
            // the two answers only NOTHING KNOWN declines to assert a clean
            // check, so that is the one to land on.
            known: account.flagType != null,
        }),
    ];
}

// --------------------------------------------------------------------------
// the decision table
// --------------------------------------------------------------------------

interface Context {
    findings: PartyFinding[];
    byParty: Record<string, PartyFinding>;
    answered: string[];
    flagged: string[];
    /** the subset of `flagged` whose flag is an accusation rather than a note */
    material: string[];
    unanswered: string[];
    /** parties other than Rotector carrying a material flag */
    corroborating: string[];
    rotectorFlag: number | null;
    rotectorActionable: boolean;
    rotectorSuspect: boolean;
    mococoScore: number;
    policy: TriagePolicy;
}

export interface Rule {
    id: string;
    recommendation: Recommendation;
    /** what this rule says, in one line; the settings screen renders these */
    summary: string;
    when(c: Context): boolean;
    headline(c: Context): string;
}

function names(parties: string[]): string {
    const labels = parties.map(p => PARTY_LABEL[p] ?? p);
    if (!labels.length) return "no database";
    if (labels.length === 1) return labels[0];
    return `${labels.slice(0, -1).join(", ")} and ${labels[labels.length - 1]}`;
}

/**
 * The whole algorithm, as data. Evaluated in order; the first match wins.
 *
 * Read top to bottom it says: Rotector's actionable types carry a ban alone;
 * Rotector's inconclusive types carry one once an independent database saw the
 * same member; two unverified sightings carry one only if the operator has said
 * they may; anything else flagged is a person's decision; and a check that did
 * not finish is not a clean check.
 */
export const RULES: Rule[] = [
    {
        id: "R0",
        recommendation: Recommendation.NOTHING,
        summary: "No database answered.",
        when: c => c.answered.length === 0,
        headline: () =>
            "No database answered, so nothing was learned about this member. " +
            "That is a failed check, not a clean one.",
    },
    {
        id: "R1",
        recommendation: Recommendation.BAN,
        summary:
            "Rotector reports flag type 1 (Flagged) or 2 (Confirmed) - the types " +
            "Rotector documents as safe to action.",
        when: c => c.rotectorActionable,
        headline: c =>
            `Rotector reports ${flagName(c.rotectorFlag)} (flag type ${c.rotectorFlag}), ` +
            "which Rotector documents as safe to action on its own.",
    },
    {
        id: "R2",
        recommendation: Recommendation.BAN,
        summary:
            "Rotector flagged them at an inconclusive type (4 Provisional, 5 Mixed) " +
            "and at least one other database independently flagged them too.",
        when: c => c.rotectorSuspect && !c.rotectorActionable && c.corroborating.length > 0,
        headline: c =>
            `Rotector reports ${flagName(c.rotectorFlag)}, which it declines to conclude ` +
            `from on its own - but ${names(c.corroborating)} independently flagged the ` +
            "same account. The corroboration is what Rotector said was missing.",
    },
    {
        id: "R3",
        recommendation: Recommendation.BAN,
        summary:
            "Both unverified databases flagged them and the mococo interaction score " +
            "meets your threshold. Off unless you switch it on.",
        when: c =>
            c.policy.banOnCorroboratedSighting &&
            c.byParty.okappiki.flagged &&
            c.byParty.mococo.flagged &&
            c.mococoScore >= c.policy.minMococoScore,
        headline: c =>
            "Both Detective Okappiki and mococo flagged them, and the mococo " +
            `interaction score is ${c.mococoScore}, at or above the ` +
            `${c.policy.minMococoScore} you set. Neither service publishes a claim ` +
            "that its sighting is safe to act on - this bar is yours.",
    },
    {
        id: "R4",
        recommendation: Recommendation.CAUTION,
        summary: "Flagged by real evidence, but nothing that supports acting on its own.",
        when: c => c.material.length > 0,
        headline: c =>
            `${c.material.length} of the ${c.answered.length} databases that answered ` +
            `flagged them (${names(c.material)}), but none of it meets the bar for ` +
            "acting on its own.",
    },
    {
        id: "R5",
        recommendation: Recommendation.INFORMATIONAL,
        summary:
            "Flagged only by types the database itself calls informational - queued, " +
            "since cleared, or withheld. Not an accusation.",
        when: c => c.flagged.length > 0,
        headline: c =>
            `On record, but only as ${flagName(c.byParty.rotector.flagType)} - which ` +
            "Rotector documents as not a finding against the member. There is nothing " +
            "here to act on.",
    },
    {
        id: "R6",
        recommendation: Recommendation.RECHECK,
        summary: "Nothing flagged, but a database did not answer.",
        when: c => c.unanswered.length > 0,
        headline: c =>
            `${names(c.unanswered)} did not answer, so this member has not actually ` +
            "been checked against every database. Run it again before drawing anything " +
            "from it.",
    },
    {
        id: "R7",
        recommendation: Recommendation.NOTHING,
        summary: "Every database answered, and none of them has a record of the account.",
        when: c => !c.findings.some(f => f.known),
        headline: c =>
            `No linked Roblox account is known to ${names(c.answered)}. There is no ` +
            "finding here at all - nothing supports acting on them.",
    },
    {
        id: "R8",
        recommendation: Recommendation.NO_FINDING,
        summary: "Every database answered and none flagged them.",
        when: () => true,
        headline: () =>
            "Every database answered and none of them flagged this member. That is not " +
            "evidence they are safe - only that nothing has been detected yet.",
    },
];

/** What the rule table decided, and everything it decided it from. */
export interface Triage {
    recommendation: Recommendation;
    /** the id of the rule that fired, e.g. "R2" — the audit handle */
    rule: string;
    /** one sentence stating what was decided and on what */
    headline: string;
    /** the ordered facts the decision rests on, one line per database */
    basis: string[];
    findings: PartyFinding[];
    answered: string[];
    flagged: string[];
    /** the databases whose flag is an accusation rather than a note */
    material: string[];
}

export function triageLabel(t: Triage): string {
    return recommendationLabel(t.recommendation);
}

/** Whether acting is supported without an override. */
export function supportsAction(t: Triage): boolean {
    return t.recommendation === Recommendation.BAN;
}

export function flaggedFindings(t: Triage): PartyFinding[] {
    return t.findings.filter(f => f.flagged);
}

function basisLines(ctx: Context): string[] {
    const lines: string[] = [];
    for (const finding of ctx.findings) {
        const label = findingLabel(finding);
        if (!finding.answered) {
            lines.push(`${label}: did not answer.`);
            continue;
        }
        if (!finding.flagged) {
            lines.push(`${label}: no detection.`);
            continue;
        }

        if (finding.party === "rotector") {
            const where =
                finding.flagType == null ? "no flag type given" : `flag type ${finding.flagType}`;
            let stance: string;
            if (flagIsActionable(finding.flagType)) {
                stance = "Rotector documents this type as safe to action.";
            } else if (flagIsSuspect(finding.flagType)) {
                stance = "Rotector does not consider this type conclusive on its own.";
            } else {
                stance = "Rotector documents this type as not actionable.";
            }
            lines.push(`${label}: flagged - ${flagName(finding.flagType)} (${where}). ${stance}`);
        } else if (finding.party === "mococo") {
            const score =
                finding.score == null
                    ? "no interaction score given"
                    : `interaction score ${finding.score}`;
            lines.push(
                `${label}: flagged - ${score}. TASE defines this as how much the member ` +
                "interacted with the detected servers, not how severe they are, and " +
                "publishes no scale for it."
            );
        } else {
            lines.push(
                `${label}: flagged. Publishes a sighting only, with no severity or ` +
                "actionability claim attached."
            );
        }
    }
    return lines;
}

/** Classify one member report. Never throws. */
export function triage(report: MemberReport, policy: TriagePolicy = DEFAULT_POLICY): Triage {
    const findings = findingsFor(report);
    const byParty: Record<string, PartyFinding> = {};
    for (const f of findings) byParty[f.party] = f;
    // a party the backend never consulted is absent from `findings`; asking for
    // it here must not throw, so unconsulted reads as an unanswered non-finding
    for (const party of PARTIES) byParty[party] ??= makeFinding(party);

    const flagged = findings.filter(f => f.flagged).map(f => f.party);
    const material = findings.filter(findingIsMaterial).map(f => f.party);
    const rotector = byParty.rotector;
    const rotectorFlag = rotector.flagged ? rotector.flagType : null;

    const ctx: Context = {
        findings,
        byParty,
        answered: findings.filter(f => f.answered).map(f => f.party),
        flagged,
        material,
        unanswered: findings.filter(f => !f.answered).map(f => f.party),
        corroborating: material.filter(p => p !== "rotector"),
        rotectorFlag,
        rotectorActionable: rotector.flagged && flagIsActionable(rotector.flagType),
        rotectorSuspect: rotector.flagged && flagIsSuspect(rotector.flagType),
        mococoScore: byParty.mococo.score ?? 0,
        policy,
    };

    const rule = RULES.find(r => r.when(ctx))!;
    return {
        recommendation: rule.recommendation,
        rule: rule.id,
        headline: rule.headline(ctx),
        basis: basisLines(ctx),
        findings,
        answered: [...ctx.answered],
        flagged: [...ctx.flagged],
        material: [...ctx.material],
    };
}

/** The strongest recommendation across several members, for a bulk preview. */
export function worstRecommendation(triages: Triage[]): Recommendation {
    if (!triages.length) return Recommendation.NOTHING;
    return triages.reduce<Recommendation>(
        (worst, t) => (t.recommendation > worst ? t.recommendation : worst),
        Recommendation.NOTHING
    );
}

/** recommendations that mean "a database said something about this member" */
export const FINDING_RECOMMENDATIONS = [Recommendation.CAUTION, Recommendation.BAN];

/**
 * The evidence tier a recommendation implies, for sorting alongside verdicts.
 *
 * Kept deliberately separate from Verdict itself: a verdict describes what a
 * database found, a recommendation describes what a moderator should do about
 * it, and the whole point of this module is that the two stopped being the same
 * thing.
 */
export function verdictFloor(rec: Recommendation): Verdict {
    switch (rec) {
        case Recommendation.NOTHING: return Verdict.UNKNOWN;
        case Recommendation.NO_FINDING: return Verdict.NO_DETECTIONS;
        case Recommendation.RECHECK: return Verdict.INFO;
        case Recommendation.INFORMATIONAL: return Verdict.INFO;
        case Recommendation.CAUTION: return Verdict.CAUTION;
        case Recommendation.BAN: return Verdict.THREAT;
    }
}
