/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * A second socket to Discord's gateway, opened by the plugin, used for one
 * thing: enumerating a guild's members properly. Ported from
 * rsb/discord/gateway.py.
 *
 * Why this exists at all. `GuildMemberStore` is a cache of what the client has
 * already been sent — roughly the sidebar window the user has scrolled, plus
 * their friends — so scanning "this server" from it checks a sample and reports
 * it as a server. members.ts says that in as many words. The app solves it the
 * way the real client does, and this is that solution carried over:
 *
 *  - **OP 14** (guild subscriptions, the "lazy request") asks for slices of the
 *    member sidebar and the server answers with GUILD_MEMBER_LIST_UPDATE
 *    dispatches carrying SYNC ops. This is the primary path for a user account.
 *    The caveat is Discord's, not ours: in a large guild the sidebar only lists
 *    non-offline members, so a scrape covers who is *visible*, not everyone.
 *  - **OP 8** (REQUEST_GUILD_MEMBERS) is the fallback, and for a bot — or for an
 *    account holding kick/ban/manage-roles/administrator — it is the whole job:
 *    an empty query with `limit: 0` returns the entire roster, offline members
 *    included, in one request. Without that permission it is brute-forced over a
 *    prefix alphabet, deepened only where a query comes back saturated.
 *
 * Renderer specifics that differ from the Python:
 *
 *  - The browser `WebSocket` is used, with `encoding=json` and **no
 *    compression**. `zlib-stream` needs a streaming inflate the renderer does
 *    not have, and `?compress=zlib-stream` without one yields binary frames
 *    nothing here can read. Equicord's CSP wildcard already covers connect-src
 *    (which is the directive that governs WebSockets) for gateway.discord.gg.
 *  - No user-agent header can be set from a renderer — the browser sends its
 *    own, which inside the Discord desktop client is the real client's. That is
 *    strictly more consistent than the Python faking one, and the same reasoning
 *    applies to `superProperties()` below: this asks Discord's own module for
 *    the descriptor the client is already sending, and only falls back to a
 *    static block when that module cannot be found.
 *  - The gateway allows roughly 120 outbound events per 60 seconds and closes
 *    the socket with 4008 when that is exceeded, so **every** OP 8 and OP 14
 *    goes through a meter. That budget is counted per connection, which is why
 *    this limiter is per instance rather than one of api/ratelimit.ts's shared
 *    per-host limiters — but it is not optional, and nothing here sends without
 *    spending from it first.
 *  - The widget-name resolution pass from the Python is not ported: it needs the
 *    guild's public widget over HTTP, and this plugin already has a better
 *    complete-enumeration primitive for the same problem in
 *    `members.roleMemberIds`, which returns every holder of a role uncapped.
 *
 * The token this connects with never appears in a log line, an error message or
 * a thrown Error. It is held in a WeakMap keyed by the instance rather than as a
 * field, so an accidental `console.log(gateway)` — or any error report that
 * serialises the object — cannot print it.
 */

import { find } from "@webpack";
import {
    ChannelStore,
    GuildChannelStore,
    GuildMemberCountStore,
    GuildRoleStore,
    GuildStore,
    PermissionsBits,
    PermissionStore,
} from "@webpack/common";

import { abortError, isAbortError, RateLimiter, sleep } from "../api/ratelimit";

// --------------------------------------------------------------------------
// constants, carried over from rsb/discord/gateway.py
// --------------------------------------------------------------------------

export const GATEWAY_URL = "wss://gateway.discord.gg/?v=9&encoding=json";

/**
 * Gateway intents. A bot must declare up front what it wants to receive, and
 * GUILD_MEMBERS is *privileged*: it has to be switched on for the application in
 * the Developer Portal, or Discord closes the socket with 4014 rather than
 * quietly sending less. These two are all a member scan needs — asking for
 * message content or presences would be requesting far more than the job.
 */
export const INTENT_GUILDS = 1 << 0;
export const INTENT_GUILD_MEMBERS = 1 << 1;
export const BOT_INTENTS = INTENT_GUILDS | INTENT_GUILD_MEMBERS;

/** members per sidebar range */
export const RANGE_SIZE = 100;

/**
 * Extra ranges requested alongside [0,99] in one subscription. Discord accepts
 * at most three ranges per channel — [0,99] plus two more. Sending a fourth is
 * rejected outright and closes the socket with 4002.
 */
export const RANGES_PER_REQUEST = 2;

/**
 * Channels subscribed to at once; each carries its own slice of the list. This,
 * not more ranges, is how a large list is read quickly.
 */
export const CHANNELS_PER_REQUEST = 5;

/** Discord's per-query result cap on OP 8. A query returning exactly this is truncated. */
export const QUERY_LIMIT = 100;

/** pause between OP 8 prefix queries, in ms, to stay clear of the gateway event budget */
export const PREFIX_QUERY_DELAY = 500;

/** fraction of the guild's member count treated as full coverage */
export const COVERAGE_TARGET = 0.995;

/** characters prefixes are extended with when a query saturates */
const PREFIX_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789_.";

/** how many characters deep the prefix search may go */
const PREFIX_MAX_DEPTH = 2;

/** how long to keep draining events after an OP 14 before deciding it is done */
const SETTLE_TIMEOUT = 2_500;

/** how long to wait for the very first response on a candidate channel */
const FIRST_RESPONSE_TIMEOUT = 6_000;

/** absolute cap on one request/drain round, however chatty the guild is */
const MAX_ROUND_MS = 20_000;

/**
 * Absolute ceiling on one *streamed* OP 8 answer, however many chunks it takes.
 * See the note in `runQuery`: an open query on a very large guild legitimately
 * runs for minutes, and only arriving chunks extend the round towards this.
 */
const MAX_STREAM_MS = 5 * 60_000;

/** how long to wait for chunks after an OP 8 query (they arrive promptly) */
const CHUNK_TIMEOUT = 1_500;

/** text-ish channel types whose member sidebar can be subscribed to */
const TEXT_CHANNEL_TYPES = [0, 5];

/** how many restricted channels are tried one at a time when the open ones fail */
const MAX_RESTRICTED_ATTEMPTS = 6;

/** reconnect backoff, in ms */
const RECONNECT_BASE = 1_000;
const RECONNECT_MAX = 60_000;

/** give up only after this many consecutive failures */
const MAX_RECONNECTS = 12;

/** how long a send will wait for a reconnect before giving up */
const RECONNECT_WAIT = 90_000;

/** how long the socket has to finish its handshake */
const OPEN_TIMEOUT = 30_000;

/** close codes that mean retrying is pointless */
const FATAL_CLOSE_CODES: Record<number, string> = {
    4004: "authentication failed - the token was rejected",
    4010: "invalid shard",
    4011: "sharding required",
    4012: "unsupported API version",
    4013: "invalid intents",
    4014: "disallowed intents",
};

/**
 * 4014 has one overwhelmingly likely cause for this program, and a generic
 * "disallowed intents" leaves the user with nothing to do about it.
 */
const INTENT_HELP =
    "The bot is not approved for the Server Members Intent. Open the Discord "
    + "Developer Portal -> your application -> Bot -> Privileged Gateway Intents, "
    + "switch on SERVER MEMBERS INTENT, and save. Without it Discord will not hand "
    + "over a member list to a bot at all.";

const TOKEN_HELP_BOT =
    "Check the bot token is the current one from the Developer Portal — regenerating "
    + "it invalidates the old one immediately.";

const TOKEN_HELP_CLIENT =
    "Discord refused the account token this client is using. Logging out and back in "
    + "issues a new one.";

export class GatewayError extends Error {
    constructor(message: string) {
        super(message);
        this.name = "GatewayError";
    }
}

// --------------------------------------------------------------------------
// public shapes
// --------------------------------------------------------------------------

export interface ScrapedMember {
    id: string;
    username: string;
    globalName?: string;
    nick?: string;
    bot: boolean;
}

export interface FetchProgress {
    found: number;
    /** the guild's real member count, or 0 when the client does not know it */
    total: number;
    stage: string;
}

export interface FetchOptions {
    /** fraction of `total` that counts as done; defaults to COVERAGE_TARGET */
    coverageTarget?: number;
    /** whether the OP 8 prefix sweep may run after the sidebar; defaults to true */
    prefixSweep?: boolean;
    onProgress?(progress: FetchProgress): void;
    signal?: AbortSignal;
}

// --------------------------------------------------------------------------
// the token, kept off the object
// --------------------------------------------------------------------------

/**
 * Tokens live here rather than on the instance.
 *
 * A field called `token` is one `console.log(gateway)`, one error report and one
 * devtools breakpoint away from being read aloud. A WeakMap is not secrecy — any
 * code in the renderer can reach the same accounts this does — but it does mean
 * the token is not part of the object's shape, so nothing serialises it by
 * accident.
 */
const TOKENS = new WeakMap<object, string>();

// --------------------------------------------------------------------------
// small primitives asyncio gives Python and a renderer does not
// --------------------------------------------------------------------------

/** One dispatch handed to a consumer: `null` is a wake-up, not an event. */
interface Dispatch {
    event: string;
    data: any;
}

/**
 * A manual-reset event, the shape of `asyncio.Event`.
 *
 * `wait` resolves true when the gate is set and false when it times out, and
 * rejects if the caller's signal fires — so a stopped scan does not sit on a
 * ninety-second reconnect window.
 */
class Gate {
    private open = false;
    private waiters: Array<(value: boolean) => void> = [];

    get isSet(): boolean {
        return this.open;
    }

    set(): void {
        this.open = true;
        const waiting = this.waiters;
        this.waiters = [];
        for (const resolve of waiting) resolve(true);
    }

    clear(): void {
        this.open = false;
    }

    wait(timeoutMs: number, signal?: AbortSignal): Promise<boolean> {
        if (this.open) return Promise.resolve(true);
        return new Promise<boolean>((resolve, reject) => {
            let done = false;
            const finish = (value: boolean) => {
                if (done) return;
                done = true;
                cleanup();
                resolve(value);
            };
            const waiter = () => finish(true);
            const timer = setTimeout(() => {
                const index = this.waiters.indexOf(waiter);
                if (index >= 0) this.waiters.splice(index, 1);
                finish(false);
            }, Math.max(0, timeoutMs));
            const onAbort = () => {
                if (done) return;
                done = true;
                const index = this.waiters.indexOf(waiter);
                if (index >= 0) this.waiters.splice(index, 1);
                cleanup();
                reject(abortError());
            };
            const cleanup = () => {
                clearTimeout(timer);
                try {
                    signal?.removeEventListener?.("abort", onAbort);
                } catch {
                    // not a real EventTarget; nothing was hooked
                }
            };
            this.waiters.push(waiter);
            if (signal?.aborted) {
                onAbort();
                return;
            }
            try {
                signal?.addEventListener?.("abort", onAbort);
            } catch {
                // as above — the wait simply cannot be cancelled early
            }
        });
    }
}

/**
 * The queue a consumer drains dispatches from, standing in for `asyncio.Queue`.
 *
 * Filtering happens here rather than at the consumer, exactly as the Python
 * argues: a live account receives a constant stream of unrelated dispatches, and
 * anything that reaches the queue is capable of disturbing the consumer's
 * timing — a MESSAGE_CREATE resetting a settle deadline would end a scrape round
 * early for no reason.
 */
class DispatchQueue {
    readonly wanted: Set<string> | null;
    private items: Array<Dispatch | null> = [];
    private waiter: ((item: Dispatch | null | undefined) => void) | null = null;

    constructor(events?: string[]) {
        this.wanted = events && events.length ? new Set(events) : null;
    }

    push(item: Dispatch | null): void {
        if (this.waiter) {
            const resolve = this.waiter;
            this.waiter = null;
            resolve(item);
            return;
        }
        this.items.push(item);
    }

    /** Next item, `undefined` on timeout, `null` when woken by a failure or a close. */
    next(timeoutMs: number, signal?: AbortSignal): Promise<Dispatch | null | undefined> {
        if (signal?.aborted) return Promise.reject(abortError());
        if (this.items.length) return Promise.resolve(this.items.shift());

        return new Promise<Dispatch | null | undefined>((resolve, reject) => {
            let done = false;
            const settle = (item: Dispatch | null | undefined) => {
                if (done) return;
                done = true;
                cleanup();
                resolve(item);
            };
            const timer = setTimeout(() => {
                if (this.waiter === settle) this.waiter = null;
                settle(undefined);
            }, Math.max(0, timeoutMs));
            const onAbort = () => {
                if (done) return;
                done = true;
                if (this.waiter === settle) this.waiter = null;
                cleanup();
                reject(abortError());
            };
            const cleanup = () => {
                clearTimeout(timer);
                try {
                    signal?.removeEventListener?.("abort", onAbort);
                } catch {
                    // nothing was hooked
                }
            };
            this.waiter = settle;
            try {
                signal?.addEventListener?.("abort", onAbort);
            } catch {
                // uncancellable, but still correct
            }
        });
    }
}

// --------------------------------------------------------------------------
// the client
// --------------------------------------------------------------------------

export class DiscordGateway {
    readonly isBot: boolean;

    /** the READY payload's `user`, once the session exists */
    user: any = null;

    /** coverage of the last fetchMembers as a fraction, or null when unknowable */
    lastCoverage: number | null = null;

    /** which channels actually produced members last time, for the UI to report */
    lastScrapeChannels: string[] = [];

    /** how many times the socket has been re-established */
    reconnects = 0;

    /** called as (attempt, delayMs, reason) when a reconnect is scheduled */
    onReconnect: ((attempt: number, delayMs: number, reason: string) => void) | null = null;

    /** called as (resumed) once a *re*-established session is usable again */
    onReconnected: ((resumed: boolean) => void) | null = null;

    private ws: WebSocket | null = null;
    private seq: number | null = null;
    private heartbeatMs = 41_250;
    private beatTimer: any = null;
    private invalidTimer: any = null;
    private listeners = new Set<DispatchQueue>();
    private closing = false;
    /** close() has been called at least once; an instance is not reusable */
    private closed = false;
    private fatal: Error | null = null;
    private sessionId: string | null = null;
    private resumeUrl: string | null = null;
    private awaitingAck = false;
    private lastError: string | null = null;
    private sessionUsable = false;

    /** READY (or a fatal failure) has happened; what connect() waits on */
    private readyGate = new Gate();

    /**
     * A socket is open. Distinct from `readyGate` because IDENTIFY has to be
     * sent *before* READY arrives, so gating sends on the handshake would
     * deadlock the handshake itself.
     */
    private socketGate = new Gate();

    /**
     * close() has been called. Anything waiting ninety seconds for a reconnect
     * needs to hear about that immediately: without this gate, stopping a scan
     * left the round that was mid-send sitting on the full reconnect window.
     */
    private closedGate = new Gate();

    private supervisor: Promise<void> | null = null;
    private endSocket: (() => void) | null = null;
    private socketEnded: Promise<void> = Promise.resolve();

    /**
     * The gateway's own budget: ~120 events per 60s, kept well under. Counted
     * per connection, so this is deliberately not one of api/ratelimit.ts's
     * shared per-host limiters — a second socket has a second budget, and
     * pretending otherwise would slow a scan down for nothing.
     */
    private sendLimit = new RateLimiter({ limit: 110, window: 60, reserve: 15 });

    constructor(token: string, isBot = false) {
        const value = typeof token === "string" ? token.trim() : "";
        if (!value) {
            // No token in the message, because there is no token — but this is
            // also the shape every other error in this file takes.
            throw new GatewayError("No token was supplied for the gateway connection.");
        }
        TOKENS.set(this, value);
        this.isBot = !!isBot;
    }

    // -- lifecycle ---------------------------------------------------------

    /** Whether a session is usable right now. */
    get connected(): boolean {
        return this.sessionUsable && this.ws != null && this.ws.readyState === 1;
    }

    /**
     * Connect and stay connected, reconnecting on its own if dropped.
     *
     * Resolves with the READY payload's user object. A dropped gateway is not an
     * error: Discord drops connections routinely, for maintenance, on op 7, or
     * because one heartbeat went unanswered, and treating that as fatal threw
     * away an hour of scanning for something entirely ordinary.
     */
    async connect(timeoutMs = 45_000): Promise<any> {
        if (typeof WebSocket === "undefined") {
            throw new GatewayError("This client has no WebSocket, so the gateway cannot be opened.");
        }
        if (this.closed) {
            // Reconnecting a closed instance would race a supervisor that is
            // still unwinding against a fresh one, and two supervisors mean two
            // sockets on one account. A new scan builds a new gateway.
            throw new GatewayError("This gateway was closed. Open a new one instead of reusing it.");
        }
        this.closing = false;
        if (!this.supervisor) {
            this.supervisor = this.supervise().catch(err => {
                this.fail(asError(err));
            });
        }

        const ready = await this.readyGate.wait(Math.max(1_000, timeoutMs));
        if (this.fatal) throw this.fatal;
        if (this.closing) throw new GatewayError("The gateway was closed before it finished connecting.");
        if (!ready) {
            this.close();
            throw new GatewayError("Timed out waiting for READY from Discord's gateway.");
        }
        return this.user || {};
    }

    /**
     * Close the socket and stop reconnecting. Safe to call twice, and safe to
     * call on an instance that never connected — a stop button that throws is
     * worse than no stop button.
     */
    close(): void {
        this.closing = true;
        this.closed = true;
        this.sessionUsable = false;
        this.socketGate.clear();
        this.closedGate.set();
        this.stopHeartbeat();
        if (this.invalidTimer !== null) {
            clearTimeout(this.invalidTimer);
            this.invalidTimer = null;
        }
        const ws = this.ws;
        this.ws = null;
        if (ws) {
            try {
                ws.onopen = null;
                ws.onmessage = null;
                ws.onerror = null;
                ws.onclose = null;
            } catch {
                // a socket implementation that will not be detached; closing
                // it still stops the traffic
            }
            try {
                ws.close();
            } catch {
                // already closing
            }
        }
        // Wake everything: a consumer blocked on a drain, and connect() itself.
        this.wakeListeners();
        this.readyGate.set();
        if (this.endSocket) {
            const end = this.endSocket;
            this.endSocket = null;
            end();
        }
    }

    // -- plumbing ----------------------------------------------------------

    private async supervise(): Promise<void> {
        let attempt = 0;
        while (!this.closing) {
            try {
                await this.openSocket();
                attempt = 0; // a clean session resets the backoff
                await this.socketEnded;
            } catch (err) {
                this.lastError = describe(err);
            }

            if (this.closing || this.fatal) break;

            this.sessionUsable = false;
            this.socketGate.clear();
            this.stopHeartbeat();
            attempt++;
            if (attempt > MAX_RECONNECTS) {
                this.fail(new GatewayError(
                    `The gateway would not stay connected after ${MAX_RECONNECTS} attempts `
                    + `(${this.lastError || "no reason given"}).`
                ));
                break;
            }

            const delay = Math.min(RECONNECT_BASE * Math.pow(2, attempt - 1), RECONNECT_MAX);
            this.reconnects++;
            if (this.onReconnect) {
                try {
                    this.onReconnect(attempt, delay, this.lastError || "closed");
                } catch {
                    // a status line, not a link problem
                }
            }
            try {
                await sleep(delay);
            } catch {
                // sleep only rejects on an abort signal, and none is passed here
            }
        }
        this.wakeListeners();
    }

    private openSocket(): Promise<void> {
        return new Promise<void>((resolve, reject) => {
            let url = this.canResume() && this.resumeUrl ? this.resumeUrl : GATEWAY_URL;
            if (url.indexOf("?") === -1) {
                url = `${url.replace(/\/+$/, "")}/?v=9&encoding=json`;
            }

            this.socketEnded = new Promise<void>(done => {
                this.endSocket = done;
            });

            let ws: WebSocket;
            try {
                ws = new WebSocket(url);
            } catch (err) {
                this.finishSocket();
                reject(new GatewayError(`Could not open a socket to Discord's gateway: ${describe(err)}`));
                return;
            }
            this.ws = ws;
            this.awaitingAck = false;

            let settled = false;
            const openTimer = setTimeout(() => {
                if (settled) return;
                settled = true;
                try {
                    ws.close();
                } catch {
                    // nothing to close
                }
                this.finishSocket();
                reject(new GatewayError("Timed out opening the gateway socket."));
            }, OPEN_TIMEOUT);

            ws.onopen = () => {
                if (settled) return;
                settled = true;
                clearTimeout(openTimer);
                this.socketGate.set();
                resolve();
            };
            ws.onmessage = (event: any) => {
                // A socket the supervisor has already replaced must not steer
                // the live one: the open-timeout path closes a socket that can
                // still deliver frames afterwards.
                if (this.ws !== ws) return;
                try {
                    this.onMessage(event);
                } catch (err) {
                    // One malformed frame must not take the socket down; the
                    // next one is very likely fine.
                    this.lastError = describe(err);
                }
            };
            ws.onerror = () => {
                // The browser deliberately gives no detail here, so there is
                // nothing to record beyond the fact of it; onclose follows.
                this.lastError = this.lastError || "the websocket reported an error";
            };
            ws.onclose = (event: any) => {
                clearTimeout(openTimer);
                this.onClose(event, ws);
                if (!settled) {
                    settled = true;
                    reject(new GatewayError(this.lastError || "The gateway closed the connection."));
                }
            };
        });
    }

    private onClose(event: any, ws: WebSocket): void {
        const stale = this.ws !== ws;
        const code = Number(event?.code) || 0;
        this.lastError = `connection closed (${code || "no close code"})`;

        const known = FATAL_CLOSE_CODES[code];
        if (known) {
            let detail = known;
            if (code === 4013 || code === 4014) {
                // 4014 for a bot means one specific thing, and saying
                // "disallowed intents" leaves the user with nothing to do.
                detail = this.isBot
                    ? `${detail}. ${INTENT_HELP}`
                    : `${detail}. Only a bot connection declares intents, so this is unexpected `
                    + "for an account token — the connection asked for something Discord did not allow.";
            } else if (code === 4004) {
                detail = `${detail}. ${this.isBot ? TOKEN_HELP_BOT : TOKEN_HELP_CLIENT}`;
            }
            this.fail(new GatewayError(`Discord refused the gateway connection (${code}): ${detail}`));
        }

        // A socket the supervisor has already given up on (the open timeout)
        // must not clear the state of the one that replaced it.
        if (stale) return;

        this.sessionUsable = false;
        this.socketGate.clear();
        this.stopHeartbeat();
        this.ws = null;
        this.wakeListeners();
        this.finishSocket();
    }

    /**
     * Wait for a socket to come back, or for close() — whichever happens first.
     *
     * Returns false when there is no point waiting any longer, which a caller
     * treats as "give up on this round".
     */
    private async waitForSocket(timeoutMs: number, signal?: AbortSignal): Promise<boolean> {
        if (this.closing) return false;
        const back = await Promise.race([
            this.socketGate.wait(timeoutMs, signal),
            this.closedGate.wait(timeoutMs, signal).then(() => false),
        ]);
        return !this.closing && back;
    }

    private finishSocket(): void {
        const end = this.endSocket;
        this.endSocket = null;
        if (end) end();
    }

    private onMessage(event: any): void {
        let msg: any;
        try {
            msg = JSON.parse(typeof event?.data === "string" ? event.data : String(event?.data ?? ""));
        } catch {
            // Binary frames only arrive with a compression transport, which this
            // deliberately does not ask for. Nothing to do with one.
            return;
        }
        if (!msg || typeof msg !== "object") return;

        if (msg.s !== null && msg.s !== undefined) this.seq = msg.s;
        const op = msg.op;

        if (op === 10) { // HELLO
            const interval = Number(msg?.d?.heartbeat_interval);
            if (isFinite(interval) && interval > 0) this.heartbeatMs = interval;
            this.awaitingAck = false;
            this.startHeartbeat();
            if (this.canResume()) this.resume();
            else this.identify();
            return;
        }
        if (op === 0) { // DISPATCH
            this.dispatch(typeof msg.t === "string" ? msg.t : "", msg.d || {});
            return;
        }
        if (op === 1) { // the server asked for a heartbeat
            this.rawSend({ op: 1, d: this.seq });
            return;
        }
        if (op === 11) { // heartbeat ACK
            this.awaitingAck = false;
            return;
        }
        if (op === 7) { // server asked us to reconnect — routine
            this.lastError = "the server requested a reconnect";
            this.dropSocket();
            return;
        }
        if (op === 9) { // invalid session
            const resumable = !!msg.d;
            this.lastError = "the session was invalidated";
            if (!resumable) {
                this.sessionId = null;
                this.resumeUrl = null;
                this.seq = null;
            }
            // Discord asks for a randomised pause here so a fleet of clients
            // does not re-identify in lockstep.
            if (this.invalidTimer !== null) clearTimeout(this.invalidTimer);
            this.invalidTimer = setTimeout(() => {
                this.invalidTimer = null;
                this.dropSocket();
            }, 1_000 + Math.random() * 4_000);
        }
    }

    private dispatch(event: string, data: any): void {
        if (event === "READY") {
            this.user = data?.user ?? null;
            this.sessionId = typeof data?.session_id === "string" ? data.session_id : null;
            const resumeUrl = data?.resume_gateway_url;
            if (typeof resumeUrl === "string" && resumeUrl) this.resumeUrl = resumeUrl;
            this.announceConnected(false);
            this.sessionUsable = true;
            this.readyGate.set();
        } else if (event === "RESUMED") {
            this.announceConnected(true);
            this.sessionUsable = true;
            this.readyGate.set();
        }

        for (const queue of Array.from(this.listeners)) {
            if (!queue.wanted || queue.wanted.has(event)) queue.push({ event, data });
        }
    }

    /**
     * Tell the caller the session is usable again, once per reconnect.
     *
     * Only fires when this is a *re*-connection: the first READY is not news
     * anyone needs telling about, and announcing the drop without ever
     * announcing the recovery is what left callers displaying "reconnecting…"
     * over a connection that had been working for the last forty minutes.
     */
    private announceConnected(resumed: boolean): void {
        if (this.sessionUsable || !this.reconnects) return;
        if (!this.onReconnected) return;
        try {
            this.onReconnected(resumed);
        } catch {
            // a display problem, not a link problem
        }
    }

    private canResume(): boolean {
        return !!(this.sessionId && this.resumeUrl && this.seq !== null);
    }

    private identify(): void {
        const token = TOKENS.get(this);
        if (!token) return;

        if (this.isBot) {
            // A bot identifies as itself and declares its intents. It must not
            // imitate the web client: the client descriptor a user token needs
            // is exactly what an application should not be sending.
            this.rawSend({
                op: 2,
                d: {
                    token,
                    intents: BOT_INTENTS,
                    properties: {
                        os: "linux",
                        browser: "RotectorScan",
                        device: "RotectorScan",
                    },
                    compress: false,
                    large_threshold: 250,
                },
            });
            return;
        }

        this.rawSend({
            op: 2,
            d: {
                token,
                capabilities: 30717,
                properties: {
                    // The same descriptor the client is already sending on its
                    // own connection; describing ourselves two different ways
                    // from inside one client is exactly what looks automated.
                    ...superProperties(),
                    referrer: "",
                    referring_domain: "",
                    referrer_current: "",
                    referring_domain_current: "",
                },
                presence: {
                    // "unknown" is what the client sends for a connection that
                    // is not meant to change the account's visible status.
                    status: "unknown",
                    since: 0,
                    activities: [],
                    afk: false,
                },
                compress: false,
                client_state: {
                    guild_versions: {},
                    highest_last_message_id: "0",
                    read_state_version: 0,
                    user_guild_settings_version: -1,
                    private_channels_version: "0",
                    api_code_version: 0,
                },
            },
        });
    }

    private resume(): void {
        const token = TOKENS.get(this);
        if (!token) return;
        this.rawSend({
            op: 6,
            d: {
                token,
                session_id: this.sessionId,
                seq: this.seq,
            },
        });
    }

    private startHeartbeat(): void {
        this.stopHeartbeat();
        // The first beat is jittered across the interval, as Discord asks.
        this.beatTimer = setTimeout(() => this.beat(), Math.floor(this.heartbeatMs * Math.random()));
    }

    private stopHeartbeat(): void {
        if (this.beatTimer !== null) {
            clearTimeout(this.beatTimer);
            this.beatTimer = null;
        }
    }

    /**
     * Beat, and treat a missing ACK as a dead connection.
     *
     * A socket that stops answering heartbeats is not detectably closed — it
     * simply stops carrying traffic, which from this end looks exactly like a
     * quiet guild. Without this the connection appeared alive and nothing
     * arrived.
     */
    private beat(): void {
        this.beatTimer = null;
        if (this.closing || !this.ws) return;
        if (this.awaitingAck) {
            this.lastError = "a heartbeat went unacknowledged";
            this.dropSocket();
            return;
        }
        this.awaitingAck = true;
        this.rawSend({ op: 1, d: this.seq });
        this.beatTimer = setTimeout(() => this.beat(), this.heartbeatMs);
    }

    private dropSocket(): void {
        const ws = this.ws;
        if (!ws) return;
        try {
            ws.close();
        } catch {
            // already gone; onclose still fires, or the supervisor times out
        }
    }

    /**
     * Send an unmetered control frame — IDENTIFY, RESUME, heartbeat.
     *
     * These are exempt in the Python too: a heartbeat that queued behind the
     * event budget would be a heartbeat that arrives late, and a late heartbeat
     * is a closed socket.
     */
    private rawSend(payload: any): void {
        const ws = this.ws;
        if (!ws || ws.readyState !== 1) return;
        try {
            ws.send(JSON.stringify(payload));
        } catch {
            // The close handler deals with it; there is nothing useful to say
            // here that would not risk quoting the payload.
        }
    }

    /**
     * Send a metered command — OP 8 and OP 14 — waiting through a reconnect
     * rather than failing.
     *
     * Waiting only happens when there is genuinely no socket. The close handler
     * clears `ws`, so "null" means a reconnect is in progress rather than merely
     * that the handshake has not finished.
     */
    private async send(payload: any, signal?: AbortSignal): Promise<void> {
        if (!this.ws && !this.closing) {
            const back = await this.waitForSocket(RECONNECT_WAIT, signal);
            if (!back) throw new GatewayError("The gateway did not come back in time.");
        }
        if (this.closing) throw new GatewayError("The gateway is closed.");

        // Metered before the readyState check, because the wait itself can be
        // long enough for a socket to come and go.
        await this.sendLimit.acquire(1, signal);

        const ws = this.ws;
        if (!ws || ws.readyState !== 1) throw new GatewayError("The gateway is not connected.");
        try {
            ws.send(JSON.stringify(payload));
        } catch {
            // The supervisor will bring it back; the caller retries the round.
            // The thrown message deliberately carries nothing from the payload.
            throw new GatewayError("The send failed and the gateway is reconnecting.");
        }
    }

    /** Record an unrecoverable failure and wake everyone waiting. */
    private fail(err: Error): void {
        if (!this.closing && !this.fatal) this.fatal = err;
        this.readyGate.set();
        this.wakeListeners();
    }

    private wakeListeners(): void {
        for (const queue of Array.from(this.listeners)) queue.push(null);
    }

    /**
     * Raise only for unrecoverable failures.
     *
     * A dropped socket is not one: the supervisor is already bringing it back,
     * and callers wait on the send rather than giving up.
     */
    private checkAlive(): void {
        if (this.fatal) throw this.fatal;
    }

    private subscribe(events: string[]): DispatchQueue {
        const queue = new DispatchQueue(events);
        this.listeners.add(queue);
        return queue;
    }

    private unsubscribe(queue: DispatchQueue): void {
        this.listeners.delete(queue);
    }

    // -- member enumeration ------------------------------------------------

    /**
     * Enumerate the members of `guildId`, as completely as possible.
     *
     * Completeness is the priority, because a member the sidebar never shows is
     * a member never checked. Three things serve that, in this order:
     *
     *  - If this connection can chunk — a bot with the Server Members Intent, or
     *    an account holding kick, ban, manage-roles or administrator — Discord
     *    hands over the whole member list, offline members included, for a
     *    single OP 8 with an empty query. That is tried first, and when it works
     *    nothing else is needed.
     *  - Channels **@everyone can view** are tried first otherwise. A sidebar
     *    only lists members who can see its channel, so a restricted channel
     *    silently yields a partial list — exactly the failure that is invisible
     *    unless you look for it.
     *  - Results from several channels are **unioned**, not replaced, so members
     *    visible only through one channel are still picked up, and if the union
     *    still falls short the OP 8 prefix sweep fills in the rest.
     */
    async fetchMembers(guildId: string, opts: FetchOptions = {}): Promise<ScrapedMember[]> {
        this.checkAlive();
        const gid = String(guildId || "");
        if (!/^\d+$/.test(gid)) throw new GatewayError("fetchMembers needs a guild id.");

        const signal = opts.signal;
        const coverageTarget = clamp(
            typeof opts.coverageTarget === "number" ? opts.coverageTarget : COVERAGE_TARGET,
            0.01,
            1
        );
        const sweepAllowed = opts.prefixSweep !== false;

        const members = new Map<string, ScrapedMember>();
        let total = memberCountOf(gid);
        this.lastScrapeChannels = [];
        this.lastCoverage = null;

        const report = (stage: string) => {
            if (!opts.onProgress) return;
            try {
                opts.onProgress({ found: members.size, total, stage });
            } catch {
                // a progress callback that throws is the caller's problem, not
                // a reason to abandon a half-finished scrape
            }
        };
        const covered = () => total > 0 && members.size >= total * coverageTarget;
        const finish = () => {
            this.lastCoverage = total > 0 ? Math.min(1, members.size / total) : null;
            return toList(members);
        };

        throwIfAborted(signal);

        // A bot never has to negotiate for this: with the GUILD_MEMBERS intent
        // the whole list is simply available, offline members and all.
        const canChunk = this.isBot || canChunkGuild(gid);

        if (canChunk) {
            report(
                "Requesting the full member list"
                + (this.isBot ? " (bot, Server Members Intent)" : " (permitted for this account)")
            );
            await this.requestMembers(gid, {
                signal,
                members,
                report,
                covered,
                openQueryOnly: true,
            });
            if (members.size) {
                this.lastScrapeChannels = ["full member list"];
                report(`Received the full member list (${fmt(members.size)})`);
            }
            if (covered()) return finish();
        }

        if (this.isBot) {
            // The sidebar paths below are OP 14 guild subscriptions, which is a
            // client feature — a bot has no member sidebar to subscribe to.
            // There is nothing further to try, so say so rather than silently
            // returning a short list as though it were complete.
            this.lastScrapeChannels = ["full member list"];
            if (members.size) {
                report(`Received ${fmt(members.size)} member(s) from the bot member list`);
            } else {
                report(
                    "Discord returned no members. The Server Members Intent is most likely "
                    + "not enabled for this bot."
                );
            }
            return finish();
        }

        const channels = candidateChannels(gid);
        const open = channels.filter(c => c.everyoneCanView);
        const restricted = channels.filter(c => !c.everyoneCanView);

        if (!open.length) {
            report("No channel is visible to @everyone — the member list may be partial");
        }

        // Everyone-visible channels are tried together first; restricted ones
        // only as a last resort, one at a time, since each can only ever
        // contribute the subset of members who can see it.
        const attempts: Candidate[][] = [];
        if (open.length) attempts.push(open.slice(0, CHANNELS_PER_REQUEST));
        for (const channel of restricted.slice(0, MAX_RESTRICTED_ATTEMPTS)) attempts.push([channel]);

        for (let index = 0; index < attempts.length; index++) {
            throwIfAborted(signal);
            const group = attempts[index];
            if (!group.length) continue;
            const label = groupLabel(group);
            report(
                `Opening member list via ${label} (${group[0].everyoneCanView ? "everyone" : "restricted"}, `
                + `attempt ${index + 1}/${attempts.length})`
            );
            const before = members.size;
            await this.scrapeSidebar(gid, group, {
                signal,
                members,
                report,
                setTotal: n => { total = n; },
            });
            if (members.size > before) {
                for (const channel of group) this.lastScrapeChannels.push(channel.name);
            } else {
                report(`${label} added nothing, trying elsewhere`);
            }
            if (covered()) break;
        }

        if (!covered() && sweepAllowed) {
            const want = total ? ` of ${fmt(total)}` : "";
            report(`The sidebar gave ${fmt(members.size)}${want} — searching for the rest (OP 8)`);
            await this.requestMembers(gid, {
                signal,
                members,
                report,
                covered,
                openQueryOnly: false,
            });
        } else if (!covered()) {
            report(
                `The sidebar gave ${fmt(members.size)}. The name search is switched off, so the `
                + "rest were not looked for."
            );
        }

        return finish();
    }

    /**
     * Read the member sidebar, several channels at a time.
     *
     * Discord allows at most three ranges per channel in one OP 14 — the
     * mandatory [0,99] plus two more, and a fourth closes the socket with 4002.
     * Speed therefore comes from subscribing to several channels in the *same*
     * request, each covering a different window, which is what the real client
     * does. Every everyone-visible channel exposes the same list, so their
     * results simply merge.
     */
    private async scrapeSidebar(
        guildId: string,
        channels: Candidate[],
        ctx: {
            signal?: AbortSignal;
            members: Map<string, ScrapedMember>;
            report(stage: string): void;
            setTotal(n: number): void;
        }
    ): Promise<void> {
        const { members, report, signal } = ctx;
        const queue = this.subscribe(["GUILD_MEMBER_LIST_UPDATE"]);
        const targets = channels.slice(0, CHANNELS_PER_REQUEST);
        const names = groupLabel(targets);

        const windows: Array<[number, number]> = [];
        // The pulled windows start *past* the head. [0,99] is sent with every
        // request as the mandatory first range and the probe round already read
        // it, so a refill from 0 would spend one of the two spare ranges asking
        // for the same hundred members again — the first channel's request came
        // out as [[0,99], [0,99], [100,199]]. This is what the Python's
        // `_sidebar_ranges` encodes when it returns only the head for offset 0
        // and windows from `offset` upwards for anything else.
        let nextStart = RANGE_SIZE;
        let listSize = 0;
        let barren = 0;
        let first = true;

        const refill = () => {
            if (!listSize) return;
            while (nextStart < listSize) {
                windows.push([nextStart, nextStart + RANGE_SIZE - 1]);
                nextStart += RANGE_SIZE;
            }
        };

        try {
            while (!this.closing) {
                throwIfAborted(signal);
                this.checkAlive();

                const requests: Record<string, number[][]> = {};
                const head = [0, RANGE_SIZE - 1];
                if (first) {
                    // one probe, to learn how long the list actually is
                    requests[targets[0].id] = [head];
                } else {
                    for (const channel of targets) {
                        const picked: number[][] = [];
                        for (let i = 0; i < RANGES_PER_REQUEST; i++) {
                            const window = windows.shift();
                            if (!window) break;
                            picked.push([window[0], window[1]]);
                        }
                        if (picked.length) requests[channel.id] = [head, ...picked];
                    }
                    if (!Object.keys(requests).length) break;
                }

                try {
                    await this.send({
                        op: 14,
                        d: {
                            guild_id: guildId,
                            channels: requests,
                            members: [],
                            activities: true,
                            typing: true,
                            threads: false,
                        },
                    }, signal);
                } catch (err) {
                    if (isAbortError(err)) throw err;
                    // The socket went away mid-round; put the windows back and
                    // wait for the supervisor rather than losing the scrape.
                    this.checkAlive();
                    const returned: Array<[number, number]> = [];
                    for (const ranges of Object.values(requests)) {
                        for (const range of ranges.slice(1)) returned.push([range[0], range[1]]);
                    }
                    windows.unshift(...returned);
                    report("Reconnecting to the gateway, the scan will continue");
                    const back = await this.waitForSocket(RECONNECT_WAIT, signal);
                    if (!back) break;
                    continue;
                }

                const before = members.size;
                let gotEvent = false;
                const timeout = first ? FIRST_RESPONSE_TIMEOUT : SETTLE_TIMEOUT;

                if (first) {
                    report(`Waiting for the ${names} member list`);
                } else {
                    const spans: number[][] = [];
                    for (const ranges of Object.values(requests)) {
                        for (const range of ranges.slice(1)) spans.push(range);
                    }
                    const lo = spans.length ? Math.min(...spans.map(r => r[0])) : 0;
                    const hi = spans.length ? Math.max(...spans.map(r => r[1])) : 0;
                    const of = listSize ? ` of ${fmt(listSize)}` : "";
                    report(
                        `Reading ${names} members ${fmt(lo)}-${fmt(hi)}${of} `
                        + `(${Object.keys(requests).length} channel(s))`
                    );
                }

                let deadline = Date.now() + timeout;
                const hardDeadline = Date.now() + MAX_ROUND_MS;
                for (;;) {
                    const remaining = Math.min(deadline, hardDeadline) - Date.now();
                    if (remaining <= 0) break;
                    const item = await queue.next(remaining, signal);
                    if (item === undefined) break; // the round went quiet
                    if (item === null) {
                        // A wake-up: fatal if something really broke, otherwise
                        // the socket is being re-established and the round is
                        // simply retried.
                        this.checkAlive();
                        break;
                    }
                    if (item.event !== "GUILD_MEMBER_LIST_UPDATE") continue;
                    if (String(item.data?.guild_id) !== String(guildId)) continue;

                    gotEvent = true;
                    deadline = Date.now() + SETTLE_TIMEOUT;

                    const count = item.data?.member_count;
                    if (typeof count === "number" && count > 0) ctx.setTotal(count);

                    const groups = item.data?.groups;
                    if (Array.isArray(groups) && groups.length) {
                        let rows = 0;
                        for (const group of groups) {
                            if (group && typeof group === "object") rows += Number(group.count) || 0;
                        }
                        // Each role divider occupies a row of the list too, so
                        // the addressable length is members plus groups.
                        if (rows) listSize = rows + groups.length;
                    }

                    absorbOps(item.data?.ops, members);
                }

                if (first && !gotEvent) return; // this channel exposes no member list
                if (first) {
                    first = false;
                    refill();
                    if (!windows.length) break;
                    continue;
                }

                refill();
                if (members.size > before) barren = 0;
                else barren++;

                report(`Reading the ${names} member list`);
                // Two consecutive rounds that add nobody means the list is
                // exhausted, or the guild has stopped answering; either way
                // there is nothing further to read here.
                if (barren >= 2) break;
                await sleep(200, signal);
            }
        } finally {
            this.unsubscribe(queue);
        }
    }

    /**
     * OP 8 sweep: an open query, then an adaptively deepened prefix search.
     *
     * A flat pass over single-character prefixes cannot fill a large guild:
     * Discord returns at most QUERY_LIMIT matches per query, so 38 prefixes can
     * surface 3,800 members at the very most however many the guild actually
     * has.
     *
     * So a prefix that comes back *saturated* — exactly the limit — is
     * understood to be hiding more, and is re-queried one character deeper
     * ("a" -> "aa", "ab", …). Prefixes that are not saturated are left alone, so
     * the extra queries are spent only where members are actually being missed.
     */
    private async requestMembers(
        guildId: string,
        ctx: {
            signal?: AbortSignal;
            members: Map<string, ScrapedMember>;
            report(stage: string): void;
            covered(): boolean;
            openQueryOnly: boolean;
        }
    ): Promise<void> {
        const { members, report, covered, signal } = ctx;
        const queue = this.subscribe(["GUILD_MEMBERS_CHUNK"]);
        let queries = 0;

        /**
         * Send one query; return how many members it *returned*.
         *
         * Deliberately the returned count rather than the newly-added count:
         * saturation is a property of the query, and a prefix can return a full
         * hundred that we have all seen before while still hiding more.
         */
        const runQuery = async (query: string, limit: number): Promise<number> => {
            await this.send({
                op: 8,
                d: {
                    guild_id: [guildId],
                    query,
                    limit,
                    presences: false,
                },
            }, signal);
            queries++;

            let returned = 0;
            const started = Date.now();
            let deadline = started + CHUNK_TIMEOUT;
            /**
             * The hard deadline is the one place this diverges from the Python,
             * and deliberately. There it is a flat twenty seconds per round;
             * here the open query is the *primary* path — a bot, or a permitted
             * account, receives the whole roster as a stream of chunks, and a
             * hundred-thousand-member guild does not finish in twenty seconds.
             * Cutting that off would return a truncated list under the words
             * "the full member list", which is precisely the failure this module
             * exists to prevent. So arriving chunks buy more time, up to an
             * absolute ceiling: a guild that has genuinely stopped answering
             * still ends the round, because only real progress extends it.
             */
            let hardDeadline = started + MAX_ROUND_MS;
            for (;;) {
                const remaining = Math.min(deadline, hardDeadline) - Date.now();
                if (remaining <= 0) break;
                const item = await queue.next(remaining, signal);
                if (item === undefined) break;
                if (item === null) {
                    this.checkAlive();
                    break;
                }
                if (item.event !== "GUILD_MEMBERS_CHUNK") continue;
                if (String(item.data?.guild_id) !== String(guildId)) continue;

                deadline = Date.now() + CHUNK_TIMEOUT;
                hardDeadline = Math.min(started + MAX_STREAM_MS, Date.now() + MAX_ROUND_MS);
                const raw = item.data?.members;
                if (Array.isArray(raw)) {
                    for (const entry of raw) {
                        returned++;
                        const member = parseMember(entry);
                        if (member) members.set(member.id, member);
                    }
                }
                const index = item.data?.chunk_index;
                const count = item.data?.chunk_count;
                // A large open query streams for minutes, and a progress bar
                // that says nothing for minutes reads as a hung scan.
                if (Number(count) > 1) {
                    report(`Receiving the member list, chunk ${fmt(Number(index) + 1)} of ${fmt(Number(count))}`);
                }
                if (index === null || index === undefined || count === null || count === undefined) break;
                if (Number(index) >= Number(count) - 1) break;
            }
            return returned;
        };

        const sweep = async (prefix: string, depth: number): Promise<void> => {
            if (covered()) return;
            throwIfAborted(signal);
            this.checkAlive();
            report(`Searching members by name "${prefix}" (query ${queries + 1}, ${fmt(members.size)} found)`);
            const returned = await runQuery(prefix, QUERY_LIMIT);
            await sleep(PREFIX_QUERY_DELAY, signal);
            // A full page back means the query was truncated, not exhausted.
            if (returned >= QUERY_LIMIT && depth < PREFIX_MAX_DEPTH) {
                for (const char of PREFIX_ALPHABET) {
                    if (covered()) return;
                    await sweep(prefix + char, depth + 1);
                }
            }
        };

        try {
            // With the right permissions — or with a bot's Server Members
            // Intent — an empty query with `limit: 0` returns everyone at once.
            // That is the whole reason to prefer a bot token for this.
            report("Requesting all members (OP 8 open query)");
            await runQuery("", 0);

            if (!covered() && !ctx.openQueryOnly) {
                for (const char of PREFIX_ALPHABET) {
                    if (covered()) break;
                    await sweep(char, 1);
                }
            }
        } finally {
            this.unsubscribe(queue);
        }

        report(`The name search finished after ${fmt(queries)} query/queries`);
    }
}

// --------------------------------------------------------------------------
// payload parsing
// --------------------------------------------------------------------------

/**
 * Fold GUILD_MEMBER_LIST_UPDATE ops into `members`.
 *
 * Returns true if any op was an INVALIDATE, which means a range beyond the end
 * of the list was asked for. The scraper does not act on that: ranges are
 * requested across several channels at once, so one INVALIDATE is not proof the
 * list ended — the barren-round counter is what ends the loop. Op kinds are the
 * gateway's: SYNC carries `items`, INSERT and UPDATE carry one `item`, DELETE
 * and INVALIDATE carry no member payload at all, and a `{ group: … }` item is a
 * role divider rather than a person.
 */
export function absorbOps(ops: any, members: Map<string, ScrapedMember>): boolean {
    if (!Array.isArray(ops)) return false;
    let invalidated = false;

    for (const op of ops) {
        if (!op || typeof op !== "object") continue;
        const kind = op.op ?? op.type;

        if (kind === "INVALIDATE") {
            invalidated = true;
            continue;
        }

        let items: any[];
        if (kind === "SYNC") {
            items = Array.isArray(op.items) ? op.items : [];
        } else if (kind === "INSERT" || kind === "UPDATE") {
            items = op.item ? [op.item] : [];
        } else {
            continue;
        }

        for (const item of items) {
            if (!item || typeof item !== "object") continue;
            const member = parseMember(item.member);
            if (member) members.set(member.id, member);
        }
    }
    return invalidated;
}

/** One member payload, reduced to the fields a scan needs. */
export function parseMember(raw: any): ScrapedMember | null {
    if (!raw || typeof raw !== "object") return null;
    const user = raw.user || {};
    const id = user.id ?? user.userId ?? user.user_id;
    if (typeof id !== "string" && typeof id !== "number") return null;
    const key = String(id);
    if (!/^\d+$/.test(key)) return null;

    const globalName = user.global_name ?? user.globalName;
    return {
        id: key,
        username: typeof user.username === "string" && user.username ? user.username : "unknown",
        globalName: typeof globalName === "string" && globalName ? globalName : undefined,
        nick: typeof raw.nick === "string" && raw.nick ? raw.nick : undefined,
        bot: !!user.bot,
    };
}

// --------------------------------------------------------------------------
// what the client already knows about the guild
// --------------------------------------------------------------------------

interface Candidate {
    id: string;
    name: string;
    position: number;
    everyoneCanView: boolean;
}

const ZERO = BigInt(0);
const VIEW_CHANNEL = BigInt(1) << BigInt(10);
const ADMINISTRATOR = BigInt(1) << BigInt(3);
const KICK_MEMBERS = BigInt(1) << BigInt(1);
const BAN_MEMBERS = BigInt(1) << BigInt(2);
const MANAGE_ROLES = BigInt(1) << BigInt(28);

/**
 * Whether this connection can ask Discord for the whole roster at once.
 *
 * Holding kick, ban, manage-roles or administrator is the difference between a
 * complete scan and a partial one: with any of them the OP 8 open query returns
 * every member, offline included, and none of the sidebar work below is needed.
 */
export function canChunkGuild(guildId: string): boolean {
    try {
        const guild = GuildStore?.getGuild?.(guildId);
        if (!guild) return false;
        const wanted = [
            permissionBit("ADMINISTRATOR", ADMINISTRATOR),
            permissionBit("KICK_MEMBERS", KICK_MEMBERS),
            permissionBit("BAN_MEMBERS", BAN_MEMBERS),
            permissionBit("MANAGE_ROLES", MANAGE_ROLES),
        ];
        for (const permission of wanted) {
            if (PermissionStore?.can?.(permission, guild)) return true;
        }
        return false;
    } catch {
        // Stores not ready, or a build whose permission API differs. Assuming
        // "no" only costs a few seconds of open query that returns nothing.
        return false;
    }
}

/**
 * One permission bit from Discord's own table, falling back to the literal.
 *
 * `PermissionsBits` is a lazy webpack proxy, so *reading a property off it*
 * is what throws when the module was not found — hence one try/catch per bit
 * rather than one around the group. The literals are the documented values and
 * do not change; they are the fallback rather than the source only because a
 * build that renumbered them would break silently.
 */
function permissionBit(name: string, fallback: bigint): bigint {
    try {
        const value = (PermissionsBits as any)?.[name];
        return typeof value === "bigint" ? value : fallback;
    } catch {
        return fallback;
    }
}

/**
 * Text channels of this guild, ordered so the most *widely visible* come first.
 *
 * This ordering is load-bearing for coverage. The member sidebar only lists
 * members who can see the channel it belongs to, so scraping a staff-only
 * channel silently returns a partial member list.
 */
export function candidateChannels(guildId: string): Candidate[] {
    let entries: any[] = [];
    try {
        const grouped: any = GuildChannelStore?.getChannels?.(guildId);
        const selectable = grouped?.SELECTABLE;
        if (Array.isArray(selectable)) entries = selectable;
        else if (selectable && typeof selectable === "object") entries = Object.values(selectable);
    } catch {
        entries = [];
    }

    const out: Candidate[] = [];
    for (const entry of entries) {
        const channel = entry?.channel ?? entry;
        if (!channel || typeof channel !== "object") continue;
        if (TEXT_CHANNEL_TYPES.indexOf(Number(channel.type)) === -1) continue;
        if (typeof channel.id !== "string") continue;
        // A channel this account cannot see has no sidebar to subscribe to.
        if (!canView(channel)) continue;
        out.push({
            id: channel.id,
            name: typeof channel.name === "string" && channel.name ? channel.name : channel.id,
            position: Number(channel.position) || 0,
            everyoneCanView: everyoneCanView(channel, guildId),
        });
    }

    out.sort((a, b) => {
        if (a.everyoneCanView !== b.everyoneCanView) return a.everyoneCanView ? -1 : 1;
        return a.position - b.position;
    });
    return out;
}

function canView(channel: any): boolean {
    try {
        const can = PermissionStore?.can?.(permissionBit("VIEW_CHANNEL", VIEW_CHANNEL), channel);
        // An undefined answer means the store could not decide; the subscription
        // failing is cheaper than dropping a channel that would have worked.
        return can !== false;
    } catch {
        return true;
    }
}

/**
 * Can the @everyone role view this channel?
 *
 * The @everyone role's base permissions, then the category's overwrite for
 * @everyone, then the channel's own — the same order Discord resolves them in,
 * and the same order rsb/discord/http.py's `_everyone_can_view` uses. When
 * anything about that cannot be read the answer is "yes", because the cost of
 * being wrong is one wasted subscription rather than a silently partial list.
 */
function everyoneCanView(channel: any, guildId: string): boolean {
    try {
        const everyone = GuildRoleStore?.getRole?.(guildId, guildId);
        const base = toBig(everyone?.permissions);
        if ((base & ADMINISTRATOR) !== ZERO) return true;

        let allowed = (base & VIEW_CHANNEL) !== ZERO;

        const parentId = channel.parent_id ?? channel.parentId;
        if (parentId) {
            const parent = ChannelStore?.getChannel?.(String(parentId));
            if (parent) allowed = applyOverwrite(allowed, parent, guildId);
        }
        return applyOverwrite(allowed, channel, guildId);
    } catch {
        return true;
    }
}

function applyOverwrite(allowed: boolean, channel: any, guildId: string): boolean {
    const overwrites = channel?.permissionOverwrites ?? channel?.permissionOverwrites_;
    if (!overwrites || typeof overwrites !== "object") return allowed;
    // The @everyone role's id is the guild id, and type 0 is a role overwrite.
    const overwrite = overwrites[guildId];
    if (!overwrite) return allowed;
    if (overwrite.type !== undefined && Number(overwrite.type) !== 0) return allowed;

    if ((toBig(overwrite.deny) & VIEW_CHANNEL) !== ZERO) return false;
    if ((toBig(overwrite.allow) & VIEW_CHANNEL) !== ZERO) return true;
    return allowed;
}

function toBig(value: any): bigint {
    try {
        if (typeof value === "bigint") return value;
        if (typeof value === "number") return BigInt(Math.trunc(value));
        if (typeof value === "string" && /^\d+$/.test(value)) return BigInt(value);
        return ZERO;
    } catch {
        return ZERO;
    }
}

function memberCountOf(guildId: string): number {
    try {
        const n = GuildMemberCountStore?.getMemberCount?.(guildId);
        return typeof n === "number" && n > 0 ? Math.floor(n) : 0;
    } catch {
        return 0;
    }
}

/**
 * The client descriptor the account's own connection is already sending.
 *
 * Discord's own module is asked first, because two connections from one client
 * describing themselves differently is precisely the pattern that looks
 * automated. The static fallback matches rsb/discord/http.py's
 * `super_properties()` and is only reached when that module cannot be found.
 */
function superProperties(): Record<string, any> {
    try {
        // isIndirect keeps a miss quiet: Vencord logs an error (and throws in a
        // dev build) for an unqualified find that comes back empty, and this one
        // has a perfectly good fallback.
        const mod: any = find(
            (m: any) => m && typeof m.getSuperProperties === "function",
            { isIndirect: true }
        );
        const props = mod?.getSuperProperties?.();
        if (props && typeof props === "object") return { ...props };
    } catch {
        // fall through to the static descriptor
    }
    return {
        os: "Linux",
        browser: "Discord Client",
        release_channel: "stable",
        client_version: "1.0.144",
        os_version: "",
        os_arch: "x64",
        app_arch: "x64",
        system_locale: "en-US",
        has_client_mods: true,
        browser_user_agent:
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            + "discord/1.0.144 Chrome/138.0.7204.251 Electron/37.6.0 Safari/537.36",
        browser_version: "37.6.0",
        client_build_number: 586984,
        native_build_number: null,
        client_event_source: null,
    };
}

// --------------------------------------------------------------------------
// small helpers
// --------------------------------------------------------------------------

function toList(members: Map<string, ScrapedMember>): ScrapedMember[] {
    return Array.from(members.values());
}

function groupLabel(channels: Candidate[]): string {
    const names = channels.slice(0, 2).map(c => `#${c.name}`).join("/");
    return channels.length > 2 ? `${names} +${channels.length - 2}` : names;
}

function clamp(value: number, min: number, max: number): number {
    if (!isFinite(value)) return max;
    return Math.min(max, Math.max(min, value));
}

function fmt(n: number): string {
    try {
        return Number(n).toLocaleString();
    } catch {
        return String(n);
    }
}

function throwIfAborted(signal?: AbortSignal): void {
    if (signal?.aborted) throw abortError();
}

function asError(err: any): Error {
    if (err instanceof Error) return err;
    return new GatewayError(describe(err));
}

/**
 * A short description of a failure, for a status line.
 *
 * Deliberately message-only: a token never reaches an error message in this
 * file, and nothing here stringifies a payload.
 */
function describe(err: any): string {
    if (!err) return "unknown error";
    if (typeof err === "string") return err;
    const name = err.name || "Error";
    const message = err.message || String(err);
    return `${name}: ${message}`;
}
