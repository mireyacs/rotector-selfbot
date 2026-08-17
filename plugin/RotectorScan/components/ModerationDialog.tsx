/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Confirming a kick or a ban.
 *
 * A port of rsb/tui/dialogs.py's ModerationDialog, which describes itself as:
 * "The dialog always shows the exact reason that will be written to the audit
 * log, and refuses to proceed on a non-actionable finding until the operator
 * explicitly acknowledges that Rotector does not support the action." Both
 * halves of that are the whole design and both are here.
 *
 * The plan is computed before anything is offered, so the members the finding
 * does not support — and the members Discord will not let this account touch —
 * are shown set aside rather than discovered halfway through a run. Bulk
 * moderation is exactly where a mistake is least recoverable, so the count of
 * what would happen is on screen before the button that makes it happen.
 *
 * Nothing here decides anything. `features/moderation.ts` owns the two rules
 * from Rotector's terms (attribution on every reason, an override named as an
 * override) and enforces them again at the point of acting; this file's job is
 * to state them plainly and collect the acknowledgement.
 */

import ErrorBoundary from "@components/ErrorBoundary";
import { copyWithToast } from "@utils/discord";
import { Logger } from "@utils/Logger";
import { GuildStore, Modal, openModal, ScrollerThin, useEffect, useMemo, useRef, useState, UserStore } from "@webpack/common";

import { recommendationLabel, verdictFloor as recommendationTier } from "../api/triage";
import { emptyReport, isStale, MemberReport, reportAge, shortAge, staleFor, staleNote } from "../api/types";
import { ATTRIBUTION, Verdict, verdictLabel, verdictName } from "../api/verdict";
import {
    ACTION_WORDS,
    ActionKind,
    ActionPlan,
    ActionPolicy,
    ActionResult,
    ActionTarget,
    actionPolicyFromSettings,
    buildNotice,
    creatorsAvailable,
    DEFAULT_NOTICE,
    deleteMessageSecondsSetting,
    describePlan,
    MAX_REASON,
    notifyBeforeActionSetting,
    planAction,
    planTotal,
    runAction,
} from "../features/moderation";
import { retainBeyondTerms, retentionNotice } from "../settings";
import { reportStore } from "../store";
import { Body, Btn, cl, Empty, Label, Meter, Micro, Node, VerdictLine } from "./primitives";
import { openReport } from "./ReportModal";

const logger = new Logger("RotectorScan");

const APPEAL_URL = "https://rotector.com";

/*
 * Not "memory only" since the scan history arrived: a finished scan is written
 * to the plugin's own database on this computer. The deadline is what matters
 * here, and it is 23 hours from the scan that produced the finding — unless the
 * operator lifted that ceiling with `retainBeyondTerms`, in which case saying
 * otherwise on the screen where a ban is confirmed would be the worst place in
 * the plugin to be wrong. Read fresh, never frozen into a constant.
 */
function retentionNoticeText(): string {
    if (!attempt(() => retainBeyondTerms(), false)) {
        return (
            "Findings are dropped within 24 hours — Rotector's terms forbid keeping their " +
            "data longer, and flag statuses change. What you are acting on here is held in " +
            "memory; a finished scan is also stored on this computer until 23 hours after " +
            "it started."
        );
    }
    const notice = attempt(
        () => retentionNotice(),
        "Findings are being kept past the 24-hour window Rotector's Terms of Use allow, by choice."
    );
    return (
        `${notice} What you are acting on here is held in memory; a finished scan is also stored ` +
        "on this computer, under the same ceiling."
    );
}

/**
 * Why nothing is DMed from here.
 *
 * rsb/config.py restricts `notify_before_action` to bot tokens in its own
 * words, and the plugin acts through the logged-in account, so the notice is
 * composed and handed over rather than sent.
 */
const NOTICE_EXPLANATION =
    "The terminal app only sends this from a bot account. A user account " +
    "messaging strangers in bulk is the pattern Discord's spam heuristics are " +
    "built to catch, and the penalty lands on your own account - so this plugin " +
    "writes the notice and leaves the sending to you. Send it before you act: " +
    "once somebody is banned there is no shared server left and a DM can no " +
    "longer reach them.";

/** how many rows the target table prints before it summarises the rest */
const TARGET_PREVIEW = 40;

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

function count(n: number): string {
    return attempt(() => n.toLocaleString(), String(n));
}

function memberHandle(id: string): string {
    return attempt(() => {
        const user = (UserStore as any)?.getUser?.(id);
        return user?.username ? `@${user.username}` : `id ${id}`;
    }, `id ${id}`);
}

/**
 * Open a URL the way the host client wants it opened. A bare `<a href>` inside
 * the renderer can navigate Discord itself, which would drop whatever the
 * moderator was in the middle of.
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

function copyText(text: string, toast: string): void {
    try {
        const result = copyWithToast(text, toast);
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

/** The server's own name, for the notice. Its id is the honest fallback. */
function guildName(guildId: string): string {
    if (!guildId) return "this server";
    const name = attempt(() => {
        const guild = (GuildStore as any)?.getGuild?.(guildId);
        return typeof guild?.name === "string" ? guild.name : null;
    }, null);
    return name || `server ${guildId}`;
}

// --------------------------------------------------------------------------
// pieces
// --------------------------------------------------------------------------

/** A checkbox with its sentence. The sentence is the control, not a caption beside one. */
function Check({
    checked,
    onChange,
    disabled,
    children,
}: {
    checked: boolean;
    onChange(value: boolean): void;
    disabled?: boolean;
    children: any;
}) {
    return (
        <label className={`rsb ${cl("check")}`}>
            <input
                type="checkbox"
                className={cl("check__box")}
                checked={Boolean(checked)}
                disabled={Boolean(disabled)}
                onChange={event => {
                    try {
                        onChange(Boolean(event?.currentTarget?.checked));
                    } catch (err) {
                        logger.error("checkbox handler failed", err);
                    }
                }}
            />
            <span className={cl("check__text")}>{children}</span>
        </label>
    );
}

/**
 * One row per member the plan would touch, and one per member it set aside.
 *
 * The rule id travels with every row. It is the handle a moderator uses to look
 * the decision up and disagree with it, and a list of names with no citation
 * beside them is the thing this project exists not to produce.
 */
function TargetTable({
    label,
    targets,
    guildId,
    blocked,
}: {
    label: string;
    targets: ActionTarget[];
    guildId: string;
    blocked?: boolean;
}) {
    if (!targets.length) return null;

    const shown = targets.slice(0, TARGET_PREVIEW);
    const hidden = targets.length - shown.length;

    return (
        <Node label={label} count={targets.length}>
            <div className={cl("ledger__wrap")}>
                <table className={cl("ledger")}>
                    <thead>
                        <tr>
                            <th scope="col">Member</th>
                            <th scope="col">Verdict</th>
                            <th scope="col">{blocked ? "Why not" : "Basis"}</th>
                        </tr>
                    </thead>
                    <tbody>
                        {shown.map(target => {
                            const hue = verdictName(target.verdict);
                            const why = !target.permission.ok
                                ? target.permission.detail
                                : target.eligibility.explanation;
                            return (
                                <tr
                                    className={cl("row")}
                                    key={target.id}
                                    tabIndex={0}
                                    role="button"
                                    onClick={() => openReport(target.id, guildId)}
                                    onKeyDown={event => {
                                        if (event.key === "Enter" || event.key === " ") {
                                            event.preventDefault();
                                            openReport(target.id, guildId);
                                        }
                                    }}
                                >
                                    <th scope="row">
                                        {target.label}
                                        <span className={cl("row__handle")}>{memberHandle(target.id)}</span>
                                    </th>
                                    <td className={cl("ledger__verdict")} data-verdict={hue}>
                                        <Meter
                                            verdict={target.verdict}
                                            title={`${verdictLabel(target.verdict)} — ${target.label}`}
                                        />
                                        <span className={cl("verdict")} data-verdict={hue}>
                                            {verdictLabel(target.verdict)}
                                        </span>
                                    </td>
                                    <td>
                                        {target.eligibility.call && (
                                            <span
                                                className={cl("rec")}
                                                data-verdict={verdictName(
                                                    recommendationTier(target.eligibility.recommendation)
                                                )}
                                            >
                                                {recommendationLabel(target.eligibility.recommendation)}
                                            </span>
                                        )}
                                        <span className={cl("row__handle")}>{why}</span>
                                    </td>
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
            </div>
            {hidden > 0 && <Micro>{`… and ${count(hidden)} more`}</Micro>}
        </Node>
    );
}

/**
 * The override notice.
 *
 * Named for what it is, in the CAUTION hue, because the hue here is evidence:
 * it is the tier of the finding being overridden, not decoration on a warning.
 */
function OverrideNotice({
    plan,
    acknowledged,
    onAcknowledge,
    typed,
    onTyped,
    needsWord,
}: {
    plan: ActionPlan;
    acknowledged: boolean;
    onAcknowledge(value: boolean): void;
    typed: string;
    onTyped(value: string): void;
    needsWord: boolean;
}) {
    const word = plan.kind.toUpperCase();
    const hue = verdictName(Verdict.CAUTION);

    return (
        <div className={cl("error")} data-verdict={hue}>
            <span className={cl("verdict")} data-verdict={hue}>NOT SUPPORTED BY THE FINDING</span>
            <Body measure>
                {`${count(plan.overrides.length)} of the ${count(plan.targets.length)} ` +
                    "members below would be actioned on a finding Rotector does not document " +
                    "as safe to act on. That is possible, and it is a deliberate override - " +
                    "not routine."}
            </Body>
            <ul className={cl("basis")}>
                {plan.overrides.slice(0, TARGET_PREVIEW).map(target => (
                    <li className={cl("basis__item")} key={target.id}>
                        {`${target.label}: ${target.eligibility.explanation}`}
                    </li>
                ))}
            </ul>
            <Check checked={acknowledged} onChange={onAcknowledge}>
                I understand Rotector does not support this action
            </Check>
            {needsWord && (
                <>
                    <Body measure>
                        {"This includes a CAUTION finding. Rotector found something and " +
                            "deliberately did not conclude from it, so this may well be wrong " +
                            "about a real person. Type the word below to confirm you have " +
                            "decided that yourself."}
                    </Body>
                    <input
                        className={cl("input")}
                        type="text"
                        value={typed}
                        placeholder={`type ${word} to confirm`}
                        aria-label={`Type ${word} to confirm`}
                        onChange={event => onTyped(String(event?.currentTarget?.value ?? ""))}
                    />
                </>
            )}
        </div>
    );
}

// --------------------------------------------------------------------------
// the dialog
// --------------------------------------------------------------------------

type Phase = "confirm" | "running" | "done";

function ModerationBody({
    kind,
    reports,
    guildId,
    close,
}: {
    kind: ActionKind;
    reports: MemberReport[];
    guildId: string;
    close(): void;
}) {
    const words = ACTION_WORDS[kind] ?? ACTION_WORDS.ban;

    const [phase, setPhase] = useState<Phase>("confirm");
    const [mode, setMode] = useState<"auto" | "custom">("auto");
    const [custom, setCustom] = useState("");
    const [seconds, setSeconds] = useState(() =>
        String(kind === "ban" ? attempt(() => deleteMessageSecondsSetting(), 0) : 0)
    );
    const [acknowledged, setAcknowledged] = useState(false);
    const [typed, setTyped] = useState("");
    const [results, setResults] = useState<ActionResult[]>([]);
    const [failure, setFailure] = useState<string | null>(null);
    const abort = useRef<AbortController | null>(null);
    const alive = useRef(true);

    /**
     * The run must not outlive the dialog.
     *
     * A Discord modal is dismissable at any moment — Escape, a backdrop click,
     * the client navigating — and the Stop button only exists while the modal
     * is on screen. Without this, closing the dialog mid-run would leave
     * `runAction` looping: kicking or banning people with nobody watching the
     * results, and with no way to stop it short of reloading the client. For a
     * destructive, irreversible action that is the worst thing this file could
     * do, so unmount aborts first and the liveness flag then keeps anything
     * still in flight from resolving into a tree that is gone.
     */
    useEffect(() => {
        alive.current = true;
        return () => {
            alive.current = false;
            try {
                abort.current?.abort();
            } catch {
                // already gone
            }
            abort.current = null;
        };
    }, []);

    const basePolicy: Partial<ActionPolicy> = useMemo(
        () => attempt(() => actionPolicyFromSettings(), {} as ActionPolicy),
        []
    );

    // Recomputed whenever the reason changes, because the reason is part of the
    // plan: every target carries the exact text that will be written for them,
    // and a preview built separately from the thing that runs would be a
    // preview of something else.
    const plan: ActionPlan = useMemo(
        () =>
            attempt(
                () =>
                    planAction(kind, reports, guildId, {
                        ...basePolicy,
                        custom: mode === "custom" ? custom : undefined,
                    }),
                {
                    kind,
                    guildId,
                    targets: [],
                    reason: "",
                    blocked: [],
                    overrides: [],
                    permission: { ok: false, detail: "The plan could not be built." },
                } as ActionPlan
            ),
        [kind, reports, guildId, basePolicy, mode, custom]
    );

    const total = planTotal(plan);
    const needsWord = plan.overrides.some(target => target.eligibility.needsDoubleConfirm);
    const wordOk = !needsWord || typed.trim().toUpperCase() === plan.kind.toUpperCase();
    const overrideOk = plan.overrides.length === 0 || (acknowledged && wordOk);
    const creators = attempt(() => creatorsAvailable(), false);
    const ready = plan.permission.ok && plan.targets.length > 0 && overrideOk && creators;

    const previewTarget = plan.targets[0] ?? plan.blocked[0] ?? null;
    const previewReason = previewTarget?.reason ?? "";

    /**
     * The members this action would touch whose finding is already past the
     * terms' 24-hour window, oldest first.
     *
     * Built here rather than in features/moderation.ts because an `ActionTarget`
     * carries the decision and not the answer it came from — the reports are in
     * this component's own props, and the age is a fact about those.
     */
    const staleTargets = useMemo(() => {
        const held = new Map<string, MemberReport>();
        for (const report of reports) {
            if (report?.discordId) held.set(String(report.discordId), report);
        }
        const out: { id: string; label: string; age: string; short: string; note: string; ms: number; }[] = [];
        for (const target of plan.targets) {
            const report = held.get(String(target.id));
            if (!attempt(() => isStale(report), false)) continue;
            out.push({
                id: target.id,
                label: target.label,
                age: attempt(() => staleFor(report), ""),
                short: attempt(() => shortAge(reportAge(report)), ""),
                note: attempt(() => staleNote(report), ""),
                ms: attempt(() => reportAge(report), 0),
            });
        }
        // Oldest first: the sentence below quotes the range, and the worst end
        // of it is the one that decides whether a re-check is due.
        return out.sort((a, b) => b.ms - a.ms);
    }, [reports, plan.targets]);

    const noticeText = useMemo(() => {
        const report = reports.find(r => r?.discordId === previewTarget?.id) ?? reports[0];
        if (!report) return "";
        return attempt(
            () =>
                buildNotice(
                    report,
                    words.past,
                    guildName(guildId),
                    DEFAULT_NOTICE,
                    basePolicy.appeal ?? null
                ),
            ""
        );
    }, [reports, previewTarget, guildId, words.past, basePolicy]);

    function stop() {
        try {
            abort.current?.abort();
        } catch {
            // already gone
        }
    }

    function run() {
        if (!ready) return;
        const controller = attempt(() => new AbortController(), null as any);
        abort.current = controller;
        setResults([]);
        setFailure(null);
        setPhase("running");

        const parsed = Number(seconds);
        const deleteSeconds = Number.isFinite(parsed) ? Math.max(0, Math.min(604800, parsed)) : 0;

        runAction(plan, guildId, {
            acknowledgedOverride: acknowledged || plan.overrides.length === 0,
            deleteMessageSeconds: kind === "ban" ? deleteSeconds : 0,
            signal: controller?.signal,
            onProgress: (_done, _all, result) => {
                // `runAction` breaks its loop on abort and still resolves, so
                // every one of these three has to check: after the dialog is
                // dismissed there is no tree left to write into.
                if (!alive.current) return;
                setResults(previous => [...previous, result]);
            },
        }).then(
            finished => {
                if (!alive.current) return;
                setResults(finished);
                setPhase("done");
            },
            err => {
                if (!alive.current) return;
                setFailure(String((err as any)?.message ?? err));
                setPhase("done");
            }
        );
    }

    function copyResults() {
        const lines = [`${words.past}: ${results.filter(r => r.ok).length} of ${results.length}`];
        for (const result of results) {
            lines.push(
                `${result.ok ? "OK  " : "FAIL"} ${result.label} (${result.id}) - ${result.reason}` +
                (result.error ? ` [${result.error}]` : "")
            );
        }
        lines.push(ATTRIBUTION);
        copyText(lines.join("\n"), "Action log copied to the clipboard.");
    }

    if (!reports.length) {
        return (
            <div className={`rsb rsb--field ${cl("mod")}`}>
                <Empty>{`There is nobody to ${words.verb}.`}</Empty>
            </div>
        );
    }

    // ----------------------------------------------------------------------
    // running / finished
    // ----------------------------------------------------------------------

    if (phase !== "confirm") {
        const done = results.length;
        const target = plan.targets.length || 1;
        const pct = Math.min(100, Math.round((done / target) * 100));
        const succeeded = results.filter(r => r.ok).length;

        return (
            <div className={`rsb rsb--field ${cl("mod")}`}>
                <div className={cl("stack")}>
                    <Label>{phase === "running" ? words.gerund : "Finished"}</Label>
                    <div
                        className={cl("bar")}
                        role="progressbar"
                        aria-valuemin={0}
                        aria-valuemax={100}
                        aria-valuenow={pct}
                    >
                        <div className={cl("bar__fill")} style={{ width: `${pct}%` }} />
                    </div>
                    <Micro>{`${count(done)} of ${count(plan.targets.length)} — ${count(succeeded)} succeeded`}</Micro>

                    {failure && (
                        <div className={cl("error")}>
                            <Label>NOTHING WAS SENT</Label>
                            <Body measure>{failure}</Body>
                        </div>
                    )}

                    <Node label="RESULTS" count={results.length}>
                        {results.length === 0
                            ? <Empty>Nothing has been sent yet.</Empty>
                            : results.map(result => (
                                <div
                                    className={cl("result", !result.ok && "result--failed")}
                                    key={result.id}
                                >
                                    <span className={cl("result__state")}>
                                        {result.ok ? words.past : "failed"}
                                    </span>
                                    <span className={cl("result__name")}>{result.label}</span>
                                    <Micro>{`id ${result.id}`}</Micro>
                                    {result.error ? <Body measure>{result.error}</Body> : null}
                                    {/* the exact text Discord was given, so the audit log can be checked against it */}
                                    <div className={cl("evidence")}>{result.reason}</div>
                                </div>
                            ))}
                    </Node>

                    <div className={cl("actions")}>
                        {phase === "running"
                            ? <Btn quiet onClick={stop}>Stop</Btn>
                            : <Btn onClick={close}>Close</Btn>}
                        <Btn quiet disabled={!results.length} onClick={copyResults}>Copy action log</Btn>
                    </div>
                </div>

                <Footer />
            </div>
        );
    }

    // ----------------------------------------------------------------------
    // the confirm step
    // ----------------------------------------------------------------------

    return (
        <div className={`rsb rsb--field ${cl("mod")}`}>
            <div className={cl("stack")}>
                <div className={cl("title")}>
                    {total === 1
                        ? `${words.title} ${previewTarget?.label ?? "member"}`
                        : `${words.title} ${count(total)} members`}
                </div>
                <Body measure>{describePlan(plan)}</Body>

                {previewTarget && total === 1 && <VerdictLine verdict={previewTarget.verdict} meaning />}

                {/*
                  * A finding older than the terms' 24-hour window, named at the
                  * step where it matters most. Acting on a three-day-old flag
                  * is exactly the case the appeal argument is about: appeals
                  * succeed and flag types change, so the record in hand can be
                  * describing somebody it is no longer true of — and a kick or
                  * a ban is the one thing here that cannot be taken back by
                  * re-checking afterwards. Zero unless the retention ceiling
                  * has been lifted, or this dialog has been open a very long
                  * time.
                  */}
                {staleTargets.length > 0 && (
                    <div className={cl("warn")}>
                        <Label>
                            {staleTargets.length === 1
                                ? "THIS FINDING IS OLDER THAN 24 HOURS"
                                : `${count(staleTargets.length)} OF THESE FINDINGS ARE OLDER THAN 24 HOURS`}
                        </Label>
                        <Body measure>
                            {staleTargets.length === 1
                                ? staleTargets[0].note
                                : `These were answered between ${staleTargets[staleTargets.length - 1].age} and `
                                  + `${staleTargets[0].age} ago, past the window Rotector's Terms of Use allow. `
                                  + "Flag types change and appeals succeed, so each of them describes the day it "
                                  + "was answered rather than today. Re-check before acting: opening a row above "
                                  + "asks the backend again."}
                        </Body>
                        <div className={cl("stack")}>
                            {staleTargets.slice(0, TARGET_PREVIEW).map(entry => (
                                <Micro key={entry.id}>
                                    <span className={cl("verdict")} data-verdict={verdictName(Verdict.CAUTION)}>
                                        {`STALE ${entry.short}`}
                                    </span>
                                    {`  ${entry.label}  ${memberHandle(entry.id)}`}
                                </Micro>
                            ))}
                            {staleTargets.length > TARGET_PREVIEW && (
                                <Micro>{`… and ${count(staleTargets.length - TARGET_PREVIEW)} more`}</Micro>
                            )}
                        </div>
                    </div>
                )}

                {!creators && (
                    <div className={cl("error")}>
                        <Label>THIS CLIENT CANNOT BE ASKED TO DO IT</Label>
                        <Body measure>
                            {"Discord's own ban and kick actions could not be found in this build, " +
                                "so the plugin cannot guarantee the audit-log reason would be " +
                                "recorded. Rotector's terms require an action taken on their data " +
                                "to attribute them, so nothing will be sent. Copy the reason below " +
                                "and act through Discord's own menu instead."}
                        </Body>
                    </div>
                )}

                {!plan.permission.ok && (
                    <div className={cl("error")}>
                        <Label>YOU CANNOT DO THIS HERE</Label>
                        <Body measure>{plan.permission.detail}</Body>
                    </div>
                )}

                <TargetTable
                    label={`WILL BE ${words.past.toUpperCase()}`}
                    targets={plan.targets}
                    guildId={guildId}
                />
                <TargetTable
                    label="SET ASIDE"
                    targets={plan.blocked}
                    guildId={guildId}
                    blocked
                />

                {/* -- the reason ------------------------------------------- */}
                <Node label="AUDIT LOG REASON">
                    <div className={cl("actions")}>
                        <Btn quiet={mode !== "auto"} onClick={() => setMode("auto")}>Automatic</Btn>
                        <Btn quiet={mode !== "custom"} onClick={() => setMode("custom")}>My own words</Btn>
                    </div>
                    {mode === "custom" && (
                        <input
                            className={cl("input")}
                            type="text"
                            value={custom}
                            placeholder="Your own reason…"
                            aria-label="Your own reason"
                            onChange={event => setCustom(String(event?.currentTarget?.value ?? ""))}
                        />
                    )}
                    <Micro>Discord will record:</Micro>
                    {/* free text, shown rather than parsed — and this is the exact string */}
                    <div className={cl("evidence")}>{previewReason}</div>
                    <Micro>
                        {`${previewReason.length} of ${MAX_REASON} characters. The appeal link is added ` +
                            "even to a reason you wrote yourself: Rotector's terms require an " +
                            "action taken on their data to attribute them, so the person it lands " +
                            "on can appeal to the database that flagged them."}
                    </Micro>
                    {total > 1 && (
                        <Micro>
                            {"Shown for the first member. Each of the others gets the same wording " +
                                "naming their own finding."}
                        </Micro>
                    )}
                    <Btn
                        quiet
                        onClick={() => copyText(previewReason, "Reason copied to the clipboard.")}
                    >
                        Copy reason
                    </Btn>
                </Node>

                {/* -- ban only --------------------------------------------- */}
                {kind === "ban" && (
                    <Node label="DELETE RECENT MESSAGES">
                        <input
                            className={cl("input")}
                            type="number"
                            min={0}
                            max={604800}
                            value={seconds}
                            aria-label="Seconds of message history to delete"
                            onChange={event => setSeconds(String(event?.currentTarget?.value ?? "0"))}
                        />
                        <Micro>
                            {"Seconds of their message history Discord deletes with the ban. " +
                                "0 keeps everything; 604800 is one week, Discord's maximum. " +
                                "Deleted messages cannot be recovered, including any evidence in them."}
                        </Micro>
                    </Node>
                )}

                {/* -- the notice ------------------------------------------- */}
                <Node label="TELLING THEM FIRST">
                    <Body measure>{NOTICE_EXPLANATION}</Body>
                    {notifyBeforeActionSetting() && (
                        <Micro>
                            {"You have \"notify before action\" switched on. It is honoured as far " +
                                "as a user account may honour it: the notice is written for you, " +
                                "and nothing is sent automatically."}
                        </Micro>
                    )}
                    {noticeText ? <div className={cl("evidence")}>{noticeText}</div> : null}
                    <Btn
                        quiet
                        disabled={!noticeText}
                        onClick={() => copyText(noticeText, "Notice copied to the clipboard.")}
                    >
                        Copy notice
                    </Btn>
                </Node>

                {/* -- the override ----------------------------------------- */}
                {plan.overrides.length > 0 && (
                    <OverrideNotice
                        plan={plan}
                        acknowledged={acknowledged}
                        onAcknowledge={setAcknowledged}
                        typed={typed}
                        onTyped={setTyped}
                        needsWord={needsWord}
                    />
                )}

                <div className={cl("actions")}>
                    <Btn quiet onClick={close}>Cancel</Btn>
                    <Btn disabled={!ready} onClick={run}>
                        {`${words.title} ${count(plan.targets.length)}`}
                    </Btn>
                </div>
                {!ready && plan.permission.ok && creators && (
                    <Micro>
                        {plan.targets.length === 0
                            ? "Nothing here is actionable."
                            : "Acknowledge the override above before this can proceed."}
                    </Micro>
                )}
            </div>

            <Footer />
        </div>
    );
}

function Footer() {
    return (
        <div className={cl("foot")}>
            <Micro>{ATTRIBUTION}</Micro>
            <Micro>
                <>
                    {"Anyone actioned here can appeal at "}
                    <a
                        className={cl("link")}
                        href={APPEAL_URL}
                        onClick={event => {
                            event.preventDefault();
                            openExternal(APPEAL_URL);
                        }}
                    >
                        rotector.com
                    </a>
                    {" — the appeal goes to Rotector, not to this server."}
                </>
            </Micro>
            <Micro>{retentionNoticeText()}</Micro>
        </div>
    );
}

function ModerationDialogModal({
    kind,
    reports,
    guildId,
    modalProps,
}: {
    kind: ActionKind;
    reports: MemberReport[];
    guildId: string;
    modalProps: any;
}) {
    const words = ACTION_WORDS[kind] ?? ACTION_WORDS.ban;
    const title = (
        <span className={`rsb ${cl("modal-title")}`}>
            {`${words.title} from Rotector findings`}
        </span>
    );

    function close() {
        try {
            modalProps?.onClose?.();
        } catch (err) {
            logger.error("could not close the moderation dialog", err);
        }
    }

    return (
        <Modal {...modalProps} size="lg" title={title}>
            {/*
              * openModal does not wrap its children, and a moderation dialog
              * that rendered as a blank rectangle is one where the operator
              * cannot see who they are about to act on.
              */}
            <ErrorBoundary message="The Rotector moderation dialog failed to render. Nothing was sent.">
                {/* the scroller sits above the `.rsb` root, so it carries the base class itself */}
                <ScrollerThin fade className={`rsb ${cl("scroll")}`}>
                    <ModerationBody kind={kind} reports={reports} guildId={guildId} close={close} />
                </ScrollerThin>
            </ErrorBoundary>
        </Modal>
    );
}

/**
 * Confirm and run a kick or ban against these members' findings.
 *
 * Safe to call from anywhere — a context menu, the scan ledger, a command.
 * Nothing happens until the dialog's own confirm step, and nothing at all
 * happens for a member the finding does not support without the override being
 * ticked in front of the operator.
 */
export function openModerationDialog(
    kind: ActionKind,
    reports: MemberReport[],
    guildId: string
): void {
    const safeKind: ActionKind = kind === "kick" ? "kick" : "ban";
    const list = Array.isArray(reports) ? reports.filter(r => r != null && Boolean(r.discordId)) : [];
    try {
        openModal(props => (
            <ModerationDialogModal
                kind={safeKind}
                reports={list}
                guildId={String(guildId ?? "")}
                modalProps={props}
            />
        ));
    } catch (err) {
        logger.error("could not open the moderation dialog", err);
    }
}

/**
 * The same dialog, opened from member ids.
 *
 * A member with no report yet gets an empty one rather than being dropped: the
 * triage table answers "no database answered, so nothing was learned" for it,
 * which is the honest state and lands them in the set-aside list where the
 * operator can see why.
 */
export function openModerationDialogForIds(
    kind: ActionKind,
    ids: string[],
    guildId: string
): void {
    const reports: MemberReport[] = [];
    for (const id of Array.isArray(ids) ? ids : []) {
        if (!id) continue;
        const held = attempt(() => reportStore.get(id), undefined);
        reports.push(held ?? emptyReport(id));
    }
    openModerationDialog(kind, reports, guildId);
}
