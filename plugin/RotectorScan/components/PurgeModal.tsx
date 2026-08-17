/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The purge screen: the terminal app's two purge dialogs
 * (rsb/tui/dialogs.py:571-721) as one modal that walks forward and cannot walk
 * around itself.
 *
 * The order of the phases is the safety mechanism, not a layout choice:
 *
 *   scope -> planning -> confirm -> running -> done
 *
 * The Delete button exists only in `confirm`, `confirm` is only reachable from
 * a plan that has already been read out of Discord and printed on screen, and
 * `runPurge` refuses to act at all without the word the confirm step collects.
 * So there is no path — not a keyboard shortcut, not a re-render, not another
 * caller of this file — that deletes a message the user has not been shown.
 *
 * The scope sentence is repeated at every phase rather than stated once at the
 * top. It is the single most misread thing about this feature: only your own
 * messages go, because Discord provides no way to remove anyone else's, and a
 * user who scrolled past that line at the start is a user who will read the
 * result as having cleaned up the whole conversation.
 */

import ErrorBoundary from "@components/ErrorBoundary";
import { Modal, openModal, ScrollerThin, useEffect, useMemo, useRef, useState } from "@webpack/common";

import {
    CONFIRMATION_WORD,
    describePlan,
    describeResult,
    describeTarget,
    estimateSeconds,
    formatDuration,
    orderedForDeletion,
    planPurge,
    purgeSettings,
    runPurge,
    scopeNote,
    whenOf,
} from "../features/purge";
import type { PlannedMessage, PurgePlan, PurgeProgress, PurgeResult, PurgeTarget } from "../features/purge";
import { Body, Btn, cl, Label, Micro, Node } from "./primitives";

/** how many planned messages are drawn before the list has to be asked for */
const PREVIEW_ROWS = 12;

/** how many more each "Show more" adds; a five-figure list is not a thing to render at once */
const PREVIEW_STEP = 100;

/** the most rows this modal will ever draw — past this the count is the honest summary */
const PREVIEW_CAP = 1000;

// --------------------------------------------------------------------------
// helpers
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
    return attempt(() => Number(n).toLocaleString(), String(n));
}

/** A whole number typed into a text field, or the fallback when it is not one. */
function intFrom(raw: string, fallback: number): number {
    const text = String(raw ?? "").trim();
    if (!text) return fallback;
    const parsed = Number(text);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.max(0, Math.floor(parsed));
}

/** The same, for the delay, which is allowed a fraction. */
function floatFrom(raw: string, fallback: number): number {
    const text = String(raw ?? "").trim();
    if (!text) return fallback;
    const parsed = Number(text);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.max(0, parsed);
}

/**
 * One labelled input.
 *
 * A plain `<input>` rather than Discord's TextInput: the host component brings
 * its own corner radius and its own filled grey panel, and this design language
 * has neither — zero radius everywhere, and tone from a hairline rather than a
 * tinted surface. Everything it needs is that hairline and the mono stack,
 * which `.rsb-input` in style.css provides.
 *
 * There is deliberately no placeholder anywhere in this modal. A placeholder is
 * grey text sitting where a value goes, and on the confirmation field that
 * would draw the word DELETE into an empty box — the one piece of text in this
 * whole feature that must never appear unless a person typed it. The hint line
 * says what the field wants instead, in full ink, where it cannot be mistaken
 * for a value.
 */
function Field({
    label,
    hint,
    value,
    onChange,
    disabled,
    id,
}: {
    label: string;
    hint?: string;
    value: string;
    onChange(next: string): void;
    disabled?: boolean;
    id: string;
}) {
    return (
        <div className={`rsb ${cl("form__row")}`}>
            <label className={cl("label")} htmlFor={id}>{label}</label>
            {hint && <span className={cl("form__hint")}>{hint}</span>}
            <input
                id={id}
                type="text"
                autoComplete="off"
                spellCheck={false}
                className={`rsb ${cl("input")}`}
                value={value}
                disabled={Boolean(disabled)}
                onChange={(e: any) => onChange(String(e?.target?.value ?? ""))}
            />
        </div>
    );
}

/** One planned message: when it was sent, and what it says. */
function PreviewRow({ message }: { message: PlannedMessage; }) {
    return (
        <div className={`rsb ${cl("preview__row")}`}>
            <span className={cl("preview__when")}>{whenOf(message)}</span>
            {/* Evidence is free text from someone's own message and is rendered
                as text, never as markup — the same rule the report modal
                applies to Rotector's evidence lines. */}
            <span className={cl("preview__text")}>{message.preview}</span>
        </div>
    );
}

// --------------------------------------------------------------------------
// the modal
// --------------------------------------------------------------------------

export function openPurgeModal(channelId: string): void {
    openModal(props => <PurgeModal modalProps={props} channelId={channelId} />);
}

type Phase = "scope" | "planning" | "confirm" | "running" | "done";

function PurgeModal({ modalProps, channelId }: { modalProps: any; channelId: string; }) {
    const target: PurgeTarget = useMemo(
        () => attempt(
            () => describeTarget(channelId),
            { channelId, label: "this conversation", kind: "channel", blocked: "This conversation could not be read." } as PurgeTarget
        ),
        [channelId]
    );

    const defaults = useMemo(
        () => attempt(() => purgeSettings(), { delaySeconds: 1, maxMessages: 0, maxAgeDays: 0 }),
        []
    );

    const [phase, setPhase] = useState<Phase>("scope");
    const [days, setDays] = useState(String(defaults.maxAgeDays));
    const [cap, setCap] = useState(String(defaults.maxMessages));
    const [delay, setDelay] = useState(String(defaults.delaySeconds));

    const [plan, setPlan] = useState<PurgePlan | undefined>(undefined);
    const [result, setResult] = useState<PurgeResult | undefined>(undefined);
    const [progress, setProgress] = useState<PurgeProgress | undefined>(undefined);
    const [error, setError] = useState<string | undefined>(undefined);

    const [typed, setTyped] = useState("");
    const [rows, setRows] = useState(PREVIEW_ROWS);

    const [startedAt, setStartedAt] = useState(0);
    const [now, setNow] = useState(0);

    const abortRef = useRef<AbortController | undefined>(undefined);
    const aliveRef = useRef(true);

    // Abort whatever is in flight when the modal goes away. A deletion the user
    // can no longer see is a deletion they can no longer stop, and this one
    // keeps running for as long as there are messages left in the plan.
    useEffect(() => {
        aliveRef.current = true;
        return () => {
            aliveRef.current = false;
            attempt(() => abortRef.current?.abort(), undefined);
        };
    }, []);

    // One clock, ticking only while something is running, so a paced delete —
    // which spends most of its time deliberately waiting — never reads as a hang.
    useEffect(() => {
        if (phase !== "planning" && phase !== "running") return;
        const timer = setInterval(() => {
            if (aliveRef.current) setNow(Date.now());
        }, 1000);
        return () => clearInterval(timer);
    }, [phase]);

    const delaySeconds = floatFrom(delay, defaults.delaySeconds);
    const elapsed = startedAt ? Math.max(0, ((now || Date.now()) - startedAt) / 1000) : 0;

    // ---- phase one: build the plan. Deletes nothing. ----------------------

    async function preview() {
        const controller = new AbortController();
        abortRef.current = controller;
        setError(undefined);
        setPlan(undefined);
        setResult(undefined);
        setTyped("");
        setRows(PREVIEW_ROWS);
        setPhase("planning");
        setProgress({ stage: `Reading history of ${target.label}`, done: 0, total: 0 });
        setStartedAt(Date.now());
        setNow(Date.now());

        try {
            const built = await planPurge(channelId, {
                maxMessages: intFrom(cap, defaults.maxMessages),
                maxAgeDays: intFrom(days, defaults.maxAgeDays),
                signal: controller.signal,
                onProgress: p => {
                    if (aliveRef.current) setProgress(p);
                },
            });
            if (!aliveRef.current) return;
            setPlan(built);
            // A plan with nothing in it still goes to the confirm screen, which
            // prints "No messages of yours in …" and offers no Delete button.
            // Bouncing straight back to the form would look like the preview
            // had failed rather than like it had answered.
            setPhase("confirm");
        } catch (err: any) {
            if (!aliveRef.current) return;
            setPhase("scope");
            setError(controller.signal.aborted
                ? "Stopped before anything was read. Nothing has been deleted."
                : String(err?.message ?? err));
        }
    }

    function stopPreview() {
        attempt(() => abortRef.current?.abort(), undefined);
    }

    // ---- phase two: the deletion. Only from the confirm screen. -----------

    async function commit() {
        if (!plan?.messages.length) return;
        // Belt as well as braces: the button is disabled until this matches,
        // and `runPurge` checks it again on the other side of the call.
        if (typed.trim() !== CONFIRMATION_WORD) return;

        const controller = new AbortController();
        abortRef.current = controller;
        setError(undefined);
        setResult(undefined);
        setPhase("running");
        setProgress({ stage: `Deleting your messages in ${plan.label}`, done: 0, total: plan.messages.length });
        setStartedAt(Date.now());
        setNow(Date.now());

        try {
            const outcome = await runPurge(plan, {
                confirmation: typed.trim(),
                delaySeconds,
                signal: controller.signal,
                onProgress: p => {
                    if (aliveRef.current) setProgress(p);
                },
            });
            if (!aliveRef.current) return;
            setResult(outcome);
            setPhase("done");
        } catch (err: any) {
            if (!aliveRef.current) return;
            setError(String(err?.message ?? err));
            setPhase("done");
        }
    }

    function stopRun() {
        attempt(() => abortRef.current?.abort(), undefined);
    }

    // ---- derived ----------------------------------------------------------

    const ordered = useMemo(() => attempt(() => orderedForDeletion(plan), [] as PlannedMessage[]), [plan]);
    const shown = ordered.slice(0, Math.min(rows, PREVIEW_CAP));
    const estimate = useMemo(
        () => attempt(() => estimateSeconds(plan, delaySeconds), 0),
        [plan, delaySeconds]
    );

    const done = progress?.done ?? 0;
    const total = progress?.total ?? 0;
    const pct = total > 0 ? Math.min(100, Math.max(0, (done / total) * 100)) : 0;
    const confirmed = typed.trim() === CONFIRMATION_WORD;

    // ---- the pieces -------------------------------------------------------

    /**
     * The scope statement, in an inverted frame.
     *
     * Emphasis here is weight and inversion rather than a colour: the five
     * verdict hexes are the only colour this plugin emits and they mean
     * "evidence about a person". A warning painted in one of them would be
     * claiming to be a finding, and a sixth hue invented for warnings would
     * stop the other five reading as evidence at all.
     */
    const scope = (
        <div className={`rsb rsb--invert ${cl("warn")}`}>
            <Label>Scope</Label>
            {/* Once a plan exists its own note is used, because it carries the
                same sentence plus whatever caveat the walk earned — a run cut
                short by the caps has to say so beside the count it produced. */}
            <Body measure>{target.blocked ? target.blocked : (plan?.note || openingScope(target))}</Body>
        </div>
    );

    const blocked = (
        <Node label="Cannot purge here">
            <div className={cl("stack")}>
                <Body measure>{target.blocked}</Body>
                <div className={cl("actions")}>
                    <Btn quiet onClick={() => modalProps?.onClose?.()}>Close</Btn>
                </div>
            </div>
        </Node>
    );

    const form = (
        <>
            <Node label="Conversation">
                <div className={cl("stack")}>
                    <Body>{target.label}</Body>
                    <Micro>
                        Nothing is deleted by previewing. This reads the history and shows you what
                        would go; the deletion is a separate, confirmed step.
                    </Micro>
                </div>
            </Node>

            <Node label="How far back">
                <div className={cl("form")}>
                    <Field
                        id="rsb-purge-days"
                        label="Days of history"
                        hint="0 = all of it. The walk stops once it reaches history older than this."
                        value={days}
                        onChange={setDays}
                    />
                    <Field
                        id="rsb-purge-cap"
                        label="Most messages to remove"
                        hint="0 = no cap. Counts your messages only, not the ones read past."
                        value={cap}
                        onChange={setCap}
                    />
                </div>
            </Node>

            <Node label="Pacing">
                <div className={cl("form")}>
                    <Field
                        id="rsb-purge-delay"
                        label="Seconds between deletions"
                        hint={
                            "Discord's per-channel delete limit is strict; going faster mostly buys 429s "
                            + "and a slower purge overall."
                        }
                        value={delay}
                        onChange={setDelay}
                    />
                </div>
            </Node>

            <div className={cl("actions")}>
                <Btn onClick={() => void preview()}>Preview</Btn>
                <Btn quiet onClick={() => modalProps?.onClose?.()}>Cancel</Btn>
            </div>
        </>
    );

    const planning = (
        <Node label="Reading history">
            <div className={cl("stack")}>
                <Micro>
                    {(progress?.stage ?? "Reading history") + "  "}
                    {count(done)} of yours out of {count(total)} read
                    {"  elapsed "}{formatDuration(elapsed)}
                </Micro>
                <Body measure>
                    Nothing has been deleted. This is the preview pass: it reads the conversation,
                    picks out the messages you sent, and stops there.
                </Body>
                <div className={cl("actions")}>
                    <Btn onClick={stopPreview}>Stop</Btn>
                </div>
            </div>
        </Node>
    );

    const confirm = plan && (
        <>
            <Node label="Confirm deletion" count={plan.messages.length}>
                <div className={cl("stack")}>
                    <Body measure>{describePlan(plan)}</Body>
                    <Micro>
                        {count(plan.scanned)} message(s) read across {count(plan.pages)} page(s).
                        {plan.messages.length
                            ? `  Estimated time: ${formatDuration(estimate)}.`
                            : ""}
                    </Micro>
                    {plan.messages.length > 0 && (
                        <div className={`rsb rsb--invert ${cl("warn")}`}>
                            <Label>This cannot be undone</Label>
                            <Body measure>
                                {count(plan.messages.length)} of your messages will be permanently deleted
                                from {plan.label}. Discord keeps no copy you can restore from, and neither
                                does this plugin.
                            </Body>
                        </div>
                    )}
                </div>
            </Node>

            {plan.messages.length > 0 && (
                <Node label="Oldest first" count={ordered.length}>
                    <div className={cl("stack")}>
                        <Micro>
                            Deletion runs in this order, so a run you stop part-way leaves the recent
                            tail rather than a random scatter.
                        </Micro>
                        <ScrollerThin className={cl("scroll")}>
                            <div className={cl("preview")}>
                                {shown.map(message => (
                                    <PreviewRow key={message.id} message={message} />
                                ))}
                            </div>
                        </ScrollerThin>
                        {ordered.length > shown.length && (
                            <>
                                <Micro>… and {count(ordered.length - shown.length)} more</Micro>
                                {shown.length < PREVIEW_CAP && (
                                    <div className={cl("actions")}>
                                        <Btn quiet onClick={() => setRows(r => Math.min(PREVIEW_CAP, r + PREVIEW_STEP))}>
                                            Show more
                                        </Btn>
                                    </div>
                                )}
                            </>
                        )}
                    </div>
                </Node>
            )}

            <Node label={`Type ${CONFIRMATION_WORD} to confirm`}>
                <div className={cl("stack")}>
                    {plan.messages.length === 0
                        ? (
                            <>
                                <Body measure>
                                    There is nothing of yours to remove here, so there is nothing to confirm.
                                </Body>
                                <div className={cl("actions")}>
                                    <Btn quiet onClick={() => setPhase("scope")}>Back</Btn>
                                    <Btn quiet onClick={() => modalProps?.onClose?.()}>Close</Btn>
                                </div>
                            </>
                        )
                        : (
                            <>
                                <div className={cl("form")}>
                                    <Field
                                        id="rsb-purge-confirm"
                                        label="Confirmation"
                                        hint={`Typing ${CONFIRMATION_WORD} exactly is what starts the deletion. Nothing else does.`}
                                        value={typed}
                                        onChange={setTyped}
                                    />
                                </div>
                                <div className={cl("actions")}>
                                    <Btn disabled={!confirmed} onClick={() => void commit()}>
                                        Delete {count(plan.messages.length)} message(s)
                                    </Btn>
                                    <Btn quiet onClick={() => { setPhase("scope"); setTyped(""); }}>
                                        Back
                                    </Btn>
                                    <Btn quiet onClick={() => modalProps?.onClose?.()}>Cancel</Btn>
                                </div>
                            </>
                        )}
                </div>
            </Node>
        </>
    );

    const running = (
        <Node label="Deleting">
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
                    {(progress?.stage ?? "Deleting") + "  "}
                    {count(done)}/{count(total)}
                    {"  elapsed "}{formatDuration(elapsed)}
                </Micro>
                <div className={cl("actions")}>
                    <Btn onClick={stopRun}>Stop</Btn>
                </div>
                <Micro>
                    Stopping is clean: it happens between messages, and what has already been deleted
                    is reported rather than lost.
                </Micro>
            </div>
        </Node>
    );

    const finished = (
        <Node label="Result">
            <div className={cl("stack")}>
                {/* `runPurge` only throws from its two gates, both of which fire
                    before the first request, so a run with no result is a run
                    that deleted nothing — and saying so plainly matters more
                    here than anywhere else in the plugin. */}
                <Body measure>
                    {result
                        ? `${plan ? `${plan.label}: ` : ""}${describeResult(result)}.`
                        : "Nothing was deleted."}
                </Body>
                {/* The last word on screen is the scope, because this is the
                    sentence a user carries away from the feature. */}
                {Boolean(result?.deleted) && (
                    <Body measure>
                        Their messages are untouched — Discord allows no other way.
                    </Body>
                )}
                {Boolean(result?.missing) && (
                    <Micro>
                        {count(result!.missing)} were already gone before this ran. That is not a failure;
                        they had been deleted some other way.
                    </Micro>
                )}
                {Boolean(result?.stopped) && (
                    <Micro>
                        Stopped early, so the more recent part of your side of the conversation is still there.
                    </Micro>
                )}
                {Boolean(result?.errors?.length) && (
                    <div className={cl("error")}>
                        <Label>Could not be deleted</Label>
                        {result!.errors.map(line => <Micro key={line}>{line}</Micro>)}
                        {result!.failed > result!.errors.length && (
                            <Micro>… and {count(result!.failed - result!.errors.length)} more</Micro>
                        )}
                    </div>
                )}
                <div className={cl("actions")}>
                    <Btn quiet onClick={() => { setPhase("scope"); setPlan(undefined); setTyped(""); setResult(undefined); }}>
                        Start again
                    </Btn>
                    <Btn quiet onClick={() => modalProps?.onClose?.()}>Close</Btn>
                </div>
            </div>
        </Node>
    );

    return (
        <Modal
            {...modalProps}
            size="lg"
            title={<span className={`rsb ${cl("modal-title")}`}>Purge your messages</span>}
        >
            <ErrorBoundary>
                {/*
                  * `rsb--field` paints the two-value ground. A plugin-owned
                  * surface that stays transparent puts #ffffff ink onto
                  * Discord's grey, which is the grey fill the design language
                  * bans outright.
                  */}
                <div className={`rsb rsb--field ${cl("purge")}`}>
                    {error && (
                        <div className={cl("error")}>
                            <Label>Failed</Label>
                            <Body measure>{error}</Body>
                        </div>
                    )}

                    {scope}

                    {target.blocked
                        ? blocked
                        : (
                            <>
                                {phase === "scope" && form}
                                {phase === "planning" && planning}
                                {phase === "confirm" && confirm}
                                {phase === "running" && running}
                                {phase === "done" && finished}
                            </>
                        )}

                    <div className={cl("foot")}>
                        <Micro>
                            This removes your own messages only. It is not a report, it does not tell
                            Discord anything, and it does not undo a conversation.
                        </Micro>
                        <Micro>
                            Anyone you have acted on because of a Rotector finding can appeal at rotector.com
                        </Micro>
                    </div>
                </div>
            </ErrorBoundary>
        </Modal>
    );
}

/**
 * The scope sentence before a plan exists.
 *
 * It comes from the same function the plan uses, so the two can never disagree
 * about what a group DM purge covers — and it has to be per-kind, because the
 * group-DM and shared-channel cases say materially different things from a DM.
 * The literal fallback is the DM wording, which is the narrowest of the three:
 * a module that failed to load must not be the reason somebody under-reads the
 * scope of a group purge, and the modal still names the conversation above it.
 */
function openingScope(target: PurgeTarget): string {
    return attempt(() => scopeNote(target.kind), "")
        || (
            "Only your own messages are deleted. Discord provides no way to remove someone else's "
            + "messages from a conversation, so their side stays exactly where it is."
        );
}
