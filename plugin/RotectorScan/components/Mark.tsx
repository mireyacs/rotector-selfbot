/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The decorator: the member list, the message author line and the DM list.
 *
 * This is the smallest surface in the plugin and the one that decides whether
 * the whole thing is honest. It renders *nothing at all* for NO_DETECTIONS and
 * UNKNOWN, which is not a visual preference — it is FilterMode.FINDINGS plus
 * hidden_verdicts from rsb/tui/app.py:121-134 and :623-629:
 *
 *     "in a real server the overwhelming majority of members have nothing
 *      against them, and listing them all buries the few that matter"
 *
 * A mark beside every member would destroy the argument the design is making.
 * The counterpart obligation is that anything hidden is still counted and
 * announced — the scan modal's summary prints "filter: findings (N hidden)" —
 * because silently dropping members is lying about coverage.
 *
 * The mark itself is drawn geometry: a stripe field in the verdict hue whose
 * duty cycle also encodes the tier, so the tier survives for a reader who
 * cannot separate the hues. No emoji, no icon font, no Discord icon component
 * (DESIGN.md:611-616).
 */

import { Tooltip, useEffect, UserStore } from "@webpack/common";

import { reportVerdict, staleNote, worstAccount } from "../api/types";
import { flagName, Verdict, verdictLabel, verdictMeaning, verdictName } from "../api/verdict";
import { settings, verdictFloor } from "../settings";
import { reportStore, useReport } from "../store";
import { openReport } from "./ReportModal";

import type { MemberReport } from "../api/types";

export type MarkSurface = "member" | "message" | "dm";

/**
 * Settings are read through this rather than directly.
 *
 * `settings.store` throws before the plugin is registered, and a decorator that
 * throws does not merely lose its own mark — the API wraps each one in an
 * ErrorBoundary, so a throw blanks the decorator for good until the next
 * re-render. Defaults here match settings.ts and are only ever reached when the
 * store is not ready.
 */
function read<T>(key: string, fallback: T): T {
    try {
        const store: any = settings?.store;
        const value = store?.[key];
        return value === undefined || value === null ? fallback : value as T;
    } catch {
        return fallback;
    }
}

/** Which setting switches this surface off. */
function surfaceEnabled(where: MarkSurface): boolean {
    switch (where) {
        case "message": return read("markMessages", true) !== false;
        case "dm": return read("markDms", true) !== false;
        default: return read("markMemberList", true) !== false;
    }
}

/**
 * Whether this surface may issue a *new* lookup for a member it has not seen.
 *
 * `autoLookup` is the user's budget for lookups they did not ask for, and the
 * message surface sits outside `visible` on purpose: that mode's own
 * description says it covers "the member list and profiles", and index.tsx's
 * MESSAGE_CREATE handler already refuses to queue message authors below
 * `visible+messages`. `reportStore.request()` only rejects `"off"`, so the
 * distinction has to be drawn here or every message in a busy channel spends
 * the shared rate limit the setting was written to cap.
 *
 * This gates the lookup and nothing else. A finding already held in memory
 * still gets its mark on every enabled surface — refusing to show evidence we
 * already have would be the wrong kind of thrift, and costs no request.
 */
function lookupAllowed(where: MarkSurface): boolean {
    const raw = read<unknown>("autoLookup", "visible");
    const mode = typeof raw === "string" ? raw : "visible";
    if (mode === "off") return false;
    if (where === "message") return mode === "visible+messages";
    return true;
}

/**
 * The lowest verdict that earns a mark.
 *
 * settings.ts owns this, but a decorator must not depend on that module having
 * loaded cleanly, so an unusable answer falls back to INFO — the documented
 * default, which hides exactly NO_DETECTIONS and UNKNOWN.
 */
function floor(): Verdict {
    try {
        if (typeof verdictFloor === "function") {
            const v = verdictFloor();
            if (typeof v === "number" && v >= Verdict.UNKNOWN && v <= Verdict.THREAT) return v;
        }
    } catch {
        // fall through
    }
    return Verdict.INFO;
}

/** True when this id is us, or a bot, and therefore never worth marking. */
function skipUser(userId: string): boolean {
    try {
        if (UserStore?.getCurrentUser?.()?.id === userId) return true;
        // an unknown user is not a bot — it is a user the client has not
        // cached, and marking it is still correct
        return UserStore?.getUser?.(userId)?.bot === true;
    } catch {
        return false;
    }
}

/**
 * The tooltip and the accessible name, which are deliberately the same text.
 *
 * Colour is never the only carrier: this string always names the verdict and
 * states its meaning, so NO DETECTIONS can never be mistaken for a clearance
 * and THREAT never rests on a hue alone.
 *
 * An answer from beyond the terms' 24-hour window says so here too, and the
 * mark's own hue does not change to say it: staleness is a fact about the
 * evidence rather than a sixth tier, and a THREAT three days old is still a
 * THREAT about three days ago. The tooltip and the accessible name are the same
 * string, so the warning reaches a screen reader on the same terms.
 */
function describe(verdict: Verdict, report: MemberReport | undefined): string {
    let text = `${verdictLabel(verdict)} — ${verdictMeaning(verdict)}`;

    try {
        const stale = staleNote(report);
        if (stale) text += `\n${stale}`;
    } catch {
        // a clock question must never cost the mark its verdict line
    }

    try {
        const account = report ? worstAccount(report) : null;
        if (account) {
            const name = flagName(account.flagType);
            const who = account.username || `id ${account.userId}`;
            text += `\n${name} — ${who}`;
        }
        if (report?.servers?.length) {
            const n = report.servers.length;
            text += `\nIn ${n} tracked server${n === 1 ? "" : "s"}`;
        }
    } catch {
        // the verdict line above is the part that matters; anything else is
        // detail and must not cost the mark its tooltip
    }

    return text;
}

/** The member-list / message / DM decorator. Null unless there is a finding to show. */
export function Mark({ userId, where }: { userId: string; where: MarkSurface; }) {
    // Hooks run unconditionally and before every early return: this component
    // returns null far more often than it returns a mark, and a conditional
    // hook would break React's ordering the first time a report arrived.
    const state = useReport(userId) ?? ({} as { report?: MemberReport; pending?: boolean; });
    const report = state.report;
    const pending = state.pending;

    const enabled = Boolean(userId) && surfaceEnabled(where) && !skipUser(userId);
    // Rendering a mark and asking for one are two different permissions: the
    // surface switch decides whether a finding is drawn here, `autoLookup`
    // decides whether this surface is allowed to go and find one.
    const mayLookup = enabled && lookupAllowed(where);

    useEffect(() => {
        if (!mayLookup) return;
        try {
            // never during render: request() schedules a batched network lookup
            reportStore?.request?.(userId);
        } catch (err) {
            console.error("[RotectorScan] lookup request failed", err);
        }
    }, [userId, mayLookup]);

    if (!enabled) return null;

    let verdict = Verdict.UNKNOWN;
    try {
        if (report) verdict = reportVerdict(report);
    } catch {
        verdict = Verdict.UNKNOWN;
    }

    const isPending = Boolean(pending) && !report;

    if (isPending) {
        // A member list full of empty outlines is noise, so the pending state
        // is opt-in. The scan modal shows progress properly; this is only for
        // someone who wants to watch lazy lookups land.
        if (read("showPending", false) !== true) return null;
        return renderMark({
            userId,
            where,
            verdictAttr: verdictName(Verdict.UNKNOWN),
            pendingMark: true,
            text: "Checking with Rotector…",
        });
    }

    // No report and nothing in flight: nothing has been said about this member,
    // which is not a finding and gets no mark.
    if (!report) return null;
    if (verdict < floor()) return null;

    return renderMark({
        userId,
        where,
        verdictAttr: verdictName(verdict),
        pendingMark: false,
        text: describe(verdict, report),
    });
}

interface MarkProps {
    userId: string;
    where: MarkSurface;
    verdictAttr: string;
    pendingMark: boolean;
    text: string;
}

function open(userId: string) {
    try {
        openReport(userId);
    } catch (err) {
        console.error("[RotectorScan] could not open the report modal", err);
    }
}

function renderMark({ userId, where, verdictAttr, pendingMark, text }: MarkProps) {
    const className =
        "rsb rsb-mark" +
        (pendingMark ? " rsb-mark--pending" : "") +
        (where === "message" ? " rsb-mark--message" : "") +
        (where === "dm" ? " rsb-mark--dm" : "");

    // The mark is the button, so the accessible name is the finding rather than
    // "button": role and label carry what the hue and the stripe period carry.
    //
    // Discord's Tooltip hands its child onMouseEnter/onMouseLeave/onFocus/onBlur
    // *and* an onClick that dismisses the tooltip, so ours is composed onto
    // theirs rather than spread over it — overwriting it leaves the tooltip
    // stuck open on top of the modal the click just opened.
    const span = (tooltipProps?: any) => (
        <span
            {...(tooltipProps ?? {})}
            className={className}
            data-verdict={verdictAttr}
            role="button"
            tabIndex={0}
            aria-label={text}
            title={tooltipProps ? undefined : text}
            onClick={(e: any) => {
                // the member row underneath opens the profile popout; this
                // click belongs to us
                e?.stopPropagation?.();
                e?.preventDefault?.();
                tooltipProps?.onClick?.(e);
                open(userId);
            }}
            onKeyDown={(e: any) => {
                if (e?.key !== "Enter" && e?.key !== " " && e?.key !== "Spacebar") return;
                e?.stopPropagation?.();
                e?.preventDefault?.();
                open(userId);
            }}
        />
    );

    // Tooltip is resolved out of Discord's own webpack modules, so it can be
    // missing on a build we have not seen. The mark still works without it —
    // aria-label and the title attribute carry the same sentence.
    if (!Tooltip) return span();

    return <Tooltip text={text}>{tooltipProps => span(tooltipProps)}</Tooltip>;
}
