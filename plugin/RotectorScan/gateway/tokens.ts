/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Which token, if any, the gateway connection may use — and the consent gate in
 * front of the one that carries a risk.
 *
 * There are two, and they are not equivalent:
 *
 *  - **A bot token** is the clean path. A bot application with the Server
 *    Members Intent switched on asks Discord for `{ query: "", limit: 0 }` and
 *    receives the entire roster, offline members included, in one request. No
 *    sidebar scraping, no prefix sweep, no Terms of Service problem, and the
 *    result is the actual member list rather than a sample of it. This is the
 *    path to recommend, and `MODE_NOTES` says so in the UI's own words.
 *  - **The account's own token** is what the terminal app uses, and it is
 *    automation of a user account. Discord's Terms of Service prohibit that,
 *    with no exception for safety tooling, and accounts caught driving the API
 *    this way get disabled. It is off by default, it stays off until the user
 *    switches `gatewayEnabled` on *and* sets `gatewayToken` to "client", and the
 *    UI has to show `CONSENT_NOTICE` before the first connection.
 *
 * Rules that hold for every line in this file: the token is never logged, never
 * put into an error message or a thrown Error, never written to settings, never
 * stored anywhere it could be persisted, and never sent anywhere but Discord's
 * own gateway. Functions here return it to exactly one caller — `DiscordGateway`
 * — which puts it straight into a WeakMap and into IDENTIFY.
 */

import { find } from "@webpack";

import { settings } from "../settings";

/** how a caller names the connection it wants */
export type TokenMode = "off" | "client" | "bot";

export const TOKEN_MODES: TokenMode[] = ["off", "client", "bot"];

/**
 * The sentence the UI must show before the first connection on the account's own
 * token. It is the app's README paragraph, adapted, and it is deliberately not
 * softened: the risk is the user's account.
 */
export const CONSENT_NOTICE =
    "Automating a user account is against Discord's Terms of Service, whatever the "
    + "account is used for. Discord does not carve out an exception for safety tooling, "
    + "and accounts caught driving the API this way get disabled. This connection only "
    + "ever reads — it opens a second gateway session, subscribes to the member sidebar "
    + "and asks for members by name — but that is still automation of a user account, "
    + "and the risk of losing the account is real and entirely yours. A bot with the "
    + "Server Members Intent does the same job, better and with no Terms problem.";

/** one plain sentence per mode, for the settings screen and the scan modal */
export const MODE_NOTES: Record<TokenMode, string> = {
    off:
        "The gateway is not used. Server scans see only the members this client has "
        + "already been sent, which in a large server is the sidebar window you have "
        + "scrolled — scan a role instead for a complete list.",
    client:
        "Your own account opens a second gateway session and reads the member sidebar. "
        + "This is automation of a user account and against Discord's Terms of Service; "
        + "the risk of losing the account is yours.",
    bot:
        "A bot application you own connects instead. With SERVER MEMBERS INTENT switched "
        + "on in the Developer Portal it can ask for every member at once — the complete "
        + "roster, offline members included — and there is no Terms of Service problem. "
        + "The bot has to be in the server you are scanning.",
};

// --------------------------------------------------------------------------
// settings, read defensively
// --------------------------------------------------------------------------

/** The live settings object, or an empty one when the plugin is not registered yet. */
function stored(): Record<string, any> {
    try {
        return (settings.store as unknown as Record<string, any>) ?? {};
    } catch {
        // `settings.store` throws until the loader has registered the plugin.
        return {};
    }
}

/** Whether the user has switched the gateway on at all. Default is off. */
export function gatewayEnabled(): boolean {
    return stored().gatewayEnabled === true;
}

/** Which connection the user chose. Anything unrecognised reads as "off". */
export function tokenMode(): TokenMode {
    const raw = String(stored().gatewayToken ?? "off");
    return TOKEN_MODES.indexOf(raw as TokenMode) === -1 ? "off" : raw as TokenMode;
}

/**
 * Defaults for `fetchMembers`, read from settings.
 *
 * `coverageTarget` is stored as a percentage because that is what the number
 * means to a person; the gateway wants a fraction.
 */
export function gatewayDefaults(): { coverageTarget: number; prefixSweep: boolean; } {
    const s = stored();
    const percent = Number(s.coverageTarget);
    const target = Number.isFinite(percent) ? Math.min(100, Math.max(1, percent)) / 100 : 0.995;
    return {
        coverageTarget: target,
        prefixSweep: s.gatewayPrefixSweep !== false,
    };
}

// --------------------------------------------------------------------------
// the account's own token
// --------------------------------------------------------------------------

let tokenModule: any = null;

/**
 * Discord's token accessor, found in the webpack cache.
 *
 * There is no Equicord helper for this — nothing in its source reads the account
 * token, because almost nothing has any business doing so. The module is the one
 * exporting `getToken` alongside `setToken`/`removeToken`, and `find` descends
 * into `exports.default`, which is where it actually lives.
 *
 * Only a successful lookup is cached. A miss must stay uncached: this can be
 * called before the module has been required, and remembering "no" would leave
 * the gateway permanently unavailable for the rest of the session.
 */
function resolveTokenModule(): any {
    if (tokenModule) return tokenModule;
    try {
        const found = find(
            (m: any) =>
                m
                && typeof m.getToken === "function"
                && (typeof m.setToken === "function"
                    || typeof m.removeToken === "function"
                    || typeof m.hideToken === "function"),
            { isIndirect: true }
        );
        if (found) tokenModule = found;
    } catch {
        // A filter that threw on some exotic module export. Nothing to cache.
    }
    return tokenModule;
}

/**
 * The logged-in account's token, or null.
 *
 * Null covers every failure the same way — no module, no session, a shape that
 * is not a token — because none of the distinctions are worth a message that
 * risks quoting the value. The caller's job is to say "this client will not give
 * up an account token", which `tokenProblem` does.
 */
export function clientToken(): string | null {
    try {
        const value = resolveTokenModule()?.getToken?.();
        return looksLikeToken(value) ? value : null;
    } catch {
        return null;
    }
}

/** Whether this build exposes an account token at all. Reveals nothing about it. */
export function hasClientToken(): boolean {
    return clientToken() !== null;
}

/**
 * A token is a string with no whitespace and some length to it. This is a
 * sanity check on the *shape* only — never a log line, never a message, and
 * never anything that echoes part of the value.
 */
function looksLikeToken(value: unknown): value is string {
    return typeof value === "string" && value.length >= 20 && !/\s/.test(value);
}

// --------------------------------------------------------------------------
// the bot token
// --------------------------------------------------------------------------

/**
 * The configured bot token, trimmed, or "".
 *
 * A pasted token often arrives with the `Bot ` prefix from a copied
 * authorization header; that is stripped here so the stored value is the token
 * itself and `tokenFor` can add the prefix back in exactly one place.
 */
export function botToken(): string {
    const raw = stored().botToken;
    if (typeof raw !== "string") return "";
    const trimmed = raw.trim().replace(/^Bot\s+/i, "").trim();
    return looksLikeToken(trimmed) ? trimmed : "";
}

// --------------------------------------------------------------------------
// the gate
// --------------------------------------------------------------------------

/**
 * The token and identity for `mode`, or null when that connection may not be
 * made.
 *
 * The gate is deliberately stricter than "did the caller ask for it":
 *
 *  - Nothing is handed out at all unless `gatewayEnabled` is on.
 *  - The account path additionally requires `gatewayToken` to be exactly
 *    "client". A caller asking for "client" while the setting says otherwise is
 *    a bug — a UI that got out of step, or a preset that outlived a settings
 *    change — and the safe answer to a bug about automating someone's account is
 *    no. The consent is the setting; nothing else may stand in for it.
 *  - The bot path only needs a token to have been configured, because a bot
 *    application connecting to Discord is what bot applications are for.
 */
export function tokenFor(mode: string): { token: string; isBot: boolean; } | null {
    if (!gatewayEnabled()) return null;

    const wanted = String(mode || "off");
    if (wanted === "bot") {
        const token = botToken();
        // Discord's gateway accepts a bot token with or without the prefix; the
        // prefixed form is what an application sends everywhere else, so it is
        // the form used here too.
        return token ? { token: `Bot ${token}`, isBot: true } : null;
    }

    if (wanted === "client") {
        if (tokenMode() !== "client") return null;
        const token = clientToken();
        return token ? { token, isBot: false } : null;
    }

    return null;
}

/**
 * Why `tokenFor(mode)` returned null, as a sentence for the user.
 *
 * Returns "" when there is no problem. Every branch is shown verbatim, and none
 * of them says anything about the value of a token.
 */
export function tokenProblem(mode: string): string {
    const wanted = String(mode || "off");

    if (wanted === "off") {
        return "The gateway connection is switched off, so a server scan sees only the "
            + "members this client already holds.";
    }
    if (!gatewayEnabled()) {
        return "The gateway connection is switched off in this plugin's settings. Nothing "
            + "will connect until it is switched on.";
    }
    if (wanted === "bot") {
        if (!botToken()) {
            return "No bot token is configured. Create an application in the Discord Developer "
                + "Portal, switch on SERVER MEMBERS INTENT under Bot -> Privileged Gateway "
                + "Intents, invite the bot to the server, and paste its token into the plugin's "
                + "settings.";
        }
        return "";
    }
    if (wanted === "client") {
        if (tokenMode() !== "client") {
            return "Using your own account for this has to be chosen explicitly in the plugin's "
                + "settings, and it has not been. " + CONSENT_NOTICE;
        }
        if (!hasClientToken()) {
            return "This client will not give up an account token — the module Discord keeps it "
                + "in was not found on this build. Use a bot token instead, which is the better "
                + "path anyway.";
        }
        return "";
    }
    return `"${wanted}" is not a connection this plugin knows about.`;
}

/** One sentence describing what `mode` does, for a settings row or a picker. */
export function describeMode(mode: string): string {
    const wanted = String(mode || "off");
    return MODE_NOTES[wanted as TokenMode] ?? MODE_NOTES.off;
}
