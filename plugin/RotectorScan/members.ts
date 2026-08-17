/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Everything about turning a Discord surface into a list of member ids, and
 * about saying honestly what that list is.
 *
 * The selfbot drives the gateway itself: it sends OP 8 prefix queries and OP 14
 * sidebar range subscriptions until it holds a guild's whole roster, and it
 * reports the fraction it reached. A client mod has neither of those levers.
 * What it has is:
 *
 *  - `GuildMemberStore`, which is a *cache* of the members Discord has already
 *    sent this client — roughly the sidebar window the user has scrolled plus
 *    their friends. It is not the roster, and in a large guild it is a small
 *    sample of it.
 *  - `GuildMemberCountStore.getMemberCount`, which is the real population. Every
 *    guild-shaped count in this module is denominated against it, because a
 *    percentage with the wrong denominator is how a 5% sample gets reported as
 *    a clean server.
 *  - `Constants.Endpoints.GUILD_ROLE_MEMBER_IDS`, which returns every holder of
 *    a role in one REST call, uncapped. That is the only complete enumeration
 *    primitive available here, which is why "scan a role" is a first-class
 *    source rather than a convenience.
 *
 * ...and, since the gateway module landed, the selfbot's own lever after all: a
 * renderer may open its own WebSocket, so `gateway/` ports the OP 14 sidebar
 * subscription and the OP 8 prefix sweep in full. It is off by default and
 * gated behind `gateway/tokens.ts`, because the account-token half of it is
 * automation of a user account. What matters *here* is that the two paths make
 * completely different claims — a gateway scrape is a real roster, a
 * `GuildMemberStore` read is a sample of one — so `Collected.via` records which
 * one ran and the note never says "coverage" without saying of what.
 *
 * Ported from rsb/sources.py.
 */

import { getCurrentChannel, getCurrentGuild } from "@utils/discord";
import { sleep } from "@utils/misc";
import { Queue } from "@utils/Queue";
import {
    ChannelActionCreators,
    ChannelStore,
    Constants,
    FluxDispatcher,
    GuildChannelStore,
    GuildMemberCountStore,
    GuildMemberStore,
    GuildRoleStore,
    GuildStore,
    RelationshipStore,
    RestAPI,
    SelectedChannelStore,
    SelectedGuildStore,
    UserStore,
} from "@webpack/common";

// A value import from "@vencord/discord-types/enums" resolves to undefined
// under the dynamic loader, so the two enums this module needs are declared
// here. The values are Discord's and are quoted from the Equicord typings:
// packages/discord-types/enums/channel.ts and .../enums/user.ts.
const enum ChannelKind {
    DM = 1,
    GROUP_DM = 3,
}

const enum RelationshipKind {
    NONE = 0,
    FRIEND = 1,
    BLOCKED = 2,
    /** Discord's API calls this PENDING_INCOMING */
    INCOMING_REQUEST = 3,
    OUTGOING_REQUEST = 4,
    IMPLICIT = 5,
    SUGGESTION = 6,
}

export type SourceKind = "guild" | "role" | "friends" | "requests" | "gdm" | "channel";

export interface MemberSource {
    kind: SourceKind;
    id?: string;
    guildId?: string;
    label: string;
    hint?: string;
}

/**
 * Where a list of ids actually came from.
 *
 * This is not bookkeeping. "Scanned 412 of 10,000 members" means one thing when
 * those 412 are everyone the gateway could find and something else entirely
 * when they are the slice of the sidebar somebody scrolled, and a surface that
 * printed the same sentence for both would be making a claim it cannot support.
 * Every note in this module is written against one of these.
 */
export type CollectVia = "cache" | "gateway" | "roster";

export interface Collected {
    ids: string[];
    /** how many the client actually knows about */
    known: number;
    /** the real population, when it can be known (guild member count); 0 when it cannot */
    total: number;
    /** one honest sentence about what this list is and is not */
    note: string;
    /** ids dropped because they are bots */
    skippedBots: number;
    /** which path produced the list; the note is written against this */
    via: CollectVia;
    /** for the gateway path: which connection was used, for the UI to name */
    gatewayMode?: string;
}

/** how long a fetched role roster is reused before it is asked for again */
const ROLE_CACHE_MS = 5 * 60 * 1000;

/** Equicord serialises channel preloads 200ms apart; so do we. */
const PRELOAD_GAP_MS = 200;

const roleCache = new Map<string, { ids: string[]; at: number; }>();

/**
 * Channel preloads are rate-sensitive — Equicord's own OnlineMemberCountStore
 * runs every one of them through a Queue with a 200ms gap, success or failure
 * (src/plugins/memberCount/OnlineMemberCountStore.ts). Sharing one queue across
 * the whole plugin means a user mashing "Load more members" cannot outrun it.
 */
const preloadQueue = new Queue();

// --------------------------------------------------------------------------
// small helpers
// --------------------------------------------------------------------------

function isSnowflake(id: unknown): id is string {
    return typeof id === "string" && id.length > 0 && id.length < 32 && /^\d+$/.test(id);
}

function count(n: number): string {
    try {
        return n.toLocaleString();
    } catch (err) {
        return String(n);
    }
}

function unique(ids: Iterable<string>): string[] {
    const seen = new Set<string>();
    const out: string[] = [];
    for (const id of ids) {
        if (!isSnowflake(id) || seen.has(id)) continue;
        seen.add(id);
        out.push(id);
    }
    return out;
}

function guildName(guildId: string | undefined): string {
    if (!guildId) return "this server";
    try {
        return GuildStore?.getGuild?.(guildId)?.name || "this server";
    } catch (err) {
        return "this server";
    }
}

function memberCountOf(guildId: string | undefined): number {
    if (!guildId) return 0;
    try {
        const n = GuildMemberCountStore?.getMemberCount?.(guildId);
        return typeof n === "number" && n > 0 ? n : 0;
    } catch (err) {
        return 0;
    }
}

function cachedMemberIds(guildId: string): string[] {
    try {
        return GuildMemberStore?.getMemberIds?.(guildId) ?? [];
    } catch (err) {
        return [];
    }
}

// --------------------------------------------------------------------------
// the gateway, loaded only if it is actually used
// --------------------------------------------------------------------------

/**
 * The loader hands every module a CommonJS `require`; the declaration only
 * keeps the identifier from looking undeclared to a reader.
 */
declare const require: (specifier: string) => any;

let gatewayModule: any = null;
let gatewayTried = false;

/**
 * `gateway/index.ts`, required on first use rather than imported at the top.
 *
 * Two reasons, and the second is the important one. It is eighteen hundred
 * lines that nothing needs until somebody switches the gateway on, so a
 * top-level import would make every member-list decorator pay to load a
 * WebSocket supervisor. And a module that fails to evaluate throws out of
 * `require` — requiring it here means a broken or missing gateway costs the
 * user their gateway scan and a readable sentence, rather than taking marks,
 * profiles and reports down with it at load time.
 */
function gatewayApi(): any {
    if (gatewayTried) return gatewayModule;
    gatewayTried = true;
    try {
        gatewayModule = require("./gateway");
    } catch (err) {
        gatewayModule = null;
    }
    return gatewayModule;
}

export interface GatewayAvailability {
    /** whether a connection can actually be made right now */
    ready: boolean;
    /** "off" | "client" | "bot", whatever the user chose */
    mode: string;
    /** empty when ready; otherwise the sentence to show, verbatim */
    problem: string;
}

/**
 * Whether a guild scan may use the gateway, and why not when it may not.
 *
 * The gate itself lives in gateway/tokens.ts and is deliberately stricter than
 * "did the caller ask" — nothing is handed out unless the user switched the
 * gateway on, and the account-token path additionally requires that exact
 * choice to be recorded in settings. This function only reports the gate's
 * answer; it never decides anything, and it never sees a token.
 */
export function gatewayAvailability(): GatewayAvailability {
    const api = gatewayApi();
    if (!api) {
        return {
            ready: false,
            mode: "off",
            problem: "The gateway module is not available in this build of the plugin.",
        };
    }
    let mode = "off";
    try {
        mode = String(api.tokenMode?.() ?? "off");
        if (api.gatewayEnabled?.() && api.tokenFor?.(mode)) {
            return { ready: true, mode, problem: "" };
        }
    } catch (err) {
        // A settings store that is not up yet reads as "not ready", which it is.
    }
    let problem = "";
    try {
        problem = String(api.tokenProblem?.(mode) ?? "");
    } catch (err) {
        problem = "";
    }
    return { ready: false, mode, problem };
}

export interface GatewayCollectOptions {
    onProgress?(progress: { found: number; total: number; stage: string; }): void;
    signal?: AbortSignal;
}

/**
 * Every member of a guild, read off Discord's gateway.
 *
 * This is the one path in this module whose result is a roster rather than a
 * sample, and it is also the one that opens a second connection on somebody's
 * account, so it is only ever reached when `gatewayAvailability().ready` says
 * the user has asked for it explicitly.
 *
 * The socket is closed in a `finally` including on the abort path: an unclosed
 * gateway goes on heartbeating for the rest of the session, and a second one
 * opened by the next scan would mean two sessions on one account.
 */
export async function gatewayMemberIds(
    guildId: string,
    opts: GatewayCollectOptions = {}
): Promise<{ ids: string[]; botIds: string[]; found: number; total: number; coverage: number | null; mode: string; }> {
    if (!isSnowflake(guildId)) throw new Error("gatewayMemberIds needs a guild id.");

    const api = gatewayApi();
    const availability = gatewayAvailability();
    if (!api || !availability.ready) {
        throw new Error(availability.problem || "The gateway connection is not available.");
    }

    const defaults = attempt(() => api.gatewayDefaults?.() ?? {}, {} as any);
    const gateway = await api.openGateway(availability.mode);
    try {
        const scraped: any[] = await gateway.fetchMembers(guildId, {
            coverageTarget: defaults.coverageTarget,
            prefixSweep: defaults.prefixSweep,
            onProgress: opts.onProgress,
            signal: opts.signal,
        });

        const ids: string[] = [];
        const botIds: string[] = [];
        for (const member of Array.isArray(scraped) ? scraped : []) {
            if (!member || !isSnowflake(member.id)) continue;
            // The gateway hands back the bot flag itself, and it is the only
            // thing here that knows: `UserStore.getUser` cannot answer for a
            // member this client was never sent, which is most of them on this
            // path, and an unknown user reads as "not a bot" by default.
            if (member.bot) botIds.push(member.id);
            ids.push(member.id);
        }

        const found = unique(ids);
        const coverage = typeof gateway.lastCoverage === "number" ? gateway.lastCoverage : null;
        return {
            ids: found,
            botIds,
            found: found.length,
            total: memberCountOf(guildId),
            coverage,
            mode: availability.mode,
        };
    } finally {
        attempt(() => gateway.close(), undefined);
    }
}

/** Run `fn`, or hand back the fallback. Nothing in this module may throw at a caller by surprise. */
function attempt<T>(fn: () => T, fallback: T): T {
    try {
        const value = fn();
        return value === undefined ? fallback : value;
    } catch (err) {
        return fallback;
    }
}

// --------------------------------------------------------------------------
// context
// --------------------------------------------------------------------------

export function currentGuildId(): string | undefined {
    // `getCurrentGuild()` derives the guild from the open channel, which is the
    // right answer inside a member-list decorator. `SelectedGuildStore` returns
    // null in DMs, so it is only the fallback.
    try {
        const fromChannel = getCurrentGuild()?.id;
        if (isSnowflake(fromChannel)) return fromChannel;
    } catch (err) {
        /* stores not ready */
    }
    try {
        const selected = SelectedGuildStore?.getGuildId?.();
        if (isSnowflake(selected)) return selected;
    } catch (err) {
        /* stores not ready */
    }
    return undefined;
}

function currentChannel(): any {
    try {
        const channel = getCurrentChannel();
        if (channel) return channel;
    } catch (err) {
        /* stores not ready */
    }
    try {
        return ChannelStore?.getChannel?.(SelectedChannelStore?.getChannelId?.());
    } catch (err) {
        return undefined;
    }
}

// --------------------------------------------------------------------------
// roles
// --------------------------------------------------------------------------

/**
 * The guild's roles, most significant first, minus @everyone.
 *
 * `memberCount` is only filled once `roleMemberIds` has actually fetched that
 * role's roster. Discord publishes no per-role member count to the client, and
 * counting the cached members who hold the role would produce a number that
 * looks authoritative and is not — the same mistake this whole module exists to
 * avoid. An absent count means "not asked yet", not "empty".
 */
export function guildRoles(guildId: string): { id: string; name: string; memberCount?: number; }[] {
    if (!isSnowflake(guildId)) return [];

    let roles: any[] = [];
    try {
        roles = GuildRoleStore?.getSortedRoles?.(guildId) ?? [];
    } catch (err) {
        roles = [];
    }
    if (!roles.length) {
        // Older builds and cold stores: the guild record carries the same map.
        try {
            // Not on Equicord's Guild typing, but present on the live object;
            // this branch only runs when GuildRoleStore gave us nothing.
            const raw = (GuildStore?.getGuild?.(guildId) as any)?.roles;
            if (raw) roles = Object.values(raw as any);
        } catch (err) {
            roles = [];
        }
        roles.sort((a: any, b: any) => (b?.position ?? 0) - (a?.position ?? 0));
    }

    const out: { id: string; name: string; memberCount?: number; }[] = [];
    for (const role of roles) {
        if (!role || !isSnowflake(role.id)) continue;
        // The @everyone role's id is the guild id, and Discord has no roster
        // endpoint for it — see roleMemberIds.
        if (role.id === guildId) continue;
        const cached = roleCache.get(`${guildId}:${role.id}`);
        const fresh = cached && Date.now() - cached.at < ROLE_CACHE_MS ? cached : undefined;
        out.push({
            id: role.id,
            name: typeof role.name === "string" && role.name ? role.name : `role ${role.id}`,
            memberCount: fresh ? fresh.ids.length : undefined,
        });
    }
    return out;
}

/**
 * Every holder of one role, complete.
 *
 * `RestAPI.get({ url: Constants.Endpoints.GUILD_ROLE_MEMBER_IDS(guildId, roleId) })`
 * returns a plain `string[]` with no cap and no online-only filter — this is
 * the shape Equicord's ClickableRoles plugin uses
 * (src/equicordplugins/clickableRoles/index.tsx:56-66). It is the only call in
 * this module whose result is the real population rather than a sample.
 */
export async function roleMemberIds(guildId: string, roleId: string): Promise<string[]> {
    if (!isSnowflake(guildId) || !isSnowflake(roleId)) {
        throw new Error("roleMemberIds needs a guild id and a role id.");
    }
    if (roleId === guildId) {
        throw new Error(
            "@everyone has no roster endpoint — every member holds it, so Discord "
            + "will not enumerate it. Scan the server instead, and read its coverage note."
        );
    }

    const key = `${guildId}:${roleId}`;
    const cached = roleCache.get(key);
    if (cached && Date.now() - cached.at < ROLE_CACHE_MS) return cached.ids.slice();

    const endpoint = (Constants as any)?.Endpoints?.GUILD_ROLE_MEMBER_IDS;
    if (typeof endpoint !== "function") {
        throw new Error("This Discord build does not expose the role member endpoint.");
    }
    if (typeof (RestAPI as any)?.get !== "function") {
        throw new Error("Discord's REST client is not available yet. Try again in a moment.");
    }

    const response = await RestAPI.get({ url: endpoint(guildId, roleId) });
    const body = response?.body;
    if (!Array.isArray(body)) {
        throw new Error("Discord returned an unexpected shape for the role's members.");
    }

    const ids = unique(body as string[]);
    roleCache.set(key, { ids, at: Date.now() });
    return ids.slice();
}

// --------------------------------------------------------------------------
// warming a guild
// --------------------------------------------------------------------------

/**
 * Ask Discord for more of a guild's member list.
 *
 * `ChannelActionCreators.preload(guildId, defaultChannelId)` is what the client
 * itself calls when you open a channel, and it is what makes the gateway start
 * sending GUILD_MEMBER_LIST_UPDATE for that guild. It warms the *top* of the
 * list; it does not page through it.
 *
 * There is deliberately no range-subscription call here. The gateway's OP 14
 * with `channels: { [id]: [[lo, hi], ...] }` is what the selfbot uses to walk a
 * roster, and it is what a reader will come to this function looking for. Its
 * Flux equivalent, `GUILD_SUBSCRIPTIONS_CHANNEL`, appears exactly once in
 * Equicord's whole source — in a string union of event names — with zero call
 * sites and no payload typing anywhere, and the only `GUILD_SUBSCRIPTIONS_FLUSH`
 * dispatch in the tree sends `{ [guildId]: { typing: true } }` and nothing more.
 * Guessing at the payload is how a user's account gets rate-limited for a
 * feature that may not work at all, so this plugin does not guess. Scroll the
 * sidebar, or scan a role.
 */
export function warmGuild(guildId: string): Promise<void> {
    if (!isSnowflake(guildId)) return Promise.resolve();

    return new Promise<void>(resolve => {
        preloadQueue.push(async () => {
            try {
                const channel = GuildChannelStore?.getDefaultChannel?.(guildId);
                const channelId = channel?.id;
                if (channelId && typeof ChannelActionCreators?.preload === "function") {
                    await ChannelActionCreators.preload(guildId, channelId);
                }
            } catch (err) {
                // A preload that fails costs us coverage, not correctness.
            }
            try {
                await sleep(PRELOAD_GAP_MS);
            } catch (err) {
                /* sleep never rejects, but this module is loaded untyped */
            }
            resolve();
        });
    });
}

/**
 * Ask Discord for the User records behind ids we already have.
 *
 * Not in the module's contract, but a role roster comes back as bare ids and
 * the ledger has to print names: without this, scanning a role lists eighty
 * rows of digits. `GUILD_MEMBERS_REQUEST` takes at most 100 user ids per
 * dispatch and only resolves people you share the guild with, and — this is the
 * important half — it cannot *discover* ids. It hydrates a list you already
 * hold and nothing else, which is why it is not a route to full coverage.
 */
export function hydrateMembers(guildId: string, ids: string[]): void {
    if (!isSnowflake(guildId) || !Array.isArray(ids) || !ids.length) return;

    const missing: string[] = [];
    for (const id of unique(ids)) {
        let known = false;
        try {
            known = UserStore?.getUser?.(id) != null;
        } catch (err) {
            known = false;
        }
        if (!known) missing.push(id);
    }
    if (!missing.length) return;

    for (let i = 0; i < missing.length; i += 100) {
        try {
            // `dispatch` returns a promise and *rejects* while the dispatcher is
            // already dispatching, so a try/catch alone cannot catch it — the
            // rejection would escape as an unhandled one. A member whose name we
            // never learn is shown by id, which is survivable, so it is swallowed
            // deliberately rather than surfaced.
            void Promise.resolve(
                FluxDispatcher.dispatch({
                    type: "GUILD_MEMBERS_REQUEST",
                    guildIds: [guildId],
                    userIds: missing.slice(i, i + 100),
                })
            ).catch(() => undefined);
        } catch (err) {
            return;
        }
    }
}

// --------------------------------------------------------------------------
// people who are not guild members
// --------------------------------------------------------------------------

export function friendIds(): string[] {
    try {
        return unique(RelationshipStore?.getFriendIDs?.() ?? []);
    } catch (err) {
        return [];
    }
}

/**
 * Incoming friend requests, by id.
 *
 * `RelationshipStore.getPendingCount()` is a count, not a list, so the ids come
 * from walking the mutable relationship map for type 3 — the same walk
 * RelationshipNotifier does (src/plugins/relationshipNotifier/utils.ts:173-190).
 */
export function pendingRequestIds(): string[] {
    let relationships: any;
    try {
        relationships = RelationshipStore?.getMutableRelationships?.();
    } catch (err) {
        return [];
    }
    if (!relationships) return [];

    const ids: string[] = [];
    const take = (id: any, type: any) => {
        if (type === RelationshipKind.INCOMING_REQUEST && isSnowflake(id)) ids.push(id);
    };

    if (typeof relationships.forEach === "function" && typeof relationships.get === "function") {
        // A Map, which is what current builds hand back.
        relationships.forEach((type: any, id: any) => take(id, type));
    } else if (typeof relationships === "object") {
        // Older builds keyed a plain object the same way.
        for (const id of Object.keys(relationships)) take(id, relationships[id]);
    }
    return unique(ids);
}

/** One source per group DM. `recipients` excludes you, so the label says so. */
export function groupDmSources(): MemberSource[] {
    let channels: any[] = [];
    try {
        channels = ChannelStore?.getSortedPrivateChannels?.() ?? [];
    } catch (err) {
        return [];
    }

    const out: MemberSource[] = [];
    for (const channel of channels) {
        if (!channel || channel.type !== ChannelKind.GROUP_DM) continue;
        const recipients: string[] = Array.isArray(channel.recipients) ? channel.recipients : [];
        const name = channel.name
            || (Array.isArray(channel.rawRecipients)
                ? channel.rawRecipients.map((r: any) => r?.username).filter(Boolean).join(", ")
                : "")
            || "Group DM";
        out.push({
            kind: "gdm",
            id: channel.id,
            label: name,
            hint: `${count(recipients.length)} other recipient${recipients.length === 1 ? "" : "s"}`,
        });
    }
    return out;
}

// --------------------------------------------------------------------------
// the source list
// --------------------------------------------------------------------------

export function listSources(guildId?: string): MemberSource[] {
    const sources: MemberSource[] = [];
    const gid = isSnowflake(guildId) ? guildId : currentGuildId();

    if (gid) {
        const known = cachedMemberIds(gid).length;
        const total = memberCountOf(gid);
        // The hint has to say which path this source will actually take, before
        // it is chosen: "everything the gateway can find" and "the slice the
        // sidebar has loaded" are different offers, and the difference is the
        // whole reason the gateway exists.
        const viaGateway = gatewayAvailability().ready;
        sources.push({
            kind: "guild",
            id: gid,
            guildId: gid,
            label: guildName(gid),
            hint: viaGateway
                ? total
                    ? `the gateway will read the real list — ${count(total)} members`
                    : "the gateway will read the real member list"
                : total
                    ? `${count(known)} of ${count(total)} members loaded in this client`
                    : `${count(known)} members loaded in this client`,
        });

        if (guildRoles(gid).length) {
            // No id: the picker chooses the role and fills it in. Collecting a
            // role source without one is handled honestly in `collect`.
            sources.push({
                kind: "role",
                guildId: gid,
                label: `A role in ${guildName(gid)}`,
                hint: "Discord returns every holder of a role — this is the only complete list available.",
            });
        }
    }

    const friends = friendIds();
    if (friends.length) {
        sources.push({
            kind: "friends",
            label: "Your friends",
            hint: `${count(friends.length)} accounts, complete`,
        });
    }

    const requests = pendingRequestIds();
    if (requests.length) {
        sources.push({
            kind: "requests",
            label: "Incoming friend requests",
            hint: `${count(requests.length)} pending, complete`,
        });
    }

    for (const gdm of groupDmSources()) sources.push(gdm);

    const channel = currentChannel();
    if (channel && (channel.type === ChannelKind.DM || channel.type === ChannelKind.GROUP_DM)) {
        // Only private channels have recipients. A guild text channel has no
        // member list of its own that this client can enumerate, so offering
        // one here would be inventing a source.
        const recipients: string[] = Array.isArray(channel.recipients) ? channel.recipients : [];
        sources.push({
            kind: "channel",
            id: channel.id,
            guildId: undefined,
            label: channel.type === ChannelKind.DM ? "This DM" : "This group DM",
            hint: `${count(recipients.length)} other recipient${recipients.length === 1 ? "" : "s"}`,
        });
    }

    return sources;
}

// --------------------------------------------------------------------------
// collecting
// --------------------------------------------------------------------------

/**
 * The sentence for a list that came off the gateway.
 *
 * Deliberately a different sentence from `coverageNote`, not the same one with
 * a different number in it. That one is about the sidebar window this client
 * happens to hold; this one is about a roster that was actually enumerated, and
 * the two support completely different claims. Naming the connection matters
 * for the same reason: a bot with the members intent is handed the whole list
 * at once, while an account token walks the sidebar and can genuinely miss
 * people, so "which one answered" is part of what the number means.
 */
function gatewayNote(
    found: number,
    total: number,
    guild: string,
    mode: string,
    coverage: number | null
): string {
    const how = mode === "bot"
        ? "A bot connection asked Discord for the member list directly"
        : "Your account's own gateway connection read the member sidebar and swept for the rest";

    if (!total) {
        return `${how}, and found ${count(found)} members of ${guild}. Discord has not told this `
            + "client how many members the server has, so this cannot be stated as a fraction of "
            + "it — but it is a list that was enumerated rather than a window that was scrolled.";
    }
    const pct = coverage != null
        ? Math.round(coverage * 1000) / 10
        : Math.round((1000 * found) / total) / 10;
    if (found >= total) {
        return `${how}, and found all ${count(total)} members of ${guild}.`;
    }
    return `${how}, and found ${count(found)} of ${guild}'s ${count(total)} members — ${pct}% of `
        + `them. The ${count(Math.max(0, total - found))} not found are not scanned, and are not `
        + "clean: they are unexamined. Discord's own member count is itself approximate, so a "
        + "figure just short of 100% usually means the scrape finished rather than that it failed.";
}

function coverageNote(known: number, total: number, guild: string): string {
    if (!total) {
        return `Discord has not told this client how many members ${guild} has, so this list of `
            + `${count(known)} cannot be stated as a fraction of it. Treat it as a sample, not a roster.`;
    }
    const pct = Math.round((100 * known) / total);
    if (known >= total) {
        return `This client holds all ${count(total)} members of ${guild}.`;
    }
    return `This client holds ${count(known)} of ${guild}'s ${count(total)} members — ${pct}% of them. `
        + "Discord only sends the member list as the sidebar is scrolled, and in a server this size it "
        + "withholds offline members from accounts without kick, ban or manage-roles. The "
        + `${count(total - known)} not held are not scanned, and are not clean: they are unexamined. `
        + "Scanning a role instead asks Discord for its complete list of holders.";
}

/**
 * Turn a source into ids, with a sentence saying what the list is.
 *
 * `known` is what the client can see; `total` is the real population where it
 * can be known — the guild member count for a guild, and the list's own length
 * for the sources that come back complete. `total` is 0 only when nothing
 * honest can be said about the denominator.
 */
export async function collect(
    source: MemberSource,
    opts?: {
        skipBots?: boolean;
        max?: number;
        /**
         * Whether a guild source may open the gateway. Defaults to true, and
         * still does nothing unless the user switched the gateway on — but a
         * caller that wants the cheap answer (a pre-flight estimate, say) can
         * ask for the cached one without waiting minutes for a scrape.
         */
        useGateway?: boolean;
        onProgress?(progress: { found: number; total: number; stage: string; }): void;
        signal?: AbortSignal;
    }
): Promise<Collected> {
    const skipBots = opts?.skipBots !== false;
    const max = typeof opts?.max === "number" && opts.max > 0 ? Math.floor(opts.max) : 0;

    let ids: string[] = [];
    let total = 0;
    let note = "";
    // Every source but a guild comes back enumerated; the guild case sets its
    // own, because it is the only one where two paths produce two claims.
    let via: CollectVia = "roster";
    let gatewayMode: string | undefined;
    /** ids the gateway itself told us are bots, which UserStore cannot know */
    let knownBots: Set<string> | undefined;

    if (!source || typeof source.kind !== "string") {
        return { ids: [], known: 0, total: 0, note: "No source was chosen.", skippedBots: 0, via: "cache" };
    }

    switch (source.kind) {
        case "guild": {
            const gid = source.guildId || source.id;
            if (!isSnowflake(gid)) {
                return {
                    ids: [], known: 0, total: 0,
                    note: "That server is not known to this client.",
                    skippedBots: 0, via: "cache",
                };
            }

            // The gateway path first, when the user has switched it on. It is
            // the only way a client mod learns about a member Discord has not
            // already sent it, and it is what makes a server scan a scan of the
            // server rather than of the sidebar.
            const useGateway = opts?.useGateway !== false && gatewayAvailability().ready;
            if (useGateway) {
                try {
                    const scraped = await gatewayMemberIds(gid, {
                        onProgress: opts?.onProgress,
                        signal: opts?.signal,
                    });
                    ids = scraped.ids;
                    total = scraped.total;
                    via = "gateway";
                    gatewayMode = scraped.mode;
                    knownBots = new Set(scraped.botIds);
                    note = gatewayNote(ids.length, total, guildName(gid), scraped.mode, scraped.coverage);
                    break;
                } catch (err: any) {
                    // A gateway that would not connect, or a scrape that was
                    // stopped, must not silently become a cache read reported
                    // as a full scan. The fallback is still taken — a partial
                    // answer beats none — but the note leads with the failure.
                    ids = unique(cachedMemberIds(gid));
                    total = memberCountOf(gid);
                    via = "cache";
                    note = `The gateway scan did not run: ${err?.message ?? String(err)} `
                        + `This list is the client's own cache instead. `
                        + coverageNote(ids.length, total, guildName(gid));
                    break;
                }
            }

            ids = unique(cachedMemberIds(gid));
            total = memberCountOf(gid);
            via = "cache";
            note = coverageNote(ids.length, total, guildName(gid));
            break;
        }

        case "role": {
            const gid = source.guildId;
            const rid = source.id;
            if (!isSnowflake(gid) || !isSnowflake(rid)) {
                return {
                    ids: [], known: 0, total: 0,
                    note: "Pick a role first — a role source needs both the server and the role.",
                    skippedBots: 0, via: "roster",
                };
            }
            try {
                ids = await roleMemberIds(gid, rid);
            } catch (err: any) {
                return {
                    ids: [], known: 0, total: 0,
                    note: `Discord would not list that role's members: ${err?.message ?? String(err)}`,
                    skippedBots: 0, via: "roster",
                };
            }
            total = ids.length;
            note = `Complete: Discord returned every one of the ${count(ids.length)} holders of this role. `
                + "No sidebar window is involved, so there is no coverage caveat — but members who hold "
                + "no role, or a different one, are not in this list.";
            break;
        }

        case "friends": {
            ids = friendIds();
            total = ids.length;
            note = `Complete: every one of the ${count(ids.length)} accounts on your friends list.`;
            break;
        }

        case "requests": {
            ids = pendingRequestIds();
            total = ids.length;
            note = `Complete: every one of the ${count(ids.length)} friend requests currently waiting on you. `
                + "Requests you sent are not included.";
            break;
        }

        case "gdm":
        case "channel": {
            let channel: any;
            try {
                channel = isSnowflake(source.id) ? ChannelStore?.getChannel?.(source.id) : currentChannel();
            } catch (err) {
                channel = undefined;
            }
            if (!channel) {
                return {
                    ids: [], known: 0, total: 0,
                    note: "That channel is not known to this client.",
                    skippedBots: 0, via: "roster",
                };
            }
            ids = unique(Array.isArray(channel.recipients) ? channel.recipients : []);
            total = ids.length;
            note = `Complete: every one of the ${count(ids.length)} other recipients of this conversation. `
                + "Discord's recipient list excludes you, so you are not in it.";
            break;
        }

        default:
            return {
                ids: [], known: 0, total: 0,
                note: `Unknown source kind "${source.kind}".`,
                skippedBots: 0, via: "cache",
            };
    }

    const known = ids.length;

    let skippedBots = 0;
    if (skipBots) {
        const kept: string[] = [];
        for (const id of ids) {
            let bot = knownBots?.has(id) === true;
            try {
                if (!bot) bot = UserStore?.getUser?.(id)?.bot === true;
            } catch (err) {
                bot = bot || false;
            }
            if (bot) skippedBots++;
            else kept.push(id);
        }
        ids = kept;
        if (skippedBots) {
            note += ` ${count(skippedBots)} bot${skippedBots === 1 ? "" : "s"} dropped — Rotector tracks people.`;
        }
    }

    if (max && ids.length > max) {
        const dropped = ids.length - max;
        ids = ids.slice(0, max);
        note += ` Capped at ${count(max)}: ${count(dropped)} member${dropped === 1 ? "" : "s"} `
            + "were not scanned and are not part of the result.";
    }

    return { ids, known, total, note, skippedBots, via, gatewayMode };
}

// --------------------------------------------------------------------------
// passive harvest
// --------------------------------------------------------------------------

/**
 * Pull user ids out of a GUILD_MEMBER_LIST_UPDATE payload.
 *
 * This is free coverage: the event fires as the user scrolls the sidebar,
 * whether or not the plugin asked for anything. The `ops` array is not typed or
 * consumed anywhere in Equicord, so whether the Flux dispatch keeps the
 * gateway's snake_case shape or rekeys it is genuinely unknown — hence the
 * tolerance below rather than a single field read. Op kinds are the gateway's:
 * SYNC carries `items`, INSERT and UPDATE carry one `item`, DELETE and
 * INVALIDATE carry no member at all, and `{ group: ... }` items are the role
 * dividers rather than people.
 */
export function harvestMemberListUpdate(payload: any): string[] {
    const ops = payload?.ops;
    if (!Array.isArray(ops)) return [];

    const found: string[] = [];
    for (const op of ops) {
        if (!op || typeof op !== "object") continue;
        const kind = op.op ?? op.type;

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
            const { member } = item;
            if (!member || typeof member !== "object") continue;
            const { user } = member;
            const id = (user && (user.id ?? user.userId ?? user.user_id))
                ?? member.userId
                ?? member.user_id;
            if (isSnowflake(id)) found.push(id);
        }
    }
    return unique(found);
}
