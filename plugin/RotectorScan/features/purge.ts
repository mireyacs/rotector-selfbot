/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Deleting your own messages from a conversation with a flagged account.
 * A port of rsb/purge.py, whose scope statement is carried over word for word
 * because it is the thing readers most often assume the other way round:
 *
 * this deletes **only your own messages**. Discord gives no way to remove
 * someone else's messages from a conversation, so their side stays exactly
 * where it is. What this achieves is removing *your* contributions — which is
 * the part you can actually control.
 *
 * Two phases, always in this order:
 *
 *  - `planPurge` reads the history and reports what *would* go. Nothing is
 *    deleted. This is what the modal shows before asking for confirmation,
 *    because message deletion cannot be undone.
 *  - `runPurge` deletes the planned messages, paced so Discord's per-channel
 *    delete bucket is not tripped, and stoppable part-way through.
 *
 * The preview is the whole safety mechanism, so it is enforced here rather
 * than left to the caller to remember. `runPurge` takes a `confirmation`
 * string and refuses to delete anything unless it is exactly `DELETE` — the
 * same typed word the terminal app's confirm dialog demands
 * (rsb/tui/dialogs.py:715-721). A code path that wanted to skip the preview
 * would have to type that word on the user's behalf, which is a deliberate act
 * rather than an oversight. It also re-checks that the account running the
 * deletion is the account the plan was built for, because a plan is a list of
 * message ids and message ids do not say whose they are.
 */

import { ChannelStore, Constants, RestAPI, UserStore } from "@webpack/common";

import { openPurgeModal as openModalImpl } from "../components/PurgeModal";
import { settings } from "../settings";

// A value import from "@vencord/discord-types/enums" resolves to undefined
// under the dynamic loader, so the channel types this module needs are
// declared here. The values are Discord's, quoted from the Equicord typings:
// packages/discord-types/enums/channel.ts.
const enum ChannelKind {
    DM = 1,
    GROUP_DM = 3,
}

/** messages per history page — Discord's own maximum for this endpoint */
export const PAGE_SIZE = 100;

/** pause between history pages, in ms, to stay clear of the read limit */
export const PAGE_DELAY = 350;

/** pause between deletions, in ms; Discord's per-channel delete bucket is strict */
export const DEFAULT_DELETE_DELAY = 1000;

/**
 * The word that has to be typed before anything is deleted.
 *
 * Exported so the modal cannot drift from it. A confirm box checking for a
 * different string than `runPurge` does would either block every purge or,
 * far worse, look like a gate while being none.
 */
export const CONFIRMATION_WORD = "DELETE";

/** how wide a preview line is allowed to be before it is cut */
export const PREVIEW_WIDTH = 60;

/** how many failures are recorded in full before the rest are only counted */
const MAX_ERRORS = 5;

/** how many times one request is retried after a 429 before it is given up on */
const MAX_RETRIES = 5;

/** the longest this module will ever sit waiting out a 429, in ms */
const MAX_BACKOFF_MS = 30_000;

/**
 * A rail on the history walk, not a feature.
 *
 * `maxMessages` caps *your* messages and `maxAgeDays` caps the age, but a
 * channel with years of other people's traffic and three messages of yours
 * would satisfy neither and page until the modal was closed. Two thousand
 * pages is two hundred thousand messages, which is well past the point where
 * the honest answer is "narrow the scope".
 */
const MAX_PAGES = 2000;

export type PurgeKind = "dm" | "group" | "channel";

/** Where the purge would run, and what it is called on screen. */
export interface PurgeTarget {
    channelId: string;
    label: string;
    kind: PurgeKind;
    /** the reason this channel cannot be purged, when there is one */
    blocked?: string;
}

/** One message the plan would delete. Never anything of anyone else's. */
export interface PlannedMessage {
    id: string;
    /** epoch milliseconds, so the modal can sort and format without re-parsing */
    at: number;
    /** the ISO timestamp exactly as Discord sent it */
    timestamp: string;
    preview: string;
}

export interface PurgePlan {
    channelId: string;
    label: string;
    kind: PurgeKind;
    /** the account the plan was built for; `runPurge` refuses to act for another */
    ownerId: string;
    messages: PlannedMessage[];
    /** every message read, yours and theirs — the denominator for the count */
    scanned: number;
    pages: number;
    /** false when a cap, a stop or a rail cut the walk short of the conversation's start */
    reachedEnd: boolean;
    /** true when it was the user pressing Stop that cut it short, rather than a limit */
    stoppedEarly: boolean;
    oldest?: number;
    newest?: number;
    /** when the plan was built */
    plannedAt: number;
    /** one honest sentence about what this plan covers and what it does not */
    note: string;
}

export interface PurgeProgress {
    stage: string;
    done: number;
    total: number;
}

export interface PlanOptions {
    /** cap on how many of *your* messages are collected; 0 means no cap */
    maxMessages?: number;
    /** stop the walk once history older than this is reached; 0 means all of it */
    maxAgeDays?: number;
    onProgress?(p: PurgeProgress): void;
    signal?: AbortSignal;
}

export interface RunOptions {
    /**
     * Must be exactly `CONFIRMATION_WORD`. This is the preview gate: without
     * it `runPurge` throws and deletes nothing.
     */
    confirmation: string;
    /** seconds between deletions */
    delaySeconds?: number;
    onProgress?(p: PurgeProgress): void;
    signal?: AbortSignal;
}

export interface PurgeResult {
    deleted: number;
    failed: number;
    /** already gone when we asked — a 404, which is not the same as a failure */
    missing: number;
    stopped: boolean;
    errors: string[];
}

// --------------------------------------------------------------------------
// small helpers, all of which must survive Discord's stores not being ready
// --------------------------------------------------------------------------

function attempt<T>(fn: () => T, fallback: T): T {
    try {
        const value = fn();
        return value === undefined ? fallback : value;
    } catch {
        return fallback;
    }
}

function isSnowflake(value: unknown): value is string {
    return typeof value === "string" && /^\d{15,25}$/.test(value);
}

function count(n: number): string {
    return attempt(() => Number(n).toLocaleString(), String(n));
}

/** The logged-in account's id, or "" when the client has not got that far yet. */
export function ownId(): string {
    return attempt(() => String((UserStore as any)?.getCurrentUser?.()?.id ?? ""), "");
}

/**
 * A sleep that gives up the moment the run is aborted, rather than after it.
 *
 * Every pause in this module is a second the user might spend pressing Stop on
 * a deletion, so a plain `setTimeout` promise would mean the stop button did
 * nothing for up to a whole delay interval.
 */
function wait(ms: number, signal?: AbortSignal): Promise<void> {
    if (!(ms > 0)) return Promise.resolve();
    return new Promise<void>((resolve, reject) => {
        if (signal?.aborted) {
            reject(new Error("Stopped."));
            return;
        }
        let settled = false;
        const finish = (fn: () => void) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            attempt(() => signal?.removeEventListener?.("abort", onAbort), undefined);
            fn();
        };
        const onAbort = () => finish(() => reject(new Error("Stopped.")));
        const timer = setTimeout(() => finish(resolve), ms);
        attempt(() => signal?.addEventListener?.("abort", onAbort), undefined);
    });
}

/** `95` -> `"1m 35s"`. A transliteration of `format_duration` in rsb/eta.py. */
export function formatDuration(seconds: number | null | undefined): string {
    if (seconds == null || !isFinite(Number(seconds))) return "?";
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

/**
 * One message flattened to one line.
 *
 * A port of `_preview` in rsb/purge.py. Newlines collapse so one message is one
 * row, and an attachment-only message says so rather than drawing as a blank
 * line the reader would take for a rendering fault — the preview is what the
 * confirmation is given against, so a row that says nothing is a row that
 * confirms nothing.
 */
export function previewOf(content: unknown, width: number = PREVIEW_WIDTH): string {
    const text = String(content ?? "").split(/\s+/).filter(Boolean).join(" ");
    if (!text) return "(no text — attachment or embed)";
    const limit = Math.max(4, Math.floor(width));
    return text.length <= limit ? text : `${text.slice(0, limit - 1)}…`;
}

/** The HTTP status behind a rejected RestAPI call, or 0 when it never got one. */
function statusOf(err: any): number {
    const status = Number(err?.status ?? 0);
    return Number.isFinite(status) ? status : 0;
}

/**
 * How long Discord asked us to wait, in ms.
 *
 * Unlike the Rotector client — which cannot read a single rate limit header
 * cross-origin and has to back off blind (api/rotector.ts) — this one talks to
 * Discord through the client's own REST layer, so the body of a 429 is right
 * there and the wait can be the one Discord actually asked for. Capped anyway,
 * because an unbounded sleep inside a modal is indistinguishable from a hang.
 */
function retryAfterMs(err: any, attemptNumber: number): number {
    const fromBody = Number(err?.body?.retry_after);
    const fromHeader = Number(err?.headers?.["retry-after"] ?? err?.headers?.["Retry-After"]);
    const seconds = Number.isFinite(fromBody) && fromBody > 0
        ? fromBody
        : Number.isFinite(fromHeader) && fromHeader > 0
            ? fromHeader
            : 0;
    // Discord sends seconds here, often fractional. With no usable value the
    // fallback doubles per attempt so a bare 429 still backs off.
    const ms = seconds > 0
        ? seconds * 1000
        : Math.min(MAX_BACKOFF_MS, 1000 * Math.pow(2, attemptNumber));
    return Math.max(250, Math.min(MAX_BACKOFF_MS, ms));
}

/** A sentence a person can act on, out of whatever RestAPI rejected with. */
function describeHttpError(err: any): string {
    const status = statusOf(err);
    const message = String(err?.body?.message ?? err?.message ?? "").trim();
    if (status === 401) {
        return "Discord rejected the request as unauthenticated. Reload the client and try again.";
    }
    if (status === 403) {
        return "Discord refused: this account may not read or delete here.";
    }
    if (status === 404) {
        return "That conversation no longer exists, or this account is no longer in it.";
    }
    if (status === 429) {
        return "Discord is rate limiting this account. Raise the delay between deletions and try again.";
    }
    if (status >= 500) return `Discord is having trouble (${status}). Try again shortly.`;
    if (status > 0) return message ? `Discord returned ${status}: ${message}` : `Discord returned ${status}.`;
    return message || "The request did not reach Discord.";
}

// --------------------------------------------------------------------------
// naming the target
// --------------------------------------------------------------------------

/**
 * What conversation this is, in the words the confirm screen will use.
 *
 * The label matters more than it looks: it is what the confirmation sentence
 * names, and a deletion confirmed against "this conversation" is a deletion
 * nobody actually checked. When the client cannot name the channel that is
 * reported as a block rather than papered over with a generic word.
 */
export function describeTarget(channelId: string): PurgeTarget {
    if (!isSnowflake(channelId)) {
        return {
            channelId: String(channelId ?? ""),
            label: "this conversation",
            kind: "channel",
            blocked: "That is not a channel this plugin can read.",
        };
    }

    const channel = attempt(() => (ChannelStore as any)?.getChannel?.(channelId), undefined) as any;
    if (!channel) {
        return {
            channelId,
            label: "this conversation",
            kind: "channel",
            blocked:
                "This client does not have that conversation loaded, so it cannot say whose it is "
                + "or what it is called. Open it once, then try again.",
        };
    }

    const type = Number(channel.type);

    if (type === ChannelKind.DM) {
        const recipients: string[] = Array.isArray(channel.recipients) ? channel.recipients : [];
        const other = recipients.length === 1 ? String(recipients[0]) : "";
        const user = other ? attempt(() => (UserStore as any)?.getUser?.(other), undefined) as any : undefined;
        const name = user?.globalName || user?.username || (other ? `id ${other}` : "someone");
        return { channelId, label: `DM with ${name}`, kind: "dm" };
    }

    if (type === ChannelKind.GROUP_DM) {
        const named = (typeof channel.name === "string" && channel.name.trim())
            ? channel.name.trim()
            : attempt(() => {
                const raw = Array.isArray(channel.rawRecipients) ? channel.rawRecipients : [];
                return raw.map((r: any) => r?.username).filter(Boolean).join(", ");
            }, "") || "Group DM";
        return { channelId, label: `group ${named}`, kind: "group" };
    }

    const name = (typeof channel.name === "string" && channel.name) ? `#${channel.name}` : "this channel";
    return { channelId, label: name, kind: "channel" };
}

/**
 * The existing DM channel with one person, or undefined.
 *
 * Deliberately a lookup rather than an open, exactly as `find_dm_channel` in
 * rsb/discord/http.py is: `POST /users/@me/channels` *creates* the channel as a
 * side effect, and opening a conversation with somebody in order to delete a
 * conversation that never existed is not what anybody wants.
 * `ChannelStore.getDMFromUserId` reads what the client already has and creates
 * nothing.
 */
export function existingDmChannelId(userId: string): string | undefined {
    if (!isSnowflake(userId)) return undefined;
    const id = attempt(() => (ChannelStore as any)?.getDMFromUserId?.(userId), undefined);
    return isSnowflake(id) ? id : undefined;
}

/**
 * The scope sentence for one kind of conversation.
 *
 * Three different facts, and putting the wrong one on screen would be the whole
 * failure. A group DM purge takes out everything you said to everyone in it,
 * not only what you said to the person you came here about; and a server
 * channel purge is a visible act in a shared room rather than a private tidy-up.
 */
export function scopeNote(kind: PurgeKind): string {
    if (kind === "group") {
        return (
            "Only your own messages are deleted, and this is a group DM — so this removes every "
            + "message you sent to the group, not only the ones aimed at one person. Everyone else's "
            + "messages stay exactly where they are; Discord gives no way to remove them."
        );
    }
    if (kind === "channel") {
        return (
            "Only your own messages are deleted. This is a shared channel rather than a private "
            + "conversation, so everyone who can read it will see your side of it disappear. "
            + "Everyone else's messages stay exactly where they are."
        );
    }
    return (
        "Only your own messages are deleted. Discord provides no way to remove someone else's "
        + "messages from a conversation, so their side stays exactly where it is. What this removes "
        + "is your contributions, which is the part you actually control."
    );
}

// --------------------------------------------------------------------------
// settings
// --------------------------------------------------------------------------

/** A finite number inside `[min, max]`, or the fallback. Never NaN. */
function num(value: unknown, fallback: number, min: number, max: number): number {
    const parsed = typeof value === "number" ? value : Number(value);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.min(max, Math.max(min, parsed));
}

/**
 * The configured pacing and caps.
 *
 * Read inside a function and never at module scope: `settings.store` throws
 * until the plugin is registered, and this module is imported by a context menu
 * that can be built at any point during startup. Every key has a working
 * default here, so a settings block that has not been extended yet is a shrug
 * rather than a crash.
 */
export function purgeSettings(): { delaySeconds: number; maxMessages: number; maxAgeDays: number; } {
    const store = attempt(() => (settings?.store as unknown as Record<string, any>) ?? {}, {});
    return {
        // Zero is allowed — it is the user's account and their call — but the
        // ceiling stops a mistyped digit turning one purge into an hour of them.
        delaySeconds: num(store.purgeDelay, DEFAULT_DELETE_DELAY / 1000, 0, 60),
        maxMessages: Math.floor(num(store.purgeMaxMessages, 0, 0, 1_000_000)),
        maxAgeDays: Math.floor(num(store.purgeMaxAgeDays, 0, 0, 36_500)),
    };
}

// --------------------------------------------------------------------------
// phase one: the plan. Deletes nothing.
// --------------------------------------------------------------------------

/** One page of history, newest first, with 429s waited out rather than thrown. */
async function readPage(channelId: string, before: string | undefined, signal?: AbortSignal): Promise<any[]> {
    const endpoint = (Constants as any)?.Endpoints?.MESSAGES;
    if (typeof endpoint !== "function") {
        throw new Error("This Discord build does not expose the channel messages endpoint.");
    }
    if (typeof (RestAPI as any)?.get !== "function") {
        throw new Error("Discord's REST client is not available yet. Try again in a moment.");
    }

    const query: Record<string, any> = { limit: PAGE_SIZE };
    if (before) query.before = before;

    for (let attemptNumber = 0; ; attemptNumber++) {
        if (signal?.aborted) throw new Error("Stopped.");
        try {
            const response = await RestAPI.get({ url: endpoint(channelId), query });
            const body = response?.body;
            return Array.isArray(body) ? body : [];
        } catch (err: any) {
            if (signal?.aborted) throw new Error("Stopped.");
            const status = statusOf(err);
            if (status === 429 && attemptNumber < MAX_RETRIES) {
                await wait(retryAfterMs(err, attemptNumber), signal);
                continue;
            }
            if (status >= 500 && attemptNumber < MAX_RETRIES) {
                await wait(Math.min(MAX_BACKOFF_MS, 1000 * Math.pow(2, attemptNumber)), signal);
                continue;
            }
            throw new Error(describeHttpError(err));
        }
    }
}

/**
 * Walk the history and collect your own messages. Deletes nothing.
 *
 * A port of `plan_purge` in rsb/purge.py, including its two stopping rules:
 * `maxMessages` caps how many of *your* messages are collected, and
 * `maxAgeDays` stops the walk once history older than that is reached. History
 * comes back newest-first, so the first message past the cutoff means every
 * message after it is past it too — which is why the age check ends the walk
 * rather than merely skipping that message.
 */
export async function planPurge(channelId: string, opts: PlanOptions = {}): Promise<PurgePlan> {
    const target = describeTarget(channelId);
    if (target.blocked) throw new Error(target.blocked);

    const me = ownId();
    if (!me) {
        throw new Error("The client has not finished logging in, so it cannot tell which messages are yours.");
    }

    const maxMessages = Math.max(0, Math.floor(Number(opts.maxMessages) || 0));
    const maxAgeDays = Math.max(0, Math.floor(Number(opts.maxAgeDays) || 0));
    const cutoff = maxAgeDays > 0 ? Date.now() - maxAgeDays * 86_400_000 : 0;

    const plan: PurgePlan = {
        channelId: target.channelId,
        label: target.label,
        kind: target.kind,
        ownerId: me,
        messages: [],
        scanned: 0,
        pages: 0,
        reachedEnd: true,
        stoppedEarly: false,
        plannedAt: Date.now(),
        note: scopeNote(target.kind),
    };

    let before: string | undefined;

    for (;;) {
        if (opts.signal?.aborted) {
            plan.reachedEnd = false;
            plan.stoppedEarly = true;
            break;
        }
        if (plan.pages >= MAX_PAGES) {
            plan.reachedEnd = false;
            break;
        }

        // A stop mid-page keeps whatever was already collected rather than
        // throwing it away. Nothing here has been deleted, so a partial plan is
        // a useful answer — it just has to say that it is partial, which is
        // what `stoppedEarly` and the note below are for.
        let page: any[];
        try {
            page = await readPage(target.channelId, before, opts.signal);
        } catch (err) {
            if (opts.signal?.aborted) {
                plan.reachedEnd = false;
                plan.stoppedEarly = true;
                break;
            }
            throw err;
        }
        if (!page.length) break;
        plan.pages++;
        plan.scanned += page.length;

        let stop = false;
        for (const message of page) {
            const timestamp = String(message?.timestamp ?? "");
            const at = timestamp ? Date.parse(timestamp) : NaN;

            if (cutoff && Number.isFinite(at) && at < cutoff) {
                // Older than the window, and everything below it is older
                // still. This is the end of the walk, not a skipped message.
                stop = true;
                break;
            }

            const author = String(message?.author?.id ?? "");
            if (author !== me) continue;

            const id = String(message?.id ?? "");
            if (!id) continue;

            plan.messages.push({
                id,
                at: Number.isFinite(at) ? at : 0,
                timestamp,
                preview: previewOf(message?.content),
            });

            if (maxMessages && plan.messages.length >= maxMessages) {
                plan.reachedEnd = false;
                stop = true;
                break;
            }
        }

        try {
            opts.onProgress?.({
                stage: `Reading history of ${target.label}`,
                done: plan.messages.length,
                total: plan.scanned,
            });
        } catch {
            // a progress callback that throws must not abort a read that is
            // itself harmless — nothing has been deleted at this point
        }

        if (stop) break;
        if (page.length < PAGE_SIZE) break;

        const nextBefore = String(page[page.length - 1]?.id ?? "");
        if (!nextBefore || nextBefore === before) {
            // Discord handed back a page that does not advance the cursor.
            // Carrying on would re-read it forever.
            plan.reachedEnd = false;
            break;
        }
        before = nextBefore;

        try {
            await wait(PAGE_DELAY, opts.signal);
        } catch {
            plan.reachedEnd = false;
            plan.stoppedEarly = true;
            break;
        }
    }

    const stamps = plan.messages.map(m => m.at).filter(at => at > 0).sort((a, b) => a - b);
    if (stamps.length) {
        plan.oldest = stamps[0];
        plan.newest = stamps[stamps.length - 1];
    }

    plan.note = planScopeNote(plan);
    return plan;
}

/**
 * The plan's own sentence: the scope note, plus the caveat when it was cut short.
 *
 * A stop and a limit both truncate the walk, but they are not the same fact and
 * a user who pressed Stop should not be told their configured caps did it.
 */
function planScopeNote(plan: PurgePlan): string {
    const parts = [scopeNote(plan.kind)];
    if (plan.stoppedEarly) {
        parts.push(
            "You stopped the preview part-way, so this plan covers only the history read up to that "
            + "point — there may be older messages of yours that are not in it."
        );
    } else if (!plan.reachedEnd) {
        parts.push(
            "The walk stopped before the start of the conversation because of the limits set, so "
            + "there may be older messages of yours that are not in this plan."
        );
    }
    return parts.join(" ");
}

/** A port of `PurgePlan.describe()` — the one-line summary the modal prints. */
export function describePlan(plan: PurgePlan | null | undefined): string {
    if (!plan) return "No plan yet.";
    if (!plan.messages.length) return `No messages of yours in ${plan.label}.`;
    const day = (at?: number) => (at ? attempt(() => new Date(at).toISOString().slice(0, 10), "") : "");
    const from = day(plan.oldest);
    const to = day(plan.newest);
    const span = from && to ? `, ${from} to ${to}` : "";
    const tail = plan.reachedEnd
        ? ""
        : plan.stoppedEarly
            ? " (history cut short when you stopped the preview)"
            : " (history truncated by the limits set)";
    return (
        `${count(plan.messages.length)} of your messages in ${plan.label} `
        + `(out of ${count(plan.scanned)} scanned${span})${tail}`
    );
}

/** How long running this plan should take at the given pacing, in seconds. */
export function estimateSeconds(plan: PurgePlan | null | undefined, delaySeconds: number): number {
    const total = plan?.messages?.length ?? 0;
    if (!total) return 0;
    // The last message has no wait after it, and each delete costs a round trip
    // the delay does not cover — call that a fifth of a second apiece.
    const gaps = Math.max(0, total - 1);
    return gaps * Math.max(0, Number(delaySeconds) || 0) + total * 0.2;
}

/** One line per message, in the order the run will delete them: oldest first. */
export function orderedForDeletion(plan: PurgePlan | null | undefined): PlannedMessage[] {
    if (!plan?.messages?.length) return [];
    return [...plan.messages].sort((a, b) => (a.at || 0) - (b.at || 0));
}

/** `2026-08-16 14:03:11` — the shape `PlannedMessage.when` produces in the TUI. */
export function whenOf(message: PlannedMessage | null | undefined): string {
    const raw = String(message?.timestamp ?? "");
    if (raw) return raw.slice(0, 19).replace("T", " ");
    const at = Number(message?.at ?? 0);
    if (!at) return "";
    return attempt(() => new Date(at).toISOString().slice(0, 19).replace("T", " "), "");
}

// --------------------------------------------------------------------------
// phase two: the deletion. Only ever after a confirmation.
// --------------------------------------------------------------------------

/** One delete, with a 429 waited out. Reports what happened rather than throwing for it. */
async function deleteOne(
    channelId: string,
    messageId: string,
    signal?: AbortSignal
): Promise<"deleted" | "missing"> {
    const endpoint = (Constants as any)?.Endpoints?.MESSAGE;
    if (typeof endpoint !== "function") {
        throw new Error("This Discord build does not expose the message endpoint.");
    }
    if (typeof (RestAPI as any)?.del !== "function") {
        throw new Error("Discord's REST client is not available yet.");
    }

    for (let attemptNumber = 0; ; attemptNumber++) {
        if (signal?.aborted) throw new Error("Stopped.");
        try {
            // The request goes through the client's own REST layer, which is
            // where its auth, its buckets and its retries live — but it is made
            // here rather than through `MessageActions.deleteMessage` so that
            // the deleted/failed counts this returns are the actual HTTP
            // outcomes. A purge that reports "42 deleted" has to have watched
            // 42 responses; the action creator makes the same call and also
            // updates the message store, but it decides for itself what to
            // surface. Nothing is lost by not using it: the gateway sends
            // MESSAGE_DELETE back to the client that did the deleting, so an
            // open channel updates on its own.
            await RestAPI.del({ url: endpoint(channelId, messageId) });
            return "deleted";
        } catch (err: any) {
            if (signal?.aborted) throw new Error("Stopped.");
            const status = statusOf(err);
            // Already gone. Counting that as a failure would send a user
            // hunting for a message that no longer exists.
            if (status === 404) return "missing";
            if (status === 429 && attemptNumber < MAX_RETRIES) {
                await wait(retryAfterMs(err, attemptNumber), signal);
                continue;
            }
            if (status >= 500 && attemptNumber < MAX_RETRIES) {
                await wait(Math.min(MAX_BACKOFF_MS, 1000 * Math.pow(2, attemptNumber)), signal);
                continue;
            }
            throw new Error(describeHttpError(err));
        }
    }
}

/**
 * Delete the planned messages, oldest first, paced and interruptible.
 *
 * A port of `execute_purge` in rsb/purge.py, with the same ordering argument:
 * oldest first, so that if it is interrupted what survives is the recent tail,
 * which is the part you are most likely to still want a record of.
 *
 * The `confirmation` check is not decoration. `planPurge` is the only thing
 * that shows a user what would go and this is the only thing that makes it go,
 * so the word the confirm screen collects is required down here, where the
 * deletion actually happens, rather than only in a UI that any other caller
 * could route around.
 */
export async function runPurge(plan: PurgePlan, opts: RunOptions): Promise<PurgeResult> {
    const result: PurgeResult = { deleted: 0, failed: 0, missing: 0, stopped: false, errors: [] };

    if (!plan || !Array.isArray(plan.messages)) {
        throw new Error("There is no plan to run. Preview the messages first.");
    }
    if (String(opts?.confirmation ?? "").trim() !== CONFIRMATION_WORD) {
        throw new Error(
            `Nothing was deleted: the purge was not confirmed. Type ${CONFIRMATION_WORD} on the `
            + "confirmation screen, which is only shown after the preview."
        );
    }

    // A plan is a list of message ids, and message ids do not carry an owner —
    // the one thing that made them yours was the account they were collected
    // under. If that has changed since, through a switched account or a
    // re-login, this plan is about somebody else's messages and must not run.
    const me = ownId();
    if (!me || me !== plan.ownerId) {
        throw new Error(
            "The logged-in account has changed since this plan was made. Nothing was deleted — "
            + "preview again so the messages can be re-checked as yours."
        );
    }

    const total = plan.messages.length;
    if (!total) return result;

    const delayMs = Math.max(0, (Number(opts.delaySeconds) || 0) * 1000);
    const ordered = orderedForDeletion(plan);

    for (let index = 0; index < ordered.length; index++) {
        if (opts.signal?.aborted) {
            result.stopped = true;
            break;
        }

        const message = ordered[index];
        try {
            const outcome = await deleteOne(plan.channelId, message.id, opts.signal);
            if (outcome === "missing") result.missing++;
            else result.deleted++;
        } catch (err: any) {
            if (opts.signal?.aborted) {
                result.stopped = true;
                break;
            }
            // One failure must not strand the run: the remaining messages are
            // still yours and still deletable.
            result.failed++;
            if (result.errors.length < MAX_ERRORS) {
                result.errors.push(`${message.id}: ${String(err?.message ?? err)}`);
            }
        }

        try {
            opts.onProgress?.({
                stage: `Deleting your messages in ${plan.label}`,
                done: index + 1,
                total,
            });
        } catch {
            // a progress callback that throws must not strand a run that is
            // already deleting things
        }

        if (index < ordered.length - 1 && delayMs) {
            try {
                await wait(delayMs, opts.signal);
            } catch {
                result.stopped = true;
                break;
            }
        }
    }

    return result;
}

/** A port of `PurgeResult.describe()`. */
export function describeResult(result: PurgeResult | null | undefined): string {
    if (!result) return "Nothing ran.";
    const bits = [`${count(result.deleted)} deleted`];
    if (result.missing) bits.push(`${count(result.missing)} already gone`);
    if (result.failed) bits.push(`${count(result.failed)} could not be deleted`);
    if (result.stopped) bits.push("stopped early");
    return bits.join(", ");
}

// --------------------------------------------------------------------------
// the modal
// --------------------------------------------------------------------------

/**
 * Open the purge modal for one channel.
 *
 * The screen itself lives in components/PurgeModal.tsx; this is here because
 * it is where the contract says to look for it, and because a caller holding
 * this module has no reason to also know which component file draws it. The
 * import is circular — PurgeModal imports `planPurge` from here — which is
 * safe for the same reason settings.ts and store.ts can import each other:
 * the loader caches a module before evaluating it, nothing in either file runs
 * at module scope, and sucrase's output reads each binding off the namespace
 * object at call time rather than copying it at import time.
 */
export function openPurgeModal(channelId: string): void {
    try {
        openModalImpl(channelId);
    } catch (err) {
        console.error("[RotectorScan] could not open the purge modal", err);
    }
}
