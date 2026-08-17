/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Kick/ban support: reason construction, eligibility, and the act itself.
 *
 * A port of rsb/moderation.py. Its docstring names two rules that are enforced
 * here rather than left to the operator, and both survive the port intact:
 *
 *  - **Attribution.** Rotector's Terms of Use require that any action taken on
 *    their data attributes Rotector, or links rotector.com so the affected
 *    person can appeal. Every reason therefore carries the appeal link,
 *    including reasons the user typed themselves. `buildReason` appends it, and
 *    `runAction` checks it again on the way out — the last point at which a
 *    reason could still be repaired before Discord writes it down.
 *  - **Actionability.** Rotector documents only flag types 1 (Flagged) and 2
 *    (Confirmed) as safe to action automatically; 0, 3, 4, 6 and 8 are
 *    explicitly not. Acting on anything else is possible but is reported as a
 *    deliberate override, never as routine — `requireThreat` gates it,
 *    `ActionPlan.overrides` names it, and `runAction` refuses to run at all
 *    unless the caller passes the acknowledgement back.
 *
 * Two things the Python does that this port deliberately does not:
 *
 *  - It never sends the notice DM. `rsb/config.py` restricts
 *    `notify_before_action` to bot tokens, in its own words: "a user account
 *    messaging strangers in bulk is what Discord's spam heuristics are built to
 *    catch, and the penalty lands on the operator's own account. With a user
 *    token the option is not offered at all." This plugin acts through the
 *    logged-in account, so the notice is composed (`buildNotice`) and handed to
 *    the moderator to send however they choose, and nothing is sent automatically.
 *  - It does not build the HTTP request. Discord's own action creators are used
 *    so permissions, the audit-log reason and the client's own state updates
 *    work exactly as they do when a moderator clicks Ban in Discord's UI. When
 *    those creators cannot be found, this module refuses to act rather than
 *    hand-rolling a REST call whose audit-log reason it could not verify —
 *    a ban recorded with no attribution is the one failure the terms forbid.
 */

import { Logger } from "@utils/Logger";
import { findByPropsLazy } from "@webpack";
import { GuildMemberStore, GuildStore, PermissionsBits, PermissionStore, UserStore } from "@webpack/common";

import {
    APPEAL_LINKS,
    DEFAULT_POLICY,
    Recommendation,
    triage,
    Triage,
    TriagePolicy,
} from "../api/triage";
import { MemberReport, reportVerdict, worstAccount } from "../api/types";
import { categoryName, flagName, Verdict, verdictLabel } from "../api/verdict";
import { policyFromSettings, settings } from "../settings";

const logger = new Logger("RotectorScan");

/**
 * Discord's own guild action creators.
 *
 * The same module Equicord's own plugins reach for
 * (`src/equicordplugins/clickableRoles/index.tsx` uses this exact filter), and
 * the reason to go through it rather than through `RestAPI`: it is the code
 * path the client itself uses, so permission errors, the audit-log reason and
 * the client's own view of the guild all behave the way they do when the ban
 * comes from Discord's UI.
 */
const GuildActionCreators = findByPropsLazy("requestMembersById", "banUser");

/** the appeal route printed on every reason, because everyone listed has one */
export const APPEAL_NOTE = "Appeal at rotector.com";

/**
 * The appeal routes of the non-Rotector services that flagged this member.
 *
 * Empty under the Rotector backend, which is the common case: `signals` is only
 * populated by an aggregating backend, so a plain Rotector scan produces the
 * single-link reason it always did. Derived from the report rather than from
 * the backend setting, because a reason has to name the evidence the action was
 * actually taken on, not whatever service happens to be selected when somebody
 * later reads the audit log.
 */
function extraAppealRoutes(report: MemberReport): string[] {
    const routes: string[] = [];
    try {
        for (const signal of report?.signals ?? []) {
            if (!signal?.flagged || signal.source === "rotector") continue;
            const url = APPEAL_LINKS[signal.source];
            if (!url) continue;
            const note = `Appeal at ${url.replace(/^https?:\/\//, "")}`;
            if (!routes.includes(note)) routes.push(note);
        }
    } catch {
        // A malformed signal must never be the reason an action loses its
        // required Rotector link, which is already on the reason by here.
    }
    return routes;
}

/** Discord truncates audit-log reasons past this (rsb/discord/http.py:677) */
export const MAX_REASON = 512;

/** Discord's message limit; the notice is composed to fit inside one message */
export const MAX_NOTICE = 2000;

/** `{reason}` is replaced by the Rotector finding; attribution is always appended */
export const DEFAULT_REASON_TEMPLATE = "Flagged by Rotector: {reason}";

/**
 * The default text somebody is given before they are actioned. Deliberately
 * plain: it states what is happening, why, and how to contest it, and claims
 * nothing Rotector does not.
 */
export const DEFAULT_NOTICE =
    "You are being {action} from {place}.\n\n" +
    "A Roblox account linked to your Discord account has been flagged in the " +
    "Rotector database: {reason}\n\n" +
    "If you believe this is a mistake, you can appeal at " +
    "https://rotector.com - the appeal goes to Rotector, not to this server.";

export type ActionKind = "kick" | "ban";

/**
 * kick/ban -> (verb, past tense, gerund, sentence case).
 *
 * Supplied rather than derived, because English does not let you get to
 * "banned" from "ban" by adding "ed" — the same note rsb/moderation.py's
 * BulkPlan.past carries.
 */
export const ACTION_WORDS: Record<ActionKind, {
    verb: string;
    past: string;
    gerund: string;
    title: string;
}> = {
    kick: { verb: "kick", past: "kicked", gerund: "Kicking", title: "Kick" },
    ban: { verb: "ban", past: "banned", gerund: "Banning", title: "Ban" },
};

// --------------------------------------------------------------------------
// settings, read defensively
// --------------------------------------------------------------------------

/**
 * Read one setting.
 *
 * `settings.store` throws before the plugin has been registered and can be an
 * incomplete object during a hot reload. A settings read is never allowed to be
 * the thing that decides somebody gets banned without a reason, so every read
 * falls back to the documented default instead.
 */
function setting<T>(key: string, fallback: T): T {
    try {
        const store = (settings as any)?.store;
        if (store == null) return fallback;
        const value = store[key];
        return value === undefined || value === null ? fallback : (value as T);
    } catch {
        return fallback;
    }
}

function num(value: unknown, fallback: number, min: number, max: number): number {
    const parsed = typeof value === "number" ? value : Number(value);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.min(max, Math.max(min, parsed));
}

function text(value: unknown, fallback: string): string {
    return typeof value === "string" ? value : fallback;
}

/**
 * Your own appeal route, alongside Rotector's.
 *
 * Never instead of. Rotector's terms require that anyone actioned on their data
 * can appeal *to Rotector*, so somebody told only to contact you would have
 * lost the ability to correct the database that flagged them.
 */
export interface AppealRoute {
    /** an invite to a server where appeals are handled */
    invite: string;
    /** free text: an email, a moderator handle, a form URL */
    contact: string;
    /** an extra sentence appended after the routes */
    note: string;
    /** also mention it in the audit-log reason, space permitting */
    includeInReason: boolean;
    /** whether any of it was actually filled in */
    configured: boolean;
}

export function appealFromSettings(): AppealRoute {
    const invite = text(setting("appealInvite", ""), "").trim();
    const contact = text(setting("appealContact", ""), "").trim();
    const note = text(setting("appealNote", ""), "").trim();
    return {
        invite,
        contact,
        note,
        includeInReason: setting("appealInReason", false) === true,
        configured: Boolean(invite || contact || note),
    };
}

/** The operator's bar for acting, separate from anything a vendor claims. */
export interface ActionPolicy {
    /**
     * Only Rotector's actionable flag types may be actioned without an
     * override. This reflects Rotector's own guidance; leaving it on means
     * anything else is blocked outright rather than merely warned about.
     */
    requireThreat: boolean;
    /**
     * Whether CAUTION findings may be actioned at all. They are never routine:
     * Rotector asks that Provisional/Mixed be reviewed by a person, so acting
     * on one always needs a second, separate confirmation.
     */
    allowCaution: boolean;
    /** the audit-log reason template; `{reason}` is the finding */
    template: string;
    /** the operator's own reason text, when they typed one instead */
    custom?: string;
    appeal: AppealRoute;
    /** the triage table's own policy — rule R3's two knobs */
    triage: TriagePolicy;
    /**
     * Whether the finding gates the action at all. False is the "this one is
     * your call" path the TUI uses for actions where the finding is context
     * rather than a precondition; it never skips the confirmation, only the
     * eligibility test.
     */
    gated: boolean;
}

export function actionPolicyFromSettings(): ActionPolicy {
    let triagePolicy: TriagePolicy = DEFAULT_POLICY;
    try {
        triagePolicy = policyFromSettings() ?? DEFAULT_POLICY;
    } catch {
        // A policy read must not be the thing that stops a moderator acting;
        // the shipped default is the conservative one anyway.
    }
    return {
        requireThreat: setting("requireThreat", true) === true,
        allowCaution: setting("allowCaution", true) === true,
        template: text(setting("defaultReason", DEFAULT_REASON_TEMPLATE), DEFAULT_REASON_TEMPLATE)
            || DEFAULT_REASON_TEMPLATE,
        appeal: appealFromSettings(),
        triage: triagePolicy,
        gated: true,
    };
}

/** How many seconds of the banned member's messages Discord should delete. */
export function deleteMessageSecondsSetting(): number {
    // Discord's own ceiling is one week; anything above it is rejected outright.
    return Math.round(num(setting("deleteMessageSeconds", 0), 0, 0, 604800));
}

/** Seconds between members in a bulk run. */
export function bulkDelaySetting(): number {
    return num(setting("bulkDelay", 1), 1, 0, 60);
}

/**
 * Whether the operator asked for people to be told before they are actioned.
 *
 * Read, and honoured only as far as a user account may honour it: see the
 * module docstring. Nothing is DMed automatically.
 */
export function notifyBeforeActionSetting(): boolean {
    return setting("notifyBeforeAction", true) === true;
}

// --------------------------------------------------------------------------
// templates
// --------------------------------------------------------------------------

const PLACEHOLDER = /\{([^{}]*)\}/g;

/**
 * Fill `{name}` placeholders, or refuse.
 *
 * Returns null when the template asks for a field this module cannot supply,
 * which is the Python's `except (KeyError, IndexError)` path: a template with a
 * stray brace in it should not break the action, and it must never cost the
 * finding its detail either, so the caller falls back to appending the finding.
 * Unbalanced braces are left alone rather than raising, which is the one place
 * this is more forgiving than Python's `str.format`.
 */
function applyTemplate(template: string, fields: Record<string, string>): string | null {
    let unknown = false;
    const filled = String(template ?? "").replace(PLACEHOLDER, (whole, key) => {
        if (Object.prototype.hasOwnProperty.call(fields, key)) return fields[key];
        unknown = true;
        return whole;
    });
    return unknown ? null : filled;
}

/** Collapse every run of whitespace, the way Discord's reason header wants it. */
function collapse(value: string): string {
    return String(value ?? "").split(/\s+/).filter(Boolean).join(" ");
}

/** A short, factual description of why this member was flagged. */
export function summariseFinding(report: MemberReport): string {
    let account: ReturnType<typeof worstAccount> = null;
    try {
        account = worstAccount(report);
    } catch {
        account = null;
    }
    if (account == null) return "no Rotector finding";

    const parts: string[] = [flagName(account.flagType)];
    const category = categoryName(account.category);
    if (category != null) parts.push(category);

    const reasons = Object.keys(account.reasons ?? {});
    if (reasons.length) parts.push(reasons.slice(0, 2).join(", "));

    parts.push(`Roblox ${account.username} (${account.userId})`);
    return parts.join(" - ");
}

/**
 * Compose an audit-log reason, always carrying the appeal link.
 *
 * The link is appended to a reason the operator typed themselves as readily as
 * to a generated one. That is not a courtesy: Rotector's terms require that an
 * action taken on their data attributes them or links rotector.com, so that the
 * person on the other end of it can appeal to the database that flagged them.
 */
export function buildReason(
    report: MemberReport,
    template: string = DEFAULT_REASON_TEMPLATE,
    custom?: string | null,
    appeal?: AppealRoute | null
): string {
    let body: string;
    if (custom != null && String(custom).trim()) {
        body = String(custom).trim();
    } else {
        const finding = summariseFinding(report);
        const filled = applyTemplate(String(template ?? ""), { reason: finding });
        body = filled != null ? filled : `${template} ${finding}`.trim();
    }

    if (!body.toLowerCase().includes("rotector.com")) {
        body = `${body} | ${APPEAL_NOTE}`;
    }

    // Rotector's route is required. The other two are required in the same
    // sense whenever they are part of why this person is being actioned: under
    // an aggregating backend a CAUTION override can rest on Detective
    // Okappiki's or mococo's sighting, and telling somebody banned on that
    // evidence to appeal to Rotector sends them to a database with no record of
    // them and no power to help. Only sources that actually flagged them are
    // named — listing a service that said nothing would be worse than useless.
    for (const route of extraAppealRoutes(report)) {
        if (body.toLowerCase().includes(route.toLowerCase())) continue;
        const candidate = collapse(`${body} | ${route}`);
        // The same rule the operator's own route obeys below: an additional
        // link never displaces the required one and never costs the finding its
        // detail. A route that will not fit is dropped rather than truncated
        // into something unclickable.
        if (candidate.length <= MAX_REASON) body = candidate;
    }

    if (appeal != null && appeal.includeInReason) {
        const own = (appeal.invite || appeal.contact || "").trim();
        if (own) {
            const candidate = collapse(`${body} | Also: ${own}`);
            // Same rule as the notice: an optional route never displaces the
            // required one, and never costs the finding its detail.
            if (candidate.length <= MAX_REASON) body = candidate;
        }
    }

    body = collapse(body);
    if (body.length > MAX_REASON) {
        // Trim the body, never the appeal link. The suffix length is measured
        // rather than guessed — Discord rejects reasons past MAX_REASON, and
        // being one character over is just as fatal as being a hundred.
        const suffix = `... | ${APPEAL_NOTE}`;
        body = body.slice(0, MAX_REASON - suffix.length).replace(/\s+$/, "") + suffix;
    }
    return body.slice(0, MAX_REASON);
}

/**
 * The attribution rule, applied to a reason that has already been built.
 *
 * `buildReason` guarantees this, so in normal operation it changes nothing.
 * It exists because `runAction` is the last point at which a reason can still
 * be repaired, and a reason assembled by some future caller that forgot the
 * link would otherwise reach Discord's audit log without it.
 */
export function ensureAttribution(reason: string): string {
    const body = collapse(reason);
    if (!body) return APPEAL_NOTE;
    if (body.toLowerCase().includes("rotector.com")) return body.slice(0, MAX_REASON);
    const suffix = ` | ${APPEAL_NOTE}`;
    if (body.length + suffix.length <= MAX_REASON) return body + suffix;
    return body.slice(0, MAX_REASON - suffix.length).replace(/\s+$/, "") + suffix;
}

/**
 * Your own appeal route, rendered for a message.
 *
 * Always *additional*, for the reason in `AppealRoute`'s own comment.
 */
export function appealLines(appeal?: AppealRoute | null): string[] {
    if (appeal == null || !appeal.configured) return [];
    const lines = ["", "", "You can also appeal directly to us:"];
    if (appeal.invite.trim()) lines.push(`  ${appeal.invite.trim()}`);
    if (appeal.contact.trim()) lines.push(`  ${appeal.contact.trim()}`);
    if (appeal.note.trim()) lines.push(appeal.note.trim());
    return lines;
}

/**
 * Compose the message somebody should be given *before* they are actioned.
 *
 * Before is the whole point: once somebody is banned there is no shared server
 * left and a DM can no longer reach them. A notice that arrives after the door
 * is shut is not a notice.
 *
 * This module composes it and never sends it — see the module docstring.
 */
export function buildNotice(
    report: MemberReport,
    action: string,
    place: string,
    template: string = DEFAULT_NOTICE,
    appeal?: AppealRoute | null
): string {
    const finding = summariseFinding(report);
    const filled = applyTemplate(String(template ?? ""), {
        action: String(action ?? ""),
        place: String(place ?? ""),
        reason: finding,
    });
    // A template with stray braces should not cost somebody their notice.
    let body = filled != null ? filled : `${template}\n\n${action} - ${place} - ${finding}`;

    if (!body.toLowerCase().includes("rotector.com")) {
        body = `${body}\n\nYou can appeal at https://rotector.com`;
    }

    const extra = appealLines(appeal);
    if (extra.length) {
        const candidate = body + extra.join("\n");
        // Only if it fits: the Rotector route is required and must never be the
        // thing that gets trimmed to make room for the optional one.
        if (candidate.length <= MAX_NOTICE) body = candidate;
    }
    return body.slice(0, MAX_NOTICE);
}

// --------------------------------------------------------------------------
// eligibility
// --------------------------------------------------------------------------

export interface Eligibility {
    allowed: boolean;
    needsOverride: boolean;
    explanation: string;
    /**
     * CAUTION means Rotector found *something* but deliberately did not
     * conclude from it. It is the one tier where acting is defensible and still
     * might be wrong, so it is permitted — behind a second, separate
     * confirmation that cannot be satisfied by the same click as the first.
     */
    needsDoubleConfirm: boolean;
    /** the id of the triage rule that decided this, e.g. "R4" — the audit handle */
    rule: string;
    recommendation: Recommendation;
    /** the full triage result, so a dialog can print the basis lines */
    call: Triage | null;
}

function ungated(report: MemberReport): Eligibility {
    let verdict = Verdict.UNKNOWN;
    try {
        verdict = reportVerdict(report);
    } catch {
        verdict = Verdict.UNKNOWN;
    }
    return {
        allowed: true,
        needsOverride: false,
        explanation:
            `${verdictLabel(verdict)}. This one is your call - the finding is ` +
            "context, not a precondition.",
        needsDoubleConfirm: false,
        rule: "-",
        recommendation: Recommendation.NOTHING,
        call: null,
    };
}

/**
 * Whether this member may be actioned, and how strongly to warn.
 *
 * The decision is api/triage.ts's, not this function's. The rule table
 * separates the piles and names the rule it used, so the explanation here is a
 * citation rather than a restatement — which is what makes it something a
 * moderator can look up and disagree with.
 */
export function checkEligibility(report: MemberReport, policy?: Partial<ActionPolicy>): Eligibility {
    const requireThreat = policy?.requireThreat !== false;
    const allowCaution = policy?.allowCaution !== false;
    if (policy?.gated === false) return ungated(report);

    let call: Triage;
    try {
        call = triage(report, policy?.triage ?? DEFAULT_POLICY);
    } catch (err) {
        // Triage is the thing that decides whether acting is supported. If it
        // could not run, nothing supports acting, and that is what is reported.
        logger.error("triage failed while checking eligibility", err);
        return {
            allowed: false,
            needsOverride: true,
            explanation:
                "The triage table could not classify this member, so nothing here " +
                "supports acting on them.",
            needsDoubleConfirm: false,
            rule: "-",
            recommendation: Recommendation.NOTHING,
            call: null,
        };
    }

    const cite = `[${call.rule}] `;

    if (call.recommendation === Recommendation.BAN) {
        return {
            allowed: true,
            needsOverride: false,
            explanation: cite + call.headline,
            needsDoubleConfirm: false,
            rule: call.rule,
            recommendation: call.recommendation,
            call,
        };
    }

    if (call.recommendation === Recommendation.CAUTION && allowCaution) {
        // Real evidence that no database will conclude from. Acting is a
        // judgement call, so it is allowed — but only through a second
        // confirmation, because the honest description of it is "might be wrong".
        return {
            allowed: true,
            needsOverride: true,
            explanation:
                cite + call.headline + " Acting on it is your judgement, not the database's.",
            needsDoubleConfirm: true,
            rule: call.rule,
            recommendation: call.recommendation,
            call,
        };
    }

    return {
        allowed: !requireThreat,
        needsOverride: true,
        explanation: cite + call.headline,
        needsDoubleConfirm: false,
        rule: call.rule,
        recommendation: call.recommendation,
        call,
    };
}

// --------------------------------------------------------------------------
// permissions
// --------------------------------------------------------------------------

export interface PermissionReport {
    ok: boolean;
    /** one plain sentence: what is missing, or that nothing is */
    detail: string;
}

const PERMISSION_OK: PermissionReport = { ok: true, detail: "" };

function guildOf(guildId: string): any {
    try {
        return (GuildStore as any)?.getGuild?.(guildId) ?? null;
    } catch {
        return null;
    }
}

function permissionBit(kind: ActionKind): bigint | null {
    try {
        const bits = PermissionsBits as any;
        const bit = kind === "ban" ? bits?.BAN_MEMBERS : bits?.KICK_MEMBERS;
        return typeof bit === "bigint" ? bit : null;
    } catch {
        return null;
    }
}

/** The cached guild member record, or null. Never throws; a miss is normal. */
function attemptMember(guildId: string, userId: string): any {
    try {
        return (GuildMemberStore as any)?.getMember?.(guildId, userId) ?? null;
    } catch {
        return null;
    }
}

function currentUserId(): string | null {
    try {
        return (UserStore as any)?.getCurrentUser?.()?.id ?? null;
    } catch {
        return null;
    }
}

/**
 * Whether you hold the permission at all, in this guild.
 *
 * Checked before the action is offered rather than after it 403s: a wall of
 * "Missing Permissions" errors after fifty requests have already gone out tells
 * the operator nothing they could not have been told first, and it spends
 * Discord's rate limit finding it out.
 */
export function checkGuildPermission(kind: ActionKind, guildId: string): PermissionReport {
    const words = ACTION_WORDS[kind] ?? ACTION_WORDS.ban;
    if (!guildId) {
        return { ok: false, detail: `There is no server here to ${words.verb} anyone from.` };
    }

    const guild = guildOf(guildId);
    if (guild == null) {
        return {
            ok: false,
            detail: "This client does not know that server, so it cannot check what you may do there.",
        };
    }

    const bit = permissionBit(kind);
    if (bit == null) {
        return {
            ok: false,
            detail:
                "Discord's permission table is not available yet. Reopen this in a moment - " +
                "acting without checking first is how a bulk run turns into fifty rejected requests.",
        };
    }

    try {
        if ((PermissionStore as any)?.can?.(bit, guild) === true) return PERMISSION_OK;
    } catch (err) {
        logger.error("permission check failed", err);
        return { ok: false, detail: "This client could not answer whether you hold that permission." };
    }

    const name = kind === "ban" ? "Ban Members" : "Kick Members";
    return { ok: false, detail: `You do not have ${name} in this server.` };
}

/**
 * Whether you may action this particular member.
 *
 * Discord's own rules, asked of Discord's own store: you cannot action
 * yourself, you cannot action the owner, and you cannot action somebody whose
 * highest role sits at or above yours.
 */
export function canActOnMember(kind: ActionKind, guildId: string, userId: string): PermissionReport {
    const guildPermission = checkGuildPermission(kind, guildId);
    if (!guildPermission.ok) return guildPermission;
    return memberPermission(kind, guildId, userId);
}

/**
 * The member-specific half of `canActOnMember`, with the guild-wide question
 * already answered. Separate because `planAction` asks the guild-wide one once
 * and this one per member, and a bulk plan over a large server should not
 * recompute the guild's permission bitfield thousands of times.
 */
function memberPermission(kind: ActionKind, guildId: string, userId: string): PermissionReport {
    const words = ACTION_WORDS[kind] ?? ACTION_WORDS.ban;

    if (userId && userId === currentUserId()) {
        return { ok: false, detail: `You cannot ${words.verb} yourself.` };
    }

    const guild = guildOf(guildId);
    const ownerId = guild?.ownerId ?? guild?.owner_id ?? null;
    if (ownerId != null && String(ownerId) === String(userId)) {
        return { ok: false, detail: "They own this server; nobody can action them." };
    }

    // The hierarchy rule is asked of Discord's own store, but only when that
    // store actually holds the member. `GuildMemberStore` is cache only, and in
    // a server this client has not fully loaded an uncached member has no roles
    // as far as it is concerned — an answer about somebody it has never seen is
    // not an answer, and blocking on it would set aside people the operator can
    // plainly action. Unknown falls through to Discord, which applies the real
    // rule and says so.
    const known = attemptMember(guildId, userId) != null;
    const bit = permissionBit(kind);
    try {
        const store = PermissionStore as any;
        if (known && bit != null && typeof store?.canManageUser === "function") {
            if (store.canManageUser(bit, userId, guild) !== true) {
                return {
                    ok: false,
                    detail:
                        "Their highest role is at or above yours, so Discord will not let you " +
                        `${words.verb} them.`,
                };
            }
        }
    } catch (err) {
        // An unanswerable hierarchy check is not a refusal — Discord will still
        // apply the real rule — so this falls through rather than blocking.
        logger.error("role hierarchy check failed", err);
    }

    // Whether they are still in the server is deliberately not checked here.
    // `GuildMemberStore` is cache only — in a server this client has not fully
    // loaded, most members are absent from it — so treating a cache miss as
    // "they left" would block kicks against people who are plainly still there.
    // Discord answers that question authoritatively, and `errorText` turns its
    // 404 into "They are no longer here."
    return PERMISSION_OK;
}

// --------------------------------------------------------------------------
// planning
// --------------------------------------------------------------------------

export interface ActionTarget {
    id: string;
    /** the member's own name, or their id when the client has never seen them */
    label: string;
    verdict: Verdict;
    eligibility: Eligibility;
    /** the exact text that will be written to the audit log for this member */
    reason: string;
    /** whether *you* may action them, which is a separate question from the finding */
    permission: PermissionReport;
}

export interface ActionPlan {
    kind: ActionKind;
    guildId: string;
    /** the members that may proceed */
    targets: ActionTarget[];
    /**
     * the template — or the operator's own text — every target's reason above
     * was composed from. Each target carries its own composed reason, because
     * each names its own finding.
     */
    reason: string;
    /** set aside: either the finding does not support it, or you cannot act on them */
    blocked: ActionTarget[];
    /** the subset of `targets` that may only proceed as a deliberate override */
    overrides: ActionTarget[];
    /** the guild-wide permission answer, asked once */
    permission: PermissionReport;
}

function memberLabel(id: string, guildId?: string): string {
    try {
        if (guildId) {
            const nick = (GuildMemberStore as any)?.getNick?.(guildId, id);
            if (nick) return String(nick);
        }
    } catch {
        // fall through to the user store
    }
    try {
        const user = (UserStore as any)?.getUser?.(id);
        const name = user?.globalName || user?.username;
        if (name) return String(name);
    } catch {
        // fall through to the id
    }
    return id;
}

function safeVerdict(report: MemberReport): Verdict {
    try {
        return reportVerdict(report);
    } catch {
        return Verdict.UNKNOWN;
    }
}

/**
 * Work out who an action may touch, before anything happens.
 *
 * Splitting the set up front is the point: acting on fifty people at once is
 * exactly where a mistake is least recoverable, so the ineligible ones are
 * identified and set aside rather than discovered halfway through.
 */
export function planAction(
    kind: ActionKind,
    reports: MemberReport[],
    guildId: string,
    policy?: Partial<ActionPolicy>
): ActionPlan {
    const resolved: ActionPolicy = {
        ...actionPolicyFromSettings(),
        ...(policy ?? {}),
    };

    const plan: ActionPlan = {
        kind: kind === "kick" ? "kick" : "ban",
        guildId: String(guildId ?? ""),
        targets: [],
        reason: (resolved.custom ?? "").trim() || resolved.template,
        blocked: [],
        overrides: [],
        permission: checkGuildPermission(kind === "kick" ? "kick" : "ban", String(guildId ?? "")),
    };

    for (const report of reports ?? []) {
        if (report == null || !report.discordId) continue;

        const eligibility = checkEligibility(report, resolved);
        const permission = plan.permission.ok
            ? memberPermission(plan.kind, plan.guildId, report.discordId)
            : plan.permission;

        const target: ActionTarget = {
            id: report.discordId,
            label: memberLabel(report.discordId, plan.guildId),
            verdict: safeVerdict(report),
            eligibility,
            reason: buildReason(report, resolved.template, resolved.custom, resolved.appeal),
            permission,
        };

        if (!eligibility.allowed || !permission.ok) {
            plan.blocked.push(target);
            continue;
        }
        plan.targets.push(target);
        if (eligibility.needsOverride) plan.overrides.push(target);
    }

    return plan;
}

export function planTotal(plan: ActionPlan): number {
    return plan.targets.length + plan.blocked.length;
}

/** One sentence stating what the plan would do, and what it set aside. */
export function describePlan(plan: ActionPlan): string {
    const total = planTotal(plan);
    if (!total) return "Nothing matches.";

    const words = ACTION_WORDS[plan.kind] ?? ACTION_WORDS.ban;
    let sentence = `${plan.targets.length} of ${total} may be ${words.past}`;

    // The two reasons a member was set aside are different facts and are
    // counted separately: "the finding does not support it" is a statement
    // about the evidence, and "you cannot action them" is a statement about
    // you. Flattening them into one number tells the operator neither.
    const unsupported = plan.blocked.filter(t => !t.eligibility.allowed).length;
    const forbidden = plan.blocked.length - unsupported;
    if (unsupported) sentence += `; ${unsupported} blocked because the finding does not support it`;
    if (forbidden) sentence += `; ${forbidden} blocked because Discord will not let you`;
    if (plan.overrides.length) {
        sentence +=
            `. ${plan.overrides.length} of the ${plan.targets.length} would be a deliberate ` +
            "override of what Rotector documents as actionable";
    }
    return `${sentence}.`;
}

// --------------------------------------------------------------------------
// acting
// --------------------------------------------------------------------------

export interface ActionResult {
    id: string;
    label: string;
    ok: boolean;
    /** the reason as it was actually handed to Discord */
    reason: string;
    error?: string;
}

export interface RunOptions {
    /**
     * The acknowledgement from the confirm step. `runAction` refuses to run a
     * plan carrying overrides without it — the override has to be a decision
     * somebody made, not a default that fell through.
     */
    acknowledgedOverride?: boolean;
    /** how many seconds of the member's messages Discord should delete (ban only) */
    deleteMessageSeconds?: number;
    /** seconds between members */
    bulkDelay?: number;
    signal?: AbortSignal;
    onProgress?(done: number, total: number, result: ActionResult): void;
}

export class ModerationError extends Error {}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
    if (ms <= 0) return Promise.resolve();
    return new Promise<void>(resolve => {
        const timer = setTimeout(finish, ms);
        function finish() {
            clearTimeout(timer);
            try {
                signal?.removeEventListener?.("abort", finish);
            } catch {
                // an environment without removeEventListener still resolved
            }
            resolve();
        }
        try {
            signal?.addEventListener?.("abort", finish, { once: true } as any);
        } catch {
            // no abort support: the timer alone is the whole behaviour
        }
    });
}

/**
 * Discord's ban/kick creators, or null.
 *
 * Null is a refusal, not a fallback. Building the REST request by hand would
 * mean guessing how the audit-log reason reaches the wire, and a ban recorded
 * with no attribution is precisely what Rotector's terms forbid.
 */
export function actionCreators(): any | null {
    try {
        const module = GuildActionCreators as any;
        if (module && typeof module.banUser === "function" && typeof module.kickUser === "function") {
            return module;
        }
    } catch (err) {
        logger.error("could not resolve Discord's guild action creators", err);
    }
    return null;
}

export function creatorsAvailable(): boolean {
    return actionCreators() != null;
}

/**
 * Call one of Discord's creators in whichever shape this client build uses.
 *
 * Discord has shipped both a positional creator and an options-object one.
 * `Function.length` is the declared parameter count and separates them: the
 * options form takes exactly one argument, while every positional form takes at
 * least the guild and the user. Getting this wrong would drop the reason, so it
 * is decided from the function itself rather than assumed.
 */
function callCreator(fn: any, positional: any[], options: Record<string, any>): any {
    if (typeof fn !== "function") throw new ModerationError("Discord's action creator is missing.");
    if (fn.length <= 1) return fn(options);
    return fn(...positional);
}

async function performOne(
    plan: ActionPlan,
    target: ActionTarget,
    reason: string,
    deleteMessageSeconds: number
): Promise<void> {
    const creators = actionCreators();
    if (creators == null) {
        throw new ModerationError(
            "This client does not expose Discord's own ban/kick action, so the plugin cannot " +
            "guarantee the audit-log reason would be recorded. Nothing was sent."
        );
    }

    if (plan.kind === "ban") {
        await Promise.resolve(
            callCreator(
                creators.banUser,
                [plan.guildId, target.id, deleteMessageSeconds, reason],
                {
                    guildId: plan.guildId,
                    userId: target.id,
                    deleteMessageSeconds,
                    reason,
                }
            )
        );
        return;
    }

    await Promise.resolve(
        callCreator(
            creators.kickUser,
            [plan.guildId, target.id, reason],
            { guildId: plan.guildId, userId: target.id, reason }
        )
    );
}

function errorText(err: unknown): string {
    if (err == null) return "Unknown error.";
    const body = (err as any)?.body;
    const message = body?.message ?? (err as any)?.message;
    const status = (err as any)?.status;
    if (typeof message === "string" && message) {
        return status ? `${message} (HTTP ${status})` : message;
    }
    if (status === 403) return "Discord refused it: missing permissions.";
    if (status === 404) return "They are no longer here.";
    return String(message ?? err);
}

/**
 * Work through a confirmed plan, one member at a time.
 *
 * Paced rather than parallel, and interruptible: this is a lot of irreversible
 * requests, and being able to stop halfway through matters more than finishing
 * quickly. One member Discord refuses does not strand the rest — the failure is
 * recorded and the run continues.
 */
export async function runAction(
    plan: ActionPlan,
    guildId: string,
    opts: RunOptions = {}
): Promise<ActionResult[]> {
    if (plan == null) throw new ModerationError("There is no plan to run.");

    // The confirm step is not optional and is not the dialog's private business:
    // a plan that only proceeds as an override must carry the operator's
    // acknowledgement with it, or nothing happens.
    if (plan.overrides.length > 0 && opts.acknowledgedOverride !== true) {
        throw new ModerationError(
            `${plan.overrides.length} of these members are only actionable as a deliberate ` +
            "override of what Rotector documents as actionable, and that was not acknowledged."
        );
    }

    if (!plan.permission.ok) throw new ModerationError(plan.permission.detail);
    if (actionCreators() == null) {
        throw new ModerationError(
            "This client does not expose Discord's own ban/kick action, so the plugin cannot " +
            "guarantee the audit-log reason would be recorded. Nothing was sent."
        );
    }

    const running: ActionPlan = { ...plan, guildId: String(guildId ?? plan.guildId ?? "") };
    if (!running.guildId) throw new ModerationError("There is no server to act in.");

    const seconds =
        running.kind === "ban"
            ? Math.round(num(opts.deleteMessageSeconds ?? deleteMessageSecondsSetting(), 0, 0, 604800))
            : 0;
    const delay = Math.max(0, num(opts.bulkDelay ?? bulkDelaySetting(), 1, 0, 60)) * 1000;

    const results: ActionResult[] = [];
    const total = running.targets.length;

    for (let index = 0; index < total; index++) {
        if (opts.signal?.aborted) break;
        const target = running.targets[index];

        // The attribution rule, applied at the last possible moment.
        const reason = ensureAttribution(target.reason);
        const result: ActionResult = { id: target.id, label: target.label, ok: false, reason };

        try {
            await performOne(running, target, reason, seconds);
            result.ok = true;
        } catch (err) {
            result.error = errorText(err);
            logger.error(`could not ${running.kind} ${target.id}`, err);
        }

        results.push(result);
        try {
            opts.onProgress?.(results.length, total, result);
        } catch (err) {
            logger.error("an action progress handler threw", err);
        }

        if (index < total - 1 && delay > 0 && !opts.signal?.aborted) {
            await sleep(delay, opts.signal);
        }
    }

    return results;
}
