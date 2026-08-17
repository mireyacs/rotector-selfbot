/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The report on one member, as a modal, and the compact version of the same
 * report for the profile panel.
 *
 * The order of the sections is a port of rsb/tui/app.py's `_render_detail`,
 * kept deliberately literal: identity, then the verdict and what it means,
 * then what a moderator should do about it and which rule said so, then the
 * evidence itself, then who was asked, then the tracked servers, then the
 * attribution and the retention notice. That order is an argument — the reader
 * meets the claim, then the reasoning, then the evidence, and never meets an
 * accusation without the sentence that says how far it goes — so it is copied
 * rather than rearranged to suit a modal's proportions.
 *
 * Two things this file must never do, both from Rotector's Terms of Use:
 * present NO_DETECTIONS as "safe" (the meaning string travels with the verdict
 * everywhere the verdict is shown), and imply the findings are kept. The footer
 * says both out loud on every report.
 */

import ErrorBoundary from "@components/ErrorBoundary";
import { copyWithToast } from "@utils/discord";
import { Logger } from "@utils/Logger";
import type { RenderModalProps } from "@vencord/discord-types";
import { GuildMemberStore, IconUtils, Modal, openModal, ScrollerThin, useEffect, useRef, useState, UserStore, UserUtils } from "@webpack/common";

import { backendAttribution, backendFrom, backendLabel } from "../api/backend";
import { cspSupported, hostClientName, requestHostPermission } from "../api/host";
import {
    APPEAL_LINKS,
    DEFAULT_POLICY,
    findingLabel,
    PARTIES,
    PartyFinding,
    recommendationLabel,
    recommendationMeaning,
    triage,
    Triage,
    TriagePolicy,
    verdictFloor as recommendationVerdict,
} from "../api/triage";
import {
    accountVerdict,
    isStale,
    MemberReport,
    profileUrl,
    reportVerdict,
    RobloxAccount,
    sortedAccounts,
    staleNote,
    staleTag,
    worstAccount,
} from "../api/types";
import {
    categoryName,
    flagIsActionable,
    flagName,
    sourceNames,
    Verdict,
    verdictForFlag,
    verdictLabel,
    verdictMeaning,
    verdictName,
} from "../api/verdict";
import { policyFromSettings, retentionNotice, settings, verdictFloor as configuredFloor } from "../settings";
import { reportStore, useReport } from "../store";
import { Body, Btn, Chip, cl, Empty, KeyVal, Label, Meter, Micro, Node, VerdictLine } from "./primitives";

const logger = new Logger("RotectorScan");

/** the TUI lists twelve servers before it summarises the rest (app.py:2632) */
const SERVER_PREVIEW = 12;

/**
 * Which parties a report on this backend can be about, and therefore whose
 * appeal routes it owes the reader.
 *
 * Rotector's terms require that their data carries the attribution or a link to
 * rotector.com so the affected person can appeal. An Okappiki response is three
 * services' findings in one envelope — Rotector's flag, Detective Okappiki's
 * sighting and mococo's TASE record — and this modal renders all three in the
 * SOURCES node, so printing only Rotector's appeal route would name someone on
 * two other services' say-so and then hand them the wrong door.
 */
const BACKEND_PARTIES: Record<string, readonly string[]> = {
    rotector: ["rotector"],
    okappiki: PARTIES,
};

/*
 * No longer "memory only", and the sentence had to change with the storage
 * rather than after somebody noticed. This report is read from the in-memory
 * cache; a finished *scan* is also written to the plugin's own database on this
 * computer. Both are under one ceiling, which is the part that actually answers
 * the question this notice exists to answer.
 *
 * The ceiling itself is 23 hours until `retainBeyondTerms` moves it, so the
 * first half comes from `retentionNotice()` and is read fresh every time rather
 * than frozen into a constant: a footer that went on promising a 24-hour drop
 * while the caches were told to hold findings for a week would be this plugin's
 * own copy misleading the reader, which is the failure it exists to prevent.
 */
const RETENTION_TAIL =
    " This report is held in memory; a finished scan is also stored on this computer, " +
    "under the same ceiling.";

function retentionLine(): string {
    const notice = (() => {
        try {
            if (typeof retentionNotice === "function") {
                const text = retentionNotice();
                if (text) return String(text);
            }
        } catch {
            // A settings read may not be the thing that blanks a report; the
            // default state is also the safe sentence to fall back to.
        }
        return (
            "Findings are held in memory only and dropped within 24 hours - Rotector's terms " +
            "forbid keeping their data longer, and flag statuses change."
        );
    })();
    return notice + RETENTION_TAIL;
}

/**
 * Why several sources can disagree about the same member, in the app's own
 * words (app.py:2593-2598). Only shown when more than one database was actually
 * consulted, which is to say under the Okappiki backend — a Rotector scan has
 * one answer and this would be explaining a disagreement that cannot happen.
 */
const SOURCES_CAPTION =
    "An aggregating backend asks several services and returns them together. " +
    "Only Rotector publishes flag types with documented actionability, so only " +
    "its findings reach THREAT; the others are unverified and read as CAUTION - " +
    "a person decides.";

const SERVERS_CAPTION =
    "Servers Rotector monitors. Membership alone is a signal, not a verdict.";

// --------------------------------------------------------------------------
// small, defensive readings of things that may not be there
// --------------------------------------------------------------------------

/**
 * Read one setting.
 *
 * `settings.store` throws before the plugin has been registered and can be an
 * incomplete object during a hot reload. A settings read is never allowed to be
 * the thing that blanks a report, so every read falls back to the documented
 * default instead.
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

/** The operator's triage policy, or the shipped default if settings are not up yet. */
function currentPolicy(): TriagePolicy {
    try {
        if (typeof policyFromSettings === "function") {
            const policy = policyFromSettings();
            if (policy) return policy;
        }
    } catch {
        // fall through to the default; a policy read must not break a report
    }
    return DEFAULT_POLICY;
}

/** The lowest verdict that earns a mark. Used by the profile section only. */
function markFloor(): Verdict {
    try {
        if (typeof configuredFloor === "function") {
            const floor = configuredFloor();
            if (typeof floor === "number") return floor as Verdict;
        }
    } catch {
        // fall through
    }
    return Verdict.INFO;
}

/** The backend the operator has selected, when nothing better is known. */
function configuredBackend(): string {
    try {
        return backendFrom((settings as any)?.store);
    } catch {
        return "rotector";
    }
}

/**
 * Which service actually answered for the report on screen.
 *
 * The opener passes it when it knows — a ledger row knows which backend its
 * scan ran against, and that is the honest answer even if the setting has been
 * changed since. Failing that, a report carrying per-source `signals` can only
 * have come from an aggregating backend (api/types.ts: `signals` is always
 * empty under Rotector), which is a fact about the data rather than about the
 * current configuration. Only then does it fall back to the setting.
 */
function backendOf(explicit: string | undefined, report: MemberReport | undefined): string {
    if (explicit) return explicit;
    if (report?.signals?.length) return "okappiki";
    return configuredBackend();
}

/** The appeal routes this backend's findings owe the person they name. */
function appealRoutes(backend: string): { party: string; url: string; }[] {
    const parties = BACKEND_PARTIES[backend] ?? BACKEND_PARTIES.rotector;
    const routes: { party: string; url: string; }[] = [];
    for (const party of parties) {
        const url = APPEAL_LINKS[party];
        if (url) routes.push({ party, url });
    }
    // Rotector's link is required whatever else went wrong reading the table.
    if (!routes.length) routes.push({ party: "rotector", url: "https://rotector.com" });
    return routes;
}

/** "https://okappiki.com/appeal" -> "okappiki.com/appeal", for link text. */
function appealHost(url: string): string {
    return String(url).replace(/^https?:\/\//, "").replace(/\/+$/, "");
}

function classify(report: MemberReport): Triage | null {
    try {
        return triage(report, currentPolicy());
    } catch (err) {
        logger.error("triage failed for a report", err);
        return null;
    }
}

/** Never throws; an unknown user is a normal state, not an error. */
function readUser(userId: string): any {
    try {
        return (UserStore as any)?.getUser?.(userId) ?? null;
    } catch {
        return null;
    }
}

function readNick(guildId: string | undefined, userId: string): string | null {
    if (!guildId) return null;
    try {
        return (GuildMemberStore as any)?.getNick?.(guildId, userId) ?? null;
    } catch {
        return null;
    }
}

function avatarFor(user: any): string | null {
    if (!user) return null;
    try {
        const url = (IconUtils as any)?.getUserAvatarURL?.(user, false, 64);
        if (url) return url;
        return (IconUtils as any)?.getDefaultAvatarURL?.(user.id, user.discriminator) ?? null;
    } catch {
        return null;
    }
}

interface Identity {
    id: string;
    displayName: string;
    /** "@name", the thing a moderator can actually search for */
    handle: string;
    avatar: string | null;
}

/**
 * The member's identity, hydrated if the client happens not to know them yet.
 *
 * A report can be opened from a ledger row for someone who has since left, so
 * the store may hold no user at all. That is rendered as "Unknown member" with
 * the id still shown, because the id is the part that identifies them.
 */
function useIdentity(userId: string, guildId?: string): Identity {
    const [user, setUser] = useState<any>(() => readUser(userId));

    useEffect(() => {
        const known = readUser(userId);
        if (known) {
            setUser(known);
            return;
        }
        let live = true;
        try {
            const pending = (UserUtils as any)?.getUser?.(userId);
            if (pending && typeof pending.then === "function") {
                pending.then(
                    (fetched: any) => {
                        if (live) setUser(fetched);
                    },
                    () => {
                        // a user the client cannot fetch is shown by id; not an error
                    }
                );
            }
        } catch {
            // no user store yet; the id is still enough to render
        }
        return () => {
            live = false;
        };
    }, [userId]);

    const nick = readNick(guildId, userId);
    const username: string | null = user?.username ?? null;
    const displayName = nick || user?.globalName || username || "Unknown member";
    const handle = username
        ? user?.discriminator && user.discriminator !== "0"
            ? `@${username}#${user.discriminator}`
            : `@${username}`
        : "@unknown";

    return { id: userId, displayName, handle, avatar: avatarFor(user) };
}

/**
 * Open a URL the way the host client wants it opened.
 *
 * A bare `<a href>` inside the renderer can navigate Discord itself, which
 * would drop whatever the moderator was doing, so the native opener is tried
 * first and `window.open` is the web fallback.
 */
function openExternal(url: string): void {
    if (!url) return;
    try {
        const native = (window as any)?.VencordNative?.native;
        if (native && typeof native.openExternal === "function") {
            native.openExternal(url);
            return;
        }
    } catch {
        // fall through to window.open
    }
    try {
        window.open(url, "_blank", "noreferrer,noopener");
    } catch (err) {
        logger.error(`could not open ${url}`, err);
    }
}

function copyText(text: string): void {
    try {
        const result = copyWithToast(text, "Report copied to the clipboard.");
        if (result && typeof (result as any).catch === "function") {
            (result as any).catch((err: unknown) => logger.error("copy failed", err));
        }
        return;
    } catch (err) {
        logger.error("copyWithToast failed, falling back to the clipboard API", err);
    }
    try {
        void navigator?.clipboard?.writeText?.(text);
    } catch (err) {
        logger.error("copy failed", err);
    }
}

/** Rotector reports confidence on a 0-1 scale; the modal states it as a percentage. */
function confidencePercent(value: number | null | undefined): string | null {
    if (typeof value !== "number" || !isFinite(value)) return null;
    const percent = value <= 1 ? value * 100 : value;
    return `${Math.round(percent)}%`;
}

/** Reason messages arrive with newlines in them; the TUI flattens them the same way. */
function reasonMessage(detail: any): string {
    const message = detail?.message;
    if (typeof message !== "string" || !message) return "";
    return message.replace(/\n+/g, " / ");
}

function evidenceOf(detail: any): string[] {
    const evidence = detail?.evidence;
    if (!Array.isArray(evidence)) return [];
    return evidence.map(item => String(item));
}

function evidenceCap(): number {
    const raw = Number(setting("evidenceCap", 6));
    return Number.isFinite(raw) && raw >= 0 ? Math.floor(raw) : 6;
}

/**
 * Does this error mean the request never left the browser?
 *
 * The renderer cannot read the response headers that would say so (see
 * api/rotector.ts), so this is a text match on what the client threw. It only
 * decides whether to offer the host-permission guidance, never whether the
 * member is flagged, so being wrong is cheap.
 */
function looksLikeTransportFailure(message: string): boolean {
    const text = String(message ?? "").toLowerCase();
    return (
        text.includes("failed to fetch") ||
        text.includes("networkerror") ||
        text.includes("network error") ||
        text.includes("cors") ||
        text.includes("csp") ||
        text.includes("content security") ||
        text.includes("blocked") ||
        text.includes("transport")
    );
}

function clientName(): string {
    try {
        if (typeof hostClientName === "function") {
            const name = hostClientName();
            if (name) return name;
        }
    } catch {
        // fall through
    }
    return "this client";
}

function canRequestPermission(): boolean {
    try {
        return typeof cspSupported === "function" && cspSupported() === true;
    } catch {
        return false;
    }
}

// --------------------------------------------------------------------------
// the plain-text report, for the clipboard
// --------------------------------------------------------------------------

/**
 * The shape of `_member_summary` in rsb/tui/app.py (:3082), with the triage
 * call added under the verdict: the modal shows the recommendation and the rule
 * that produced it, and a copied report that quietly dropped them would be a
 * weaker claim than the one the reader was looking at.
 *
 * The tail is the same three lines the on-screen footer prints, in the same
 * order — the backend's attribution, the appeal routes, and the 24-hour
 * expiry. A pasted report outlives the modal and is the copy someone actually
 * forwards to a moderator, so it is the copy that most needs to say who found
 * this, where they can argue with it, and that it goes stale.
 */
function summaryTail(backend: string): string[] {
    return [
        backendAttribution(backend),
        `Anyone listed here can appeal at ${appealRoutes(backend).map(route => route.url).join(", ")}`,
        retentionLine(),
    ];
}

function summaryText(
    identity: Identity,
    report: MemberReport | undefined,
    call: Triage | null,
    backend: string
): string {
    const lines: string[] = [];
    lines.push(`${identity.displayName} (${identity.handle})`);
    lines.push(`Discord ID: ${identity.id}`);
    lines.push(`Checked against: ${backendLabel(backend)}`);

    if (!report) {
        lines.push("Verdict: not checked");
        lines.push(...summaryTail(backend));
        return lines.join("\n");
    }

    const verdict = reportVerdict(report);
    // The meaning travels with the verdict even into the clipboard: a pasted
    // "NO DETECTIONS" with no sentence after it is exactly the reading
    // Rotector's terms forbid.
    lines.push(`Verdict: ${verdictLabel(verdict)} - ${verdictMeaning(verdict)}`);
    // Immediately under the verdict, exactly as on screen: a pasted report is
    // the copy somebody forwards to a moderator, and the age is the difference
    // between "this is true" and "this was true on Tuesday".
    const stale = staleNote(report);
    if (stale) lines.push(stale);
    if (call) lines.push(`Triage: ${recommendationLabel(call.recommendation)} (rule ${call.rule})`);
    if (report.error) lines.push(`Lookup error: ${report.error}`);

    for (const account of sortedAccounts(report)) {
        const category = categoryName(account.category);
        lines.push(
            `  Roblox ${account.username} (${account.userId}) - ${flagName(account.flagType)}` +
            (category ? ` / ${category}` : "")
        );
        lines.push(`    ${profileUrl(account)}`);
        for (const [name, detail] of Object.entries(account.reasons ?? {})) {
            lines.push(`    ${name}: ${reasonMessage(detail)}`);
        }
    }

    if (report.servers.length) {
        lines.push(
            "Tracked servers: " + report.servers.slice(0, 8).map(server => server.serverName).join(", ")
        );
    }

    lines.push(...summaryTail(backend));
    return lines.join("\n");
}

// --------------------------------------------------------------------------
// pieces
// --------------------------------------------------------------------------

function IdentityBand({ identity }: { identity: Identity; }) {
    return (
        <div className={cl("id")}>
            {identity.avatar
                ? <img className={cl("id__avatar")} src={identity.avatar} alt="" />
                : <div className={cl("id__avatar", "id__avatar--blank")} aria-hidden="true" />}
            <div className={cl("id__text")}>
                <div className={cl("title")}>{identity.displayName}</div>
                <Micro>{`${identity.handle}  id ${identity.id}`}</Micro>
            </div>
            <Chip className={cl("id__chip")} />
        </div>
    );
}

/**
 * The age of a finding that outlived the terms' window.
 *
 * Renders nothing for a fresh report, so the ordinary case is unchanged. When
 * it does render it sits directly under the verdict and above the triage call,
 * because it qualifies the verdict rather than the recommendation — a THREAT
 * from three days ago is still a THREAT about three days ago.
 *
 * The CAUTION hue is the same borrowing the "DID NOT ANSWER" source line makes
 * and for the same reason: staleness is a finding about the evidence, which is
 * what colour is reserved for. It is not a sixth verdict and the mark's own hue
 * never changes.
 */
function StaleLine({ report }: { report: MemberReport | undefined; }) {
    if (!isStale(report)) return null;
    return (
        <div className={cl("stale")} data-verdict={verdictName(Verdict.CAUTION)}>
            <span className={cl("verdict")} data-verdict={verdictName(Verdict.CAUTION)}>
                {staleTag(report)}
            </span>
            <Body measure>{staleNote(report)}</Body>
        </div>
    );
}

/**
 * The triage call.
 *
 * The rule id is never hidden and never abbreviated away: it is the handle a
 * moderator uses to look the rule up and disagree with it, which is the only
 * reason the table is a table and not a heuristic.
 */
function TriageCall({ call }: { call: Triage; }) {
    const hue = verdictName(recommendationVerdict(call.recommendation));

    return (
        <div className={cl("triage")}>
            <div className={cl("triage__hd")}>
                <span className={cl("verdict")} data-verdict={hue}>
                    {recommendationLabel(call.recommendation)}
                </span>
                <Micro>{`rule ${call.rule}`}</Micro>
            </div>
            <Micro>{recommendationMeaning(call.recommendation)}</Micro>
            <Body measure>{call.headline}</Body>
            {call.basis.length > 0 && (
                <ul className={cl("basis")}>
                    {call.basis.map((line, index) => (
                        <li className={cl("basis__item")} key={`${index}-${line.slice(0, 24)}`}>{line}</li>
                    ))}
                </ul>
            )}
        </div>
    );
}

function AccountBlock({ account, cap }: { account: RobloxAccount; cap: number; }) {
    const verdict = accountVerdict(account);
    const hue = verdictName(verdict);
    const category = categoryName(account.category);
    const confidence = confidencePercent(account.confidence);
    const reasons = Object.entries(account.reasons ?? {});
    const url = profileUrl(account);

    return (
        <div className={cl("acct")} data-verdict={hue}>
            <div className={cl("acct__hd")}>
                <Meter
                    verdict={verdict}
                    title={`${verdictLabel(verdict)} — ${flagName(account.flagType)}`}
                />
                <span className={cl("verdict")} data-verdict={hue}>{flagName(account.flagType)}</span>
                <Micro>{flagIsActionable(account.flagType) ? "actionable" : "not actionable"}</Micro>
            </div>

            <KeyVal k="account" v={`${account.username} (${account.userId})`} />
            {category != null && <KeyVal k="category" v={category} />}
            {confidence != null && <KeyVal k="confidence" v={confidence} />}
            <KeyVal k="detected via" v={sourceNames(account.sources)} />
            <KeyVal
                k="profile"
                v={
                    <a
                        className={cl("link")}
                        href={url}
                        onClick={event => {
                            event.preventDefault();
                            openExternal(url);
                        }}
                    >
                        {url}
                    </a>
                }
            />

            {reasons.map(([name, detail]) => {
                const evidence = evidenceOf(detail);
                const shown = cap > 0 ? evidence.slice(0, cap) : [];
                const hidden = evidence.length - shown.length;
                return (
                    <div className={cl("reason")} key={name}>
                        <div className={cl("reason__name")}>{name}</div>
                        <Body measure>{reasonMessage(detail)}</Body>
                        {/*
                          * Evidence is free text written by someone else's
                          * detector and it can contain anything, including
                          * markup and server names chosen by the people being
                          * flagged. It is rendered as a React text child and
                          * therefore escaped — never parsed, never as HTML.
                          */}
                        {shown.map((item, index) => (
                            <div className={cl("evidence")} key={`${index}-${item.slice(0, 24)}`}>{item}</div>
                        ))}
                        {hidden > 0 && <Micro>{`… and ${hidden} more evidence line${hidden === 1 ? "" : "s"} not shown`}</Micro>}
                    </div>
                );
            })}
        </div>
    );
}

/**
 * One line per database that was consulted, including the ones that said
 * nothing. A database that did not answer is drawn in the CAUTION hue on
 * purpose: an outage is not a clean result, and letting silence read as
 * "nothing found" is the single easiest way for this plugin to mislead someone.
 */
function SourceLine({ finding }: { finding: PartyFinding; }) {
    const label = findingLabel(finding);

    if (!finding.answered) {
        return (
            <div className={cl("source", "source--silent")} data-verdict={verdictName(Verdict.CAUTION)}>
                <span className={cl("source__name")}>{label}</span>
                {/*
                  * Reviewed and kept, twice. The CAUTION hue on this line is not
                  * chrome given a colour: a source that went quiet is a finding
                  * about the evidence, which is exactly what the design language
                  * reserves colour for. rsb/tui/app.py's detail pane paints the
                  * same sentence `style="yellow"` (app.py:2621-2625), and
                  * fidelity to the app decides it. Please
                  * do not drain it — raise it with the app instead.
                  */}
                <span className={cl("verdict")} data-verdict={verdictName(Verdict.CAUTION)}>DID NOT ANSWER</span>
                <Micro>not the same as answering "not flagged"</Micro>
            </div>
        );
    }

    if (!finding.flagged) {
        return (
            <div className={cl("source")}>
                <span className={cl("source__name")}>{label}</span>
                <span className={cl("source__state")}>
                    {finding.known ? "no detections" : "no linked Roblox account on record"}
                </span>
                {finding.score != null && <Micro>{`score ${finding.score}`}</Micro>}
            </div>
        );
    }

    // Only Rotector publishes a flag type with documented actionability, so only
    // its findings can carry a tier of their own; every other source is a
    // sighting, which is CAUTION-strength by construction.
    const verdict = finding.party === "rotector" ? verdictForFlag(finding.flagType) : Verdict.CAUTION;
    const hue = verdictName(verdict);

    return (
        <div className={cl("source")} data-verdict={hue}>
            <span className={cl("source__name")}>{label}</span>
            <span className={cl("verdict")} data-verdict={hue}>{verdictLabel(verdict)}</span>
            {finding.party === "rotector" && finding.flagType != null && (
                <Micro>{`${flagName(finding.flagType)} (type ${finding.flagType})`}</Micro>
            )}
            {finding.score != null && <Micro>{`score ${finding.score}`}</Micro>}
            {/* the source's own words, shown rather than parsed */}
            {finding.reason ? <Body measure>{finding.reason}</Body> : null}
        </div>
    );
}

function AccountsNode({ report, backendName }: { report: MemberReport; backendName: string; }) {
    const accounts = sortedAccounts(report);
    const cap = evidenceCap();

    return (
        <Node label="LINKED ROBLOX ACCOUNTS" count={accounts.length}>
            {accounts.length === 0
                // Named after whoever was actually asked. "known to Rotector"
                // under the Okappiki backend would credit a service that was
                // not the one that answered.
                ? <Empty>{`No Roblox account linked to this Discord user is known to ${backendName}.`}</Empty>
                : accounts.map(account => (
                    <AccountBlock key={`${account.userId}`} account={account} cap={cap} />
                ))}
        </Node>
    );
}

function SourcesNode({ call }: { call: Triage; }) {
    const findings = call.findings ?? [];
    const answered = findings.filter(finding => finding.answered).length;

    return (
        <Node label={`SOURCES (${answered} ANSWERED)`}>
            {findings.length > 1 && <Body measure>{SOURCES_CAPTION}</Body>}
            {findings.length === 0
                ? <Empty>No database was consulted for this member.</Empty>
                : findings.map(finding => <SourceLine key={finding.party} finding={finding} />)}
        </Node>
    );
}

function ServersNode({ report }: { report: MemberReport; }) {
    const servers = report.servers ?? [];
    if (!servers.length) return null;

    const shown = servers.slice(0, SERVER_PREVIEW);
    const hidden = servers.length - shown.length;

    return (
        <Node label="TRACKED SERVER MEMBERSHIPS" count={servers.length}>
            <Body measure>{SERVERS_CAPTION}</Body>
            {shown.map(server => {
                const tags: string[] = [];
                if (server.isTase) tags.push("TASE");
                if (server.inGracePeriod) tags.push("grace period");
                return (
                    <div className={cl("srv")} key={server.serverId || server.serverName}>
                        <span className={cl("srv__name")}>{server.serverName || "(unnamed server)"}</span>
                        <Micro>{server.serverId}</Micro>
                        {tags.length > 0 && <Micro>{`(${tags.join(", ")})`}</Micro>}
                    </div>
                );
            })}
            {hidden > 0 && <Micro>{`… and ${hidden} more`}</Micro>}
        </Node>
    );
}

/**
 * The appeal routes, one link per service whose findings this backend carries.
 *
 * Built as a flat array of keyed children rather than nested fragments because
 * the React here is Discord's and the separators have to key cleanly.
 */
function AppealLine({ backend }: { backend: string; }) {
    const routes = appealRoutes(backend);
    const children: any[] = ["Anyone listed here can appeal at "];

    routes.forEach((route, index) => {
        if (index > 0) {
            children.push(
                <span key={`sep-${route.party}`}>
                    {index === routes.length - 1 ? " or " : ", "}
                </span>
            );
        }
        children.push(
            <a
                className={cl("link")}
                key={route.party}
                href={route.url}
                onClick={event => {
                    event.preventDefault();
                    openExternal(route.url);
                }}
            >
                {appealHost(route.url)}
            </a>
        );
    });

    return <Micro>{children}</Micro>;
}

function ReportFooter({
    onCopy,
    onRecheck,
    busy,
    roblox,
    backend,
}: {
    onCopy(): void;
    onRecheck(): void;
    busy: boolean;
    roblox: RobloxAccount | null;
    backend: string;
}) {
    return (
        <div className={cl("foot")}>
            <div className={cl("actions")}>
                <Btn onClick={onCopy}>Copy report</Btn>
                <Btn quiet disabled={busy} onClick={onRecheck}>{busy ? "Re-checking" : "Re-check"}</Btn>
                {roblox && (
                    <a
                        className={cl("link", "link--action")}
                        href={profileUrl(roblox)}
                        onClick={event => {
                            event.preventDefault();
                            openExternal(profileUrl(roblox));
                        }}
                    >
                        Open Roblox profile
                    </a>
                )}
            </div>
            {/* The backend's own line, not Rotector's fixed one: an Okappiki
                report names three services in its SOURCES node and owes all
                three the credit and the appeal route. */}
            <Micro>{backendAttribution(backend)}</Micro>
            <AppealLine backend={backend} />
            <Micro>{retentionLine()}</Micro>
        </div>
    );
}

/**
 * The loading state is the frames with nothing in them yet, not a spinner.
 * Nothing in this design moves position, and an empty frame says more about
 * what is coming than a turning circle does.
 */
function LoadingState({ backendName }: { backendName: string; }) {
    return (
        <>
            <Node label="LINKED ROBLOX ACCOUNTS">
                <Meter verdict={Verdict.UNKNOWN} wide pending title="Looking this member up" />
                <Micro>{`Asking ${backendName} about this member…`}</Micro>
            </Node>
            <Node label="SOURCES">
                <Meter verdict={Verdict.UNKNOWN} wide pending title="Looking this member up" />
                <Micro>Waiting for an answer.</Micro>
            </Node>
        </>
    );
}

function ErrorState({
    error,
    busy,
    onRecheck,
    backendName,
}: {
    error: string;
    busy: boolean;
    onRecheck(): void;
    backendName: string;
}) {
    const [note, setNote] = useState<string | null>(null);
    const transport = looksLikeTransportFailure(error);

    function grant() {
        try {
            const result = requestHostPermission();
            if (result && typeof (result as any).then === "function") {
                (result as any).then(
                    (outcome: string) => setNote(String(outcome)),
                    (err: unknown) => setNote(`Could not ask ${clientName()}: ${String(err)}`)
                );
            } else if (typeof result === "string") {
                setNote(result);
            }
        } catch (err) {
            setNote(`Could not ask ${clientName()}: ${String(err)}`);
        }
    }

    return (
        <Node label="LOOKUP FAILED">
            <Body measure>{error}</Body>
            {transport && (
                <>
                    <Body measure>
                        {`The request never left ${clientName()}. That is usually the host client ` +
                            `blocking the connection rather than ${backendName} being down — both ` +
                            "backends answer browser requests with an open CORS policy."}
                    </Body>
                    {canRequestPermission() && (
                        <>
                            <Btn quiet onClick={grant}>Grant host permission</Btn>
                            <Micro>Granting it needs a full restart of the client before it takes effect.</Micro>
                        </>
                    )}
                </>
            )}
            {note && <Body measure>{note}</Body>}
            <Btn disabled={busy} onClick={onRecheck}>{busy ? "Re-checking" : "Re-check"}</Btn>
        </Node>
    );
}

// --------------------------------------------------------------------------
// the modal
// --------------------------------------------------------------------------

function ReportBody({
    userId,
    guildId,
    backend: openedFrom,
}: {
    userId: string;
    guildId?: string;
    backend?: string;
}) {
    const { report, pending, error } = useReport(userId);
    const [busy, setBusy] = useState(false);
    const alive = useRef(true);
    const asked = useRef("");

    useEffect(() => {
        alive.current = true;
        return () => {
            alive.current = false;
        };
    }, []);

    function recheck() {
        setBusy(true);
        try {
            const result = reportStore.refresh(userId);
            if (result && typeof (result as any).then === "function") {
                (result as any).then(
                    () => {
                        if (alive.current) setBusy(false);
                    },
                    (err: unknown) => {
                        logger.error(`re-check failed for ${userId}`, err);
                        if (alive.current) setBusy(false);
                    }
                );
            } else if (alive.current) {
                setBusy(false);
            }
        } catch (err) {
            logger.error(`re-check failed for ${userId}`, err);
            if (alive.current) setBusy(false);
        }
    }

    // Opening the report is an explicit request for this member, so it looks
    // them up even when automatic lookups are switched off — but only once per
    // member per mount, so a failing lookup cannot spin.
    useEffect(() => {
        if (asked.current === userId) return;
        if (report || pending) return;
        asked.current = userId;
        recheck();
    }, [userId, report, pending]);

    const identity = useIdentity(userId, guildId);
    // The call is computed whether or not it is displayed: the SOURCES node is
    // built from the same findings, and who was asked is not a preference.
    const call = report ? classify(report) : null;
    const showTriage = setting("showTriage", true);
    const roblox = report ? worstAccount(report) : null;
    const failure = error ?? report?.error ?? null;
    // A report can arrive carrying accounts *and* an error — the Discord hop
    // answered and the Roblox flag hop did not. The failure is stated first and
    // the evidence that did arrive is still shown, because burying a real
    // finding behind an error banner is as wrong as printing a partial lookup
    // as a finished one. When nothing arrived, only the failure is shown: an
    // empty accounts node would read as "Rotector knows nothing about them",
    // which is a clean result and this was not one.
    const partial = Boolean(failure) && Boolean(report?.accounts?.length);
    // Who this report is from, and therefore who the footer credits and whose
    // appeal routes it prints.
    const backend = backendOf(openedFrom, report);
    const backendName = backendLabel(backend);

    function copy() {
        copyText(summaryText(identity, report, showTriage ? call : null, backend));
    }

    // `rsb--field` paints the two-value ground. A plugin-owned surface left
    // transparent puts pure-ink type and the verdict hexes onto Discord's grey,
    // which the design language bans as a fill.
    return (
        <div className={`rsb rsb--field ${cl("report")}`}>
            <IdentityBand identity={identity} />

            {report && (
                <>
                    <VerdictLine verdict={reportVerdict(report)} meaning />
                    <StaleLine report={report} />
                    {showTriage && call && <TriageCall call={call} />}
                </>
            )}

            {failure && (
                <ErrorState
                    error={String(failure)}
                    busy={busy}
                    onRecheck={recheck}
                    backendName={backendName}
                />
            )}

            {partial && (
                <Body measure>
                    What follows is what arrived before the lookup failed. It is not the whole
                    picture — re-check before acting on it.
                </Body>
            )}

            {report && (!failure || partial) && (
                <>
                    <AccountsNode report={report} backendName={backendName} />
                    {call && <SourcesNode call={call} />}
                    <ServersNode report={report} />
                </>
            )}

            {!report && !failure && (pending || busy) && <LoadingState backendName={backendName} />}

            {!report && !failure && !pending && !busy && (
                <Node label="NOT CHECKED">
                    <Empty>This member has not been looked up yet.</Empty>
                    <Btn disabled={busy} onClick={recheck}>Check now</Btn>
                </Node>
            )}

            <ReportFooter
                onCopy={copy}
                onRecheck={recheck}
                busy={busy}
                roblox={roblox}
                backend={backend}
            />
        </div>
    );
}

function ReportModal({
    userId,
    guildId,
    backend,
    modalProps,
}: {
    userId: string;
    guildId?: string;
    backend?: string;
    modalProps: RenderModalProps;
}) {
    // The modal header is Discord's own chrome, not one of our grounds, so the
    // title cannot take the pure-ink #ffffff a `Label` forces — it would vanish
    // against a light-theme header. `rsb-modal-title` exists for exactly this,
    // and the scan modal titles itself with the same node.
    const title = <span className={`rsb ${cl("modal-title")}`}>Rotector report</span>;

    return (
        <Modal
            {...modalProps}
            size="lg"
            title={title}
        >
            {/*
              * The profile-section API wraps its children in an ErrorBoundary
              * but openModal does not, and a modal that renders nothing at all
              * is indistinguishable from "Rotector has nothing on them" — which
              * is the one misreading this plugin must never cause.
              */}
            <ErrorBoundary message="The Rotector report failed to render.">
                {/*
                  * The scroller is the modal's outermost node, above the `.rsb`
                  * root, so the `.rsb, .rsb *` reset would never reach it. It
                  * carries the base class itself or it keeps Discord's radius.
                  */}
                <ScrollerThin fade className={`rsb ${cl("scroll")}`}>
                    <ReportBody userId={userId} guildId={guildId} backend={backend} />
                </ScrollerThin>
            </ErrorBoundary>
        </Modal>
    );
}

/**
 * Open the full report for one member. Safe to call from anywhere, including a
 * context menu.
 *
 * `backend` is optional and is what the opener knows: a ledger row knows which
 * service its scan actually asked, which stays true even if the setting is
 * changed before the row is clicked. Callers that do not know it may leave it
 * out, and the modal works the answer out from the report and the settings.
 */
export function openReport(userId: string, guildId?: string, backend?: string): void {
    if (!userId) return;
    try {
        openModal(props => (
            <ReportModal userId={userId} guildId={guildId} backend={backend} modalProps={props} />
        ));
    } catch (err) {
        logger.error(`could not open the report for ${userId}`, err);
    }
}

// --------------------------------------------------------------------------
// the profile panel
// --------------------------------------------------------------------------

/**
 * The same report, compacted into the profile panel.
 *
 * It returns null whenever there is nothing to say. That is the whole design of
 * this surface: in a real server almost every member has nothing against them,
 * and a section that renders "NO DETECTIONS" on every profile trains the reader
 * to stop looking at it — which is exactly the reader you do not want when the
 * one profile that matters comes up.
 */
export function ProfileReportSection({ userId, isSideBar }: { userId: string; isSideBar: boolean; }) {
    const { report, pending, error } = useReport(userId);

    useEffect(() => {
        if (!userId) return;
        try {
            // Opt-in per settings; `request` is a no-op when automatic lookups
            // are off, which is why this surface never forces one.
            reportStore.request(userId);
        } catch (err) {
            logger.error(`could not queue a lookup for ${userId}`, err);
        }
    }, [userId]);

    if (!userId) return null;
    if (!setting("profileSection", true)) return null;

    const rootClass = `rsb ${cl("profile", isSideBar ? "profile--sidebar" : "profile--full")}`;
    // This surface has no opener to be told by, so it reads the report itself
    // and falls back to the configured backend.
    const backend = backendOf(undefined, report);
    const backendName = backendLabel(backend);

    if (error) {
        // A failed check is not a clean check, so it is said rather than hidden.
        return (
            <div className={rootClass}>
                <Micro>{`${backendName} check failed.`}</Micro>
                <Btn quiet onClick={() => openReport(userId, undefined, backend)}>Full report</Btn>
            </div>
        );
    }

    if (!report) {
        if (!pending) return null;
        if (!setting("showPending", false)) return null;
        return (
            <div className={rootClass}>
                <Meter verdict={Verdict.UNKNOWN} pending title={`Checking ${backendName}`} />
                <Micro>{`Checking ${backendName}…`}</Micro>
            </div>
        );
    }

    const verdict = reportVerdict(report);
    // Classified whether or not the call is shown: "a database flagged them"
    // decides whether this section appears at all, and that is a fact about the
    // member rather than a display preference.
    const call = classify(report);
    const showTriage = setting("showTriage", true);
    const floor = markFloor();
    const material = call ? call.material.length > 0 : false;

    // Below the floor and with nothing for a moderator to weigh: say nothing.
    if (verdict < floor && !material) return null;

    const account = worstAccount(report);

    return (
        <div className={rootClass}>
            <VerdictLine verdict={verdict} meaning />
            {/* The compact form of the same warning. This panel is where a
                moderator is standing while they decide, so an answer from
                three days ago has to say so here rather than only in the modal
                they may never open. */}
            <StaleLine report={report} />
            {showTriage && call && (
                <div className={cl("profile__row")}>
                    <span
                        className={cl("verdict")}
                        data-verdict={verdictName(recommendationVerdict(call.recommendation))}
                    >
                        {recommendationLabel(call.recommendation)}
                    </span>
                    <Micro>{`rule ${call.rule}`}</Micro>
                </div>
            )}
            {account && (
                <div className={cl("profile__row")}>
                    <span
                        className={cl("verdict")}
                        data-verdict={verdictName(accountVerdict(account))}
                    >
                        {flagName(account.flagType)}
                    </span>
                    <Micro>{`${account.username} (${account.userId})`}</Micro>
                </div>
            )}
            <Btn quiet onClick={() => openReport(userId, undefined, backend)}>Full report</Btn>
            {/*
              * Rotector's terms require the attribution, or a link to
              * rotector.com, wherever their data is acted on. This panel is not
              * a preview: it names a verdict, a recommendation, a rule id, a
              * flag type and a Roblox account, and it sits in the profile a
              * moderator is looking at while deciding — so it carries both the
              * attribution and the appeal routes itself rather than deferring
              * them to a modal the reader may never open. Under Okappiki that
              * is three services' credit and three appeal routes, because the
              * flag on screen may be any of theirs.
              */}
            <Micro>{backendAttribution(backend)}</Micro>
            <AppealLine backend={backend} />
        </div>
    );
}
