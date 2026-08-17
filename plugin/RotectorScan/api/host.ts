/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Can this renderer reach the Rotector API, and if not, whose fault is it?
 *
 * There is a tempting shortcut here that is wrong. `VencordNative.csp
 * .isDomainAllowed(url, ["connect-src"])` looks like the answer, and its own
 * doc comment says why it is not: "Only supports full explicit matches, not
 * wildcards." Equicord's equicordHelper native sets `CspPolicies["*"] = CSPSrc`
 * — every plugin native is loaded into the main process at startup regardless
 * of whether its plugin is enabled — so on an Equicord desktop build the
 * wildcard already permits connect-src to anywhere while `isDomainAllowed`
 * still answers false for roscoe.rotector.com. Trusting it would tell a user
 * their connection is blocked while their scans are running perfectly, and push
 * them into an OS permission dialog and a full restart for nothing.
 *
 * So reachability is decided from a real request, and only from a real request:
 *
 *   1. the backend's own ping() — one cheap request, built for whichever host
 *      the URL names. If it answers, the state is "ok" and nothing else matters.
 *   2. If it failed with a TypeError, the request never left the browser. That
 *      one failure mode covers a CSP refusal, a missing CORS header, a rejected
 *      preflight and having no network at all, and JS cannot tell them apart.
 *      So a second, no-cors GET goes out. A no-cors request is exempt from CORS
 *      but *not* from CSP, which separates the two:
 *        - the no-cors probe also fails -> the browser refused to send it, or
 *          the host is unreachable. That is the shape CSP makes, and it is the
 *          only case where asking for host permission can help.
 *        - the no-cors probe succeeds (an opaque response) -> the request went
 *          out and the host answered, so CSP is not the problem and a
 *          permission grant would change nothing. The block is CORS-shaped.
 *
 * Both of those are real requests to the API's host, so both are paid for out
 * of that host's shared rate-limit budget before they go out. A probe is a
 * diagnostic, not an exemption: the panel that runs it is one Ctrl+R away from
 * a running scan, and units spent here are units that scan no longer has.
 */

import { buildBackend, DEFAULT_BACKEND } from "./backend";
import type { LookupBackend } from "./backend";
import { OKAPPIKI_BASE } from "./okappiki";
import { BASE_URL } from "./rotector";

export type HostState = "ok" | "needs-permission" | "blocked" | "unknown";

export interface HostReport {
    state: HostState;
    detail: string;
}

/** Probe whether the renderer may reach `url`. Never throws. */
export async function checkHost(url?: string): Promise<HostReport> {
    const target = normalise(url);
    const host = hostOf(target);

    try {
        const client = clientFor(target);
        const result = await client.ping();

        if (result.ok) {
            return {
                state: "ok",
                detail: `${host} answered. Lookups work from this client.`,
            };
        }

        if (result.neverSent) {
            // Nothing left the browser: the limiter was still holding callers
            // back. Wording this as "the API answered" would blame the service
            // for our own queue, and the answer is to wait rather than to
            // change anything.
            return {
                state: "unknown",
                detail:
                    `The check could not be sent without going over the rate limit for ` +
                    `${host} — a scan is probably using the budget, or a 429 is still ` +
                    "being waited out. Try again in a few seconds.",
            };
        }

        if (!result.corsBlocked) {
            // The request reached the API and it said something unwelcome — a
            // rejected key, a rate limit, an outage. Nothing the host client
            // can grant will change that, so it is not "blocked".
            return {
                state: "unknown",
                detail: `Reached ${host}, but it answered: ${result.error || "an unknown error"}.`,
            };
        }

        const left = await requestLeftTheBrowser(target);
        if (left === "unknown") {
            // The follow-up probe is a request like any other and could not be
            // paid for out of the rate-limit budget in time. Saying "blocked"
            // here would blame the client for a queue.
            return {
                state: "unknown",
                detail:
                    `The request to ${host} did not come back, and the follow-up check ` +
                    "that tells a client block from a CORS refusal could not be sent " +
                    "without going over the rate limit — a scan is probably using the " +
                    "budget, or a 429 is still being waited out. Try again in a few " +
                    "seconds.",
            };
        }
        if (left === "sent") {
            return {
                state: "blocked",
                detail:
                    `The request reached ${host} but the browser rejected the response, ` +
                    "which means missing or refused CORS headers rather than a client " +
                    "restriction. Host permission would not change this. If it worked " +
                    "before, the API's CORS configuration or something between you and " +
                    "it has changed.",
            };
        }

        if (cspSupported()) {
            return {
                state: "needs-permission",
                detail:
                    `${hostClientName()} would not send the request to ${host} at all. ` +
                    "That is what a content-security-policy block looks like. You can " +
                    "grant connect-src permission for this host below; it opens a dialog " +
                    "from the client itself and needs a full restart of Discord — not " +
                    "just Ctrl+R — before it takes effect. If you are offline, that is " +
                    "the other thing this looks like.",
            };
        }

        return {
            state: "blocked",
            detail:
                `The request to ${host} never left ${hostClientName()}, and this build ` +
                "has no host-permission API to ask with. The usual causes are being " +
                "offline, DNS failing, or a network that blocks the host.",
        };
    } catch (err: any) {
        // checkHost is called from a settings panel and from error states; it
        // must never be the thing that throws.
        return {
            state: "unknown",
            detail: `Could not check ${host}: ${err?.message || String(err)}.`,
        };
    }
}

/**
 * Ask the host client for connect-src permission. Returns a human-readable
 * outcome — this is shown to the user verbatim, so every branch is a sentence.
 */
export async function requestHostPermission(url?: string): Promise<string> {
    const target = normalise(url);
    const host = hostOf(target);
    const csp = cspApi();

    if (!csp?.requestAddOverride) {
        return (
            `${hostClientName()} has no host-permission API — that dialog only exists ` +
            "on the desktop client. Nothing was changed."
        );
    }

    let result: string;
    try {
        result = await csp.requestAddOverride(target, ["connect-src"], "RotectorScan");
    } catch (err: any) {
        return `The permission request failed: ${err?.message || String(err)}.`;
    }

    switch (result) {
        case "ok":
            return (
                `Allowed. ${host} is now in this client's connect-src list. Fully close ` +
                "and restart Discord — a reload is not enough — and check again."
            );
        case "cancelled":
            return "You cancelled the permission dialog. Nothing was changed.";
        case "unchecked":
            return (
                "The dialog needs the trust checkbox ticked as well as Allow. Nothing " +
                "was changed."
            );
        case "conflict":
            return (
                `${host} already has a rule in this client. If connections are still ` +
                "failing, the block is not the content-security-policy and permission " +
                "cannot fix it."
            );
        case "invalid":
            return `The client rejected the request for ${host} as invalid.`;
        default:
            return `The client answered "${String(result)}"; nothing is known to have changed.`;
    }
}

/** Which client mod this is running under, for use in a sentence. */
export function hostClientName(): string {
    try {
        if (typeof IS_VESKTOP !== "undefined" && IS_VESKTOP) return "Vesktop";
        if (typeof IS_EQUIBOP !== "undefined" && IS_EQUIBOP) return "Equibop";
        if (isEquicord()) return "Equicord";
        if (typeof IS_WEB !== "undefined" && IS_WEB) return "the web client";
        return "Vencord";
    } catch {
        return "this client";
    }
}

/** Whether the host-permission API exists at all on this build. */
export function cspSupported(): boolean {
    return cspApi() != null;
}

// --------------------------------------------------------------------------
// internals
// --------------------------------------------------------------------------

/** what the no-cors probe was able to establish */
type ProbeResult = "sent" | "refused" | "unknown";

/** a probe that cannot be paid for within this long is not worth waiting on */
const PROBE_BUDGET_MS = 8_000;

/**
 * A no-cors GET, purely to find out whether the browser was willing to send
 * anything at all.
 *
 * The response is opaque by definition — no status, no headers, no body — so
 * this can only ever answer "the request went out" or "it did not". That is
 * exactly the one bit of information the CORS/CSP distinction needs, and it is
 * why this is not, and cannot be turned into, a way to read the API without
 * CORS. `mode: "no-cors"` also means no custom headers, so nothing about the
 * plugin's auth or content type is involved.
 *
 * It is still a request to the API's host, so it costs a request unit from that
 * host's limiter like everything else. If the budget cannot be had inside
 * PROBE_BUDGET_MS the probe is abandoned rather than sent unmetered, and the
 * caller says so instead of guessing — "unknown" is the honest third answer.
 */
async function requestLeftTheBrowser(target: string): Promise<ProbeResult> {
    const controller = newController();
    const timer = controller
        ? setTimeout(() => {
            try {
                controller.abort();
            } catch {
                // already aborted; nothing to do
            }
        }, PROBE_BUDGET_MS)
        : null;

    try {
        await clientFor(target).limiter.acquire(1, controller?.signal);
    } catch {
        // The only way acquire() fails is the abort above: the limiter is still
        // holding callers back (a 429 penalty, or a scan spending the window).
        if (timer !== null) clearTimeout(timer);
        return "unknown";
    }

    // The deadline has to survive into the fetch. Clearing it in a `finally`
    // above left this request with no timeout and no signal at all, so a host
    // that black-holes the connection never settles and the status panel sits
    // on "checking…" for the rest of the session.
    try {
        await fetch(target, {
            method: "GET",
            mode: "no-cors",
            credentials: "omit",
            cache: "no-store",
            referrerPolicy: "no-referrer",
            signal: controller?.signal,
        });
        return "sent";
    } catch {
        return "refused";
    } finally {
        if (timer !== null) clearTimeout(timer);
    }
}

function newController(): AbortController | null {
    try {
        return typeof AbortController === "undefined" ? null : new AbortController();
    } catch {
        return null;
    }
}

/**
 * One client per base URL, kept for the life of the session.
 *
 * The client is reused so its options and caches are not rebuilt on every
 * mount of the status panel. What actually keeps the probes honest is a floor
 * below this: `RotectorClient` takes its limiter from `sharedLimiter(baseUrl)`
 * in api/ratelimit.ts, so this client and the scan's client — separate objects,
 * built from different options — spend the same window. Before that was true, a
 * probe on every panel mount was quietly spending units the scan could not see.
 */
const clients = new Map<string, LookupBackend>();

function clientFor(target: string): LookupBackend {
    const base = baseOf(target);
    let client = clients.get(base);
    if (!client) {
        // No API key: ping does not need one, and a probe should not be the
        // thing that reveals whether a key is valid. No rate-limit options
        // either — the shared limiter is already configured by whoever built
        // the scan's client from settings, and a probe must not reset it.
        client = buildBackend(backendForBase(base), { baseUrl: base, cacheTtl: 0 });
        clients.set(base, client);
    }
    return client;
}

/**
 * Which client speaks to this host.
 *
 * Built through `buildBackend` rather than hard-coded to `RotectorClient`,
 * because the two backends share no endpoint: a `RotectorClient` pointed at
 * okappiki.com would POST `/v1/lookup/discord/user` at a host that has never
 * heard of it and report the 404 as "the API answered with an error" — a false
 * failure, produced by the diagnostic itself. Okappiki's own `ping()` uses the
 * cheap `vencord_check_flag` action precisely so that checking reachability
 * does not spend a live Rotector lookup out of Okappiki's budget.
 *
 * An unrecognised host reads as Rotector rather than throwing: `checkHost` is
 * called from a settings panel and from error states, and a base URL nobody
 * recognises is a question about connectivity, not a reason to have no answer.
 */
function backendForBase(base: string): string {
    const host = hostOf(base).toLowerCase();
    if (host === hostOf(OKAPPIKI_BASE).toLowerCase()) return "okappiki";
    if (host === hostOf(BASE_URL).toLowerCase()) return "rotector";
    return DEFAULT_BACKEND;
}

function cspApi(): any {
    try {
        return (globalThis as any)?.VencordNative?.csp ?? null;
    } catch {
        return null;
    }
}

/**
 * Equicord ships EquicordHelper as a built-in plugin; Vencord does not. There
 * is no `Equicord` global to look at, and the build constants do not
 * distinguish the two, so the plugin record is the available evidence.
 */
function isEquicord(): boolean {
    try {
        const plugins = (globalThis as any)?.Vencord?.Plugins?.plugins;
        return !!plugins && "EquicordHelper" in plugins;
    } catch {
        return false;
    }
}

/**
 * The URL a bare `checkHost()` is about.
 *
 * The default is the host the user's scans actually go to, not Rotector's — a
 * connectivity panel that reports on a service the operator switched away from
 * is a panel that can say "everything works" while every scan fails. The
 * backend is read off the global settings object rather than imported, because
 * settings.ts imports the status panel that imports this file, and a top-level
 * import back into settings would close that loop at load time. Every step is
 * guarded: an unreadable settings store simply means Rotector, which is the
 * default backend anyway.
 */
function normalise(url?: string): string {
    const value = String(url || "").trim();
    if (value) return value;
    try {
        const stored = (globalThis as any)?.Vencord?.Settings?.plugins?.RotectorScan;
        const name = String(stored?.backend ?? "").trim().toLowerCase();
        if (name === "okappiki") return OKAPPIKI_BASE;
    } catch {
        // Settings are not readable; the default backend is the right answer.
    }
    return BASE_URL;
}

function baseOf(target: string): string {
    try {
        const parsed = new URL(target);
        return `${parsed.protocol}//${parsed.host}`;
    } catch {
        return String(target).replace(/\/+$/, "");
    }
}

function hostOf(target: string): string {
    try {
        return new URL(target).host;
    } catch {
        return target;
    }
}
