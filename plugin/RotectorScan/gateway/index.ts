/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The gateway module's public face: the client, the token gate, and one helper
 * that puts them together so no caller has to.
 *
 * `openGateway` exists because there is exactly one correct order — ask
 * gateway/tokens.ts whether this connection is allowed, then connect with what
 * it hands back — and a caller that assembles that itself is a caller that can
 * get it wrong in the one place where getting it wrong means automating
 * someone's account without their say-so.
 */

export {
    absorbOps,
    BOT_INTENTS,
    candidateChannels,
    canChunkGuild,
    CHANNELS_PER_REQUEST,
    COVERAGE_TARGET,
    DiscordGateway,
    GATEWAY_URL,
    GatewayError,
    INTENT_GUILD_MEMBERS,
    INTENT_GUILDS,
    parseMember,
    PREFIX_QUERY_DELAY,
    QUERY_LIMIT,
    RANGE_SIZE,
    RANGES_PER_REQUEST,
} from "./gateway";
export type { FetchOptions, FetchProgress, ScrapedMember } from "./gateway";

export {
    botToken,
    clientToken,
    CONSENT_NOTICE,
    describeMode,
    gatewayDefaults,
    gatewayEnabled,
    hasClientToken,
    MODE_NOTES,
    TOKEN_MODES,
    tokenFor,
    tokenMode,
    tokenProblem,
} from "./tokens";
export type { TokenMode } from "./tokens";

import { DiscordGateway, GatewayError } from "./gateway";
import { tokenFor, tokenMode, tokenProblem } from "./tokens";

/**
 * Connect a gateway for `mode`, or throw a sentence explaining why not.
 *
 * `mode` defaults to whatever the user chose in settings. The thrown message is
 * `tokenProblem`'s, which is written to be shown verbatim and never mentions a
 * token's value. The caller owns the returned object and must `close()` it —
 * including on the error path of whatever it does next, because an unclosed
 * socket keeps heartbeating for the rest of the session.
 */
export async function openGateway(mode?: string, timeoutMs?: number): Promise<DiscordGateway> {
    const wanted = mode ? String(mode) : tokenMode();
    const credentials = tokenFor(wanted);
    if (!credentials) {
        throw new GatewayError(
            tokenProblem(wanted) || "This gateway connection is not available."
        );
    }

    const gateway = new DiscordGateway(credentials.token, credentials.isBot);
    try {
        await gateway.connect(timeoutMs);
    } catch (err) {
        // A half-open socket left behind by a failed handshake would go on
        // reconnecting on its own; the supervisor is deliberately persistent.
        gateway.close();
        throw err;
    }
    return gateway;
}
