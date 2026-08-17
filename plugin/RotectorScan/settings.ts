/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Every setting the plugin has, grouped the way config.example.toml groups
 * them, plus the three readers that turn stored values into the shapes the rest
 * of the plugin actually wants: a triage policy, a client configuration, and the
 * verdict floor a mark has to clear before it is drawn.
 *
 * Two rules govern this file.
 *
 * Nothing here may touch `settings.store` at module scope. The store getter
 * throws until the loader has assigned `settings.pluginName`, so every read
 * happens inside a function, behind a try/catch, with a fallback — a settings
 * read is never allowed to be the thing that takes a decorator down.
 *
 * And nothing here is a cache. Rotector's Terms of Use forbid retaining their
 * responses beyond 24 hours; plugin settings are mirrored into settings.json on
 * every change and are never swept, so a finding written here would be a finding
 * kept forever. Findings live in store.ts, in memory, and — once a scan has
 * finished — in features/history.ts's own IndexedDB database, which is swept and
 * clamped to the same ceiling. Those two places, and no others.
 *
 * That ceiling is 23 hours by default and is the one thing here a setting can
 * move: `retainBeyondTerms` lifts it and `retentionHours` says how far, both of
 * them below. It is off by default, it is a deliberate choice to hold
 * Rotector's responses outside the terms they were served under, and every
 * finding served from beyond the 24-hour window is labelled stale with its age
 * wherever it appears — see `isStale` and `staleNote` in api/types.ts. What no
 * setting does is put findings anywhere new: the two homes above are still the
 * only two.
 */

import { definePluginSettings } from "@api/Settings";
import { OptionType } from "@utils/types";

import {
    backendFrom,
    backendOptionsFrom,
    RETENTION_HOURS_DEFAULT,
    retainBeyondTermsFrom,
    retentionCeilingMsFrom,
    retentionHoursFrom,
} from "./api/backend";
import type { BackendName } from "./api/backend";
import type { ClientOptions } from "./api/rotector";
import { DEFAULT_POLICY } from "./api/triage";
import type { TriagePolicy } from "./api/triage";
import { ALL_VERDICTS, Verdict, VERDICT_NAMES } from "./api/verdict";
import { HistoryPurgePanel, StatusPanel } from "./components/StatusPanel";
import { reportStore } from "./store";

/**
 * The loader hands every module a CommonJS `require`, declared here only so the
 * identifier does not look undeclared to a reader. It is used for exactly one
 * thing below: reaching features/vibe.ts from an `onChange` without importing
 * it, because vibe.ts imports this file and a top-level import both ways would
 * make the player's module-scope constants depend on a half-built settings
 * module. A require inside a handler runs long after both are loaded.
 */
declare const require: (specifier: string) => any;

/** the one class the motion gate keys off; style.css writes `html.rsb-motion` */
export const MOTION_CLASS = "rsb-motion";

/**
 * Rebuild the API client on the next lookup.
 *
 * Every setting that shapes a request carries this: the client holds its own
 * limiter and caches, so changing the rate limit or the key on a live client
 * would leave the old budget in place until a reload.
 */
function rebuildClient(): void {
    try {
        reportStore.setClient(null);
    } catch {
        // The store is not up yet (a settings write during startup). The client
        // is built lazily from these same values, so there is nothing to fix.
    }
}

/**
 * The vibe player, reached without importing it.
 *
 * features/vibe.ts imports this module for its settings, so importing it back
 * at the top would be a load-time cycle around the one object whose whole job
 * is to hold a live `<audio>` element. Both handlers below run from a settings
 * change, long after every module is loaded, so a require is safe there and an
 * absent player is simply a player there is nothing to stop.
 */
function vibeModule(): any {
    try {
        return require("./features/vibe");
    } catch {
        return null;
    }
}

function stopVibe(): void {
    try {
        vibeModule()?.vibePlayer?.stop?.();
    } catch {
        // Switching the feature off must not throw out of a settings screen.
    }
}

function setVibeVolume(value: number): void {
    try {
        if (Number.isFinite(value)) vibeModule()?.vibePlayer?.setVolume?.(value);
    } catch {
        // The player polls this setting on every timeupdate anyway; the call is
        // only here so a drag of the slider is heard immediately.
    }
}

export const settings = definePluginSettings({
    // ----------------------------------------------------------------------
    // [scan] — which service answers "is this member flagged".
    //
    // They are alternatives, not layers: a scan asks one of them. Okappiki's
    // own response happens to carry a Rotector verdict alongside its two other
    // sources, so choosing it does not give Rotector's answer up.
    // ----------------------------------------------------------------------
    backend: {
        type: OptionType.SELECT,
        description:
            "Which service a scan asks. Rotector answers a hundred members per request. Okappiki " +
            "answers one member per request and has no batch endpoint, so the same server costs " +
            "roughly fifty times as many requests - but its reply carries Okappiki's own list, " +
            "Rotector's verdict and mococo's sightings together, which is what makes triage rules " +
            "R2 and R3 able to fire at all. Only Rotector's flag type can reach THREAT; the other " +
            "two services' sightings stop at CAUTION.",
        options: [
            { label: "Rotector - batched, fastest, one source", value: "rotector", default: true },
            { label: "Okappiki - three sources, one request per member", value: "okappiki" },
        ],
        restartNeeded: false,
        // Without this a backend switch keeps scanning the old service until a
        // reload: the live client holds its own limiter, caches and base URL.
        onChange: rebuildClient,
    },
    okappikiRateLimit: {
        type: OptionType.NUMBER,
        description:
            "Requests allowed per window against Okappiki. The default is the one rsb/config.py " +
            "measured against the live service. This is a separate key from the Rotector limit on " +
            "purpose: the two budgets are not comparable numbers, and pointing Rotector's " +
            "50-per-10s at Okappiki would be a fifty-fold overspend of somebody else's budget on " +
            "the first scan.",
        default: 6,
        restartNeeded: false,
        onChange: rebuildClient,
    },
    okappikiWindow: {
        type: OptionType.NUMBER,
        description: "Okappiki's rate limit window, in seconds.",
        default: 1,
        restartNeeded: false,
        onChange: rebuildClient,
    },
    okappikiReserve: {
        type: OptionType.NUMBER,
        description:
            "Request units held back from Okappiki's window as headroom. Zero by default: the " +
            "window is one second long, so a unit held back is a unit lost every second rather " +
            "than a cushion.",
        default: 0,
        restartNeeded: false,
        onChange: rebuildClient,
    },
    okappikiConcurrency: {
        type: OptionType.NUMBER,
        description:
            "Okappiki requests in flight at once. Capped at 5 and defaulting to 4, and that is not " +
            "a tuning knob: the Python project measured the curve and throughput plateaus at 4-5 " +
            "and falls at 6 while latency climbs. Every Okappiki request also costs Okappiki a " +
            "live Rotector lookup out of their budget, so spreading a scan over more connections " +
            "is the rate-limit circumvention Rotector's terms prohibit rather than a way to go " +
            "faster.",
        default: 4,
        restartNeeded: false,
        onChange: rebuildClient,
    },
    okappikiCacheTtl: {
        type: OptionType.NUMBER,
        description:
            "How long an Okappiki lookup is reused, in seconds. Held in memory only and clamped " +
            "under 24 hours, exactly as the Rotector cache is - an Okappiki answer contains a " +
            "Rotector answer, so Rotector's retention terms travel with it. The one thing that " +
            "moves that ceiling is `retain beyond terms` further down, and while it is on this " +
            "number is ignored in favour of `retention hours`.",
        default: 3600,
        restartNeeded: false,
        onChange: rebuildClient,
    },

    // ----------------------------------------------------------------------
    // [rotector] — API access. The defaults are the documented standard tier,
    // carried over from RotectorConfig in rsb/config.py.
    // ----------------------------------------------------------------------
    apiKey: {
        type: OptionType.STRING,
        description: "Optional Rotector API key. Raises your rate limit on one connection.",
        default: "",
        placeholder: "leave empty for the standard tier",
        restartNeeded: false,
        onChange: rebuildClient,
    },
    rateLimit: {
        type: OptionType.NUMBER,
        description:
            "Requests allowed per window. The documented standard tier is 50 per 10 seconds, " +
            "per IP; an API key raises it on that one connection.",
        default: 50,
        restartNeeded: false,
        onChange: rebuildClient,
    },
    window: {
        type: OptionType.NUMBER,
        description: "The rate limit window, in seconds. Rotector documents 10.",
        default: 10,
        restartNeeded: false,
        onChange: rebuildClient,
    },
    reserve: {
        type: OptionType.NUMBER,
        description:
            "Request units held back as headroom. The plugin cannot read Rotector's rate limit " +
            "headers from a browser, so this reserve is the only defence against another tab, " +
            "another client or clock skew spending the same budget. Raise it to be careful, " +
            "lower it for speed.",
        default: 5,
        restartNeeded: false,
        onChange: rebuildClient,
    },
    concurrency: {
        type: OptionType.NUMBER,
        description:
            "Requests in flight at once. This does not raise the rate limit and cannot be used " +
            "to get around it - it only decides how quickly each window is spent.",
        default: 3,
        restartNeeded: false,
        onChange: rebuildClient,
    },
    cacheTtl: {
        type: OptionType.NUMBER,
        description:
            "How long a lookup is reused, in seconds. Held in memory only, and clamped under 24 " +
            "hours: Rotector's terms forbid keeping their responses longer than that, and flag " +
            "statuses change. The one thing that moves that ceiling is `retain beyond terms` " +
            "further down, and while it is on this number is ignored in favour of `retention " +
            "hours`.",
        default: 3600,
        restartNeeded: false,
        onChange: rebuildClient,
    },

    // ----------------------------------------------------------------------
    // What gets looked up, and where a finding is drawn.
    // ----------------------------------------------------------------------
    autoLookup: {
        type: OptionType.SELECT,
        description:
            "Look members up as they appear. `visible` covers the member list and profiles; " +
            "adding messages also checks message authors. Lookups are batched 100 at a time, " +
            "so a member list costs one request rather than one per member. This is ignored " +
            "entirely while the backend is Okappiki, which has no batch endpoint: there the same " +
            "hundred members would be a hundred requests, four at a time, each one costing " +
            "Okappiki a live Rotector lookup - so scrolling a member list would quietly become a " +
            "scan. Under Okappiki, members are checked only when you ask for a scan or open a " +
            "report.",
        options: [
            { label: "Off - only what you ask for explicitly", value: "off" },
            { label: "Visible members and profiles", value: "visible", default: true },
            { label: "Visible members, profiles and message authors", value: "visible+messages" },
        ],
        restartNeeded: false,
    },
    markMemberList: {
        type: OptionType.BOOLEAN,
        description: "Draw a mark beside flagged members in the member list.",
        default: true,
        restartNeeded: false,
    },
    markMessages: {
        type: OptionType.BOOLEAN,
        description: "Draw a mark beside the author of a message from a flagged member.",
        default: true,
        restartNeeded: false,
    },
    markDms: {
        type: OptionType.BOOLEAN,
        description: "Draw a mark beside flagged people in the DM list.",
        default: true,
        restartNeeded: false,
    },
    profileSection: {
        type: OptionType.BOOLEAN,
        description:
            "Add a findings section to the user profile panel. It is absent entirely when there " +
            "is nothing to report, so a profile with no findings is not cluttered.",
        default: true,
        restartNeeded: false,
    },
    minVerdict: {
        type: OptionType.SELECT,
        description:
            "The lowest verdict that gets a mark. NO DETECTIONS and UNKNOWN are hidden by " +
            "default: in a real server almost every member has nothing against them, and " +
            "marking them all buries the few that matter. Nothing is discarded - hidden " +
            "members are still counted, and the scan window says how many.",
        options: [
            { label: "THREAT - Flagged or Confirmed only", value: "THREAT" },
            { label: "CAUTION and above", value: "CAUTION" },
            { label: "INFO and above (findings only)", value: "INFO", default: true },
            { label: "NO DETECTIONS and above", value: "NO_DETECTIONS" },
            { label: "Everything, including members nothing is known about", value: "UNKNOWN" },
        ],
        restartNeeded: false,
    },
    showPending: {
        type: OptionType.BOOLEAN,
        description:
            "Draw a hairline outline while a member is still being looked up. Off by default - " +
            "a member list full of empty outlines is noise, and the mark arrives on its own.",
        default: false,
        restartNeeded: false,
    },
    showTriage: {
        type: OptionType.BOOLEAN,
        description:
            "Show the triage call - cautious, or does the evidence support acting - alongside " +
            "the verdict, with the id of the rule that decided it.",
        default: true,
        restartNeeded: false,
    },
    evidenceCap: {
        type: OptionType.NUMBER,
        description:
            "How many evidence lines to show per reason in the report. Evidence is free text " +
            "from the service and is rendered as text, never as markup.",
        default: 6,
        restartNeeded: false,
    },
    skipBots: {
        type: OptionType.BOOLEAN,
        description: "Skip bot accounts when scanning. They have no Roblox connections.",
        default: true,
        restartNeeded: false,
    },
    maxMembers: {
        type: OptionType.NUMBER,
        description: "Cap how many members one scan looks up. 0 = no cap.",
        default: 0,
        restartNeeded: false,
    },
    motion: {
        type: OptionType.BOOLEAN,
        description:
            "Let the stripe fields drift. Off by default: nothing in this design moves position " +
            "or reflows, and adding drift to somebody else's app is not worth the distraction. " +
            "Ignored entirely when your system asks for reduced motion.",
        default: false,
        restartNeeded: false,
        onChange(value: any) {
            applyMotion(Boolean(value));
        },
    },

    // ----------------------------------------------------------------------
    // [moderation] — the triage policy. Both of these need more than one
    // database to have answered, so they fire only while the backend is
    // Okappiki, whose single response carries Okappiki's own list, Rotector's
    // verdict and mococo's sightings together. Under Rotector alone there is
    // no second source to corroborate anything and neither rule can match.
    // They are still shown under both, because a policy that silently
    // disappeared with the backend would be a policy nobody could audit.
    // ----------------------------------------------------------------------
    banOnCorroboratedSighting: {
        type: OptionType.BOOLEAN,
        description:
            "Rule R3: let Detective Okappiki and mococo, agreeing with each other and with no " +
            "Rotector flag, support a ban on their own. Off by default and deliberately so - " +
            "neither service publishes a claim that its sighting is safe to act on, so " +
            "switching this on is your policy call and not something either service told you " +
            "to make. Applies when the backend is Okappiki; under Rotector alone there is no " +
            "second source and the rule cannot fire.",
        default: false,
        restartNeeded: false,
    },
    minMococoScore: {
        type: OptionType.NUMBER,
        description:
            "R3's floor on mococo's score. TASE publishes no scale for that number and defines " +
            "it as how much a member interacted with the detected servers, not how severe they " +
            "are, so there is no correct value. 40 is what the sample records this was built " +
            "against carried; it is not a vendor threshold. Applies when the backend is Okappiki.",
        default: 40,
        restartNeeded: false,
    },

    // ----------------------------------------------------------------------
    // [scan] — the queue.
    //
    // Several scans can be queued at once. They do not add throughput: the
    // rate limiter is shared per backend because Rotector's terms forbid
    // getting around the window, so a second scan splits one budget rather
    // than opening a second one.
    // ----------------------------------------------------------------------
    maxConcurrentJobs: {
        type: OptionType.NUMBER,
        description:
            "How many queued scans may run at the same time. This splits one rate-limit budget; " +
            "it does not add one. Two scans on the same backend each go at roughly half speed, " +
            "which is the point: it lets a short source finish while a ten-thousand-member " +
            "server grinds, without either of them going faster than the published limit.",
        default: 2,
        restartNeeded: false,
    },
    onRescan: {
        type: OptionType.SELECT,
        description:
            "What starting a scan does when the same source is already queued or finished on the " +
            "same backend. Scanning one source on both backends is never a duplicate - the whole " +
            "point of two backends is two answers - so only the same pair counts.",
        options: [
            { label: "Ask which I meant", value: "ask", default: true },
            { label: "Reuse the results already held", value: "reuse" },
            { label: "Always scan again", value: "rescan" },
        ],
        restartNeeded: false,
    },

    // ----------------------------------------------------------------------
    // [gateway] — the real member scanner.
    //
    // Off by default, and the account-token half of it stays off until it is
    // chosen explicitly: automating a user account is against Discord's Terms
    // of Service and the risk is the user's account. A bot token does the same
    // job better and carries no such problem, which is why it is named first
    // everywhere this appears.
    // ----------------------------------------------------------------------
    gatewayEnabled: {
        type: OptionType.BOOLEAN,
        description:
            "Let a server scan open its own gateway connection and read the real member list " +
            "instead of the slice this client happens to hold. Off by default. Nothing connects " +
            "until you also choose which token below, and a bot token is the path to prefer.",
        default: false,
        restartNeeded: false,
    },
    gatewayToken: {
        type: OptionType.SELECT,
        description:
            "Which connection the member scanner uses. A bot application you own, with SERVER " +
            "MEMBERS INTENT switched on in the Developer Portal, can ask Discord for every member " +
            "at once - the complete roster, offline members included - and there is no Terms of " +
            "Service problem. Your own account can only scrape the member sidebar range by range, " +
            "and doing so is automation of a user account, which Discord's Terms prohibit with no " +
            "exception for safety tooling; accounts caught driving the API that way get disabled, " +
            "and that risk is entirely yours.",
        options: [
            { label: "Off - scan only what this client already holds", value: "off", default: true },
            { label: "A bot token (recommended; needs SERVER MEMBERS INTENT)", value: "bot" },
            { label: "My own account (against Discord's Terms of Service)", value: "client" },
        ],
        restartNeeded: false,
    },
    botToken: {
        type: OptionType.STRING,
        description:
            "The bot token used when the connection above is set to a bot. The bot must be in the " +
            "server you are scanning and must have SERVER MEMBERS INTENT switched on under Bot -> " +
            "Privileged Gateway Intents, or Discord closes the connection with code 4014 and no " +
            "members arrive. It is sent to Discord's gateway and nowhere else, and is never " +
            "written to a log or an error message.",
        default: "",
        placeholder: "leave empty unless you are using a bot",
        restartNeeded: false,
    },
    coverageTarget: {
        type: OptionType.NUMBER,
        description:
            "What percentage of a server's member count counts as a finished scrape. The last few " +
            "members can cost as much as the first ninety percent, and Discord's own member count " +
            "is itself approximate, so stopping just short is usually right. Whatever is reached, " +
            "the scan reports the real fraction rather than rounding it up.",
        default: 99.5,
        restartNeeded: false,
    },
    gatewayPrefixSweep: {
        type: OptionType.BOOLEAN,
        description:
            "After the sidebar has been read, ask Discord for members by name prefix to fill in " +
            "the rest. It is what closes the gap on a large server; it is also more requests, so " +
            "it can be switched off when the sidebar alone is enough.",
        default: true,
        restartNeeded: false,
    },

    // ----------------------------------------------------------------------
    // [moderation] — kicking and banning from the results.
    //
    // Two things here are enforced rather than left to the operator: every
    // audit-log reason carries the appeal route, because Rotector's terms
    // require that anyone actioned on their data can appeal; and only flag
    // types 1 and 2 are documented as safe to action, so anything else is
    // reported as a deliberate override rather than as routine.
    // ----------------------------------------------------------------------
    requireThreat: {
        type: OptionType.BOOLEAN,
        description:
            "Only offer a kick or a ban for THREAT findings - Rotector flag types Flagged (1) and " +
            "Confirmed (2), the only two documented as safe to action. Leaving it on does not " +
            "make anything impossible; it makes acting on anything else an explicit override, " +
            "named as one, in front of you.",
        default: true,
        restartNeeded: false,
    },
    allowCaution: {
        type: OptionType.BOOLEAN,
        description:
            "Whether CAUTION findings may be actioned at all. They are never routine: Rotector " +
            "asks that Provisional and Mixed findings be reviewed by a person, so every one of " +
            "them needs its own second confirmation.",
        default: true,
        restartNeeded: false,
    },
    defaultReason: {
        type: OptionType.STRING,
        description:
            "The audit-log reason. {reason} is replaced by the Rotector finding. The appeal link " +
            "is always appended if your text does not already carry one - that is not a default " +
            "you can turn off, because Rotector's terms require that an action taken on their " +
            "data attributes them or links rotector.com so the person can appeal.",
        default: "Flagged by Rotector: {reason}",
        restartNeeded: false,
    },
    deleteMessageSeconds: {
        type: OptionType.NUMBER,
        description:
            "How much of a banned member's recent message history Discord deletes, in seconds " +
            "(0 to 604800, a week). 0 keeps everything - which is usually right if anyone might " +
            "need to review what happened.",
        default: 0,
        restartNeeded: false,
    },
    notifyBeforeAction: {
        type: OptionType.BOOLEAN,
        description:
            "Try to DM someone before kicking or banning them, so they know where to appeal - a " +
            "banned account shares no server with you and cannot be reached afterwards. Bot " +
            "tokens only: a user account messaging strangers in bulk is exactly what Discord's " +
            "spam heuristics are built to catch, and the penalty lands on your account.",
        default: true,
        restartNeeded: false,
    },
    bulkDelay: {
        type: OptionType.NUMBER,
        description:
            "Seconds between members in a bulk action. Bulk kicks and bans are the fastest way to " +
            "trip Discord's abuse heuristics, and the pause costs nothing next to having the " +
            "account flagged.",
        default: 1,
        restartNeeded: false,
    },

    // ----------------------------------------------------------------------
    // [appeal] — an appeal route of your own, alongside Rotector's.
    //
    // This never replaces the rotector.com link, it is added to it. What it is
    // for is the case Rotector's own queue does not cover: their backlog, a
    // finding you applied judgement to, or simply wanting the person to be
    // able to reach you rather than only the database that flagged them.
    // ----------------------------------------------------------------------
    appealInvite: {
        type: OptionType.STRING,
        description: "An invite to a server where you handle appeals. Added to Rotector's link, never instead of it.",
        default: "",
        restartNeeded: false,
    },
    appealContact: {
        type: OptionType.STRING,
        description: "Free text: an email, a moderator handle, a form URL - however someone should reach you.",
        default: "",
        restartNeeded: false,
    },
    appealNote: {
        type: OptionType.STRING,
        description: "One extra sentence appended after the appeal routes, in your own words.",
        default: "",
        restartNeeded: false,
    },
    appealInReason: {
        type: OptionType.BOOLEAN,
        description:
            "Also mention your own appeal route in the audit-log reason, space permitting. " +
            "Rotector's link is there either way; this only decides whether yours joins it in a " +
            "field with 512 characters in it.",
        default: false,
        restartNeeded: false,
    },

    // ----------------------------------------------------------------------
    // [export] — files you keep, and therefore files you must delete.
    // ----------------------------------------------------------------------
    exportFormats: {
        type: OptionType.STRING,
        description:
            "Which formats the export menu ticks by default, comma separated: csv, txt, json, " +
            "html, png.",
        default: "csv,txt",
        restartNeeded: false,
    },
    exportScope: {
        type: OptionType.SELECT,
        description:
            "Whether an export carries the findings only or every member checked. Either way the " +
            "file states which it is and how many were left out - a list that quietly dropped rows " +
            "is a list nobody can check.",
        options: [
            { label: "Findings only, as shown", value: "filtered", default: true },
            { label: "Every member checked", value: "all" },
        ],
        restartNeeded: false,
    },
    exportSegmentSize: {
        type: OptionType.NUMBER,
        description:
            "Rows per CSV part. A long list is split into numbered parts so every one of them " +
            "still opens in a spreadsheet. 0 = never split.",
        default: 1000,
        restartNeeded: false,
    },
    exportFollowTheme: {
        type: OptionType.BOOLEAN,
        description:
            "Draw exports in this client's theme instead of the project's fixed export palette. " +
            "Verdict colours never follow a theme - a verdict hue that changes with your theme " +
            "stops being evidence.",
        default: false,
        restartNeeded: false,
    },

    // ----------------------------------------------------------------------
    // [purge] — deleting your own messages, and only ever your own.
    //
    // These are what the purge screen pre-fills, not limits enforced behind
    // your back: each one is editable before the preview runs.
    // ----------------------------------------------------------------------
    purgeDelay: {
        type: OptionType.NUMBER,
        description:
            "Seconds between deletions when purging your own messages. Discord's per-channel " +
            "delete limit is strict; lowering this mostly buys 429s and a slower purge.",
        default: 1,
        restartNeeded: false,
    },
    purgeMaxMessages: {
        type: OptionType.NUMBER,
        description: "Cap on how many of your messages one purge collects. 0 = no cap.",
        default: 0,
        restartNeeded: false,
    },
    purgeMaxAgeDays: {
        type: OptionType.NUMBER,
        description: "Only look back this far when purging. 0 = all of it.",
        default: 0,
        restartNeeded: false,
    },

    // ----------------------------------------------------------------------
    // [vibe] — music for a scan that takes half an hour.
    // ----------------------------------------------------------------------
    vibeEnabled: {
        type: OptionType.BOOLEAN,
        description:
            "Show a player in the scan window. Off by default: this is a music player in " +
            "somebody's chat client. Nothing is bundled and nothing is written to disk - tracks " +
            "are streamed from the repository below, one at a time, and released from memory when " +
            "they end.",
        default: false,
        restartNeeded: false,
        onChange(value: any) {
            if (!value) stopVibe();
        },
    },
    vibeRepo: {
        type: OptionType.STRING,
        description:
            "The GitHub repository holding the track library. Its index.json is the library - " +
            "there is no other list, and nothing is discovered by crawling.",
        default: "mireyacs/openlofi",
        restartNeeded: false,
    },
    vibeBranch: {
        type: OptionType.STRING,
        description: "Which branch of that repository to read.",
        default: "main",
        restartNeeded: false,
    },
    vibeVolume: {
        type: OptionType.NUMBER,
        description: "Playback volume, 0 to 100.",
        default: 60,
        restartNeeded: false,
        onChange(value: any) {
            setVibeVolume(Number(value));
        },
    },
    vibeShuffle: {
        type: OptionType.BOOLEAN,
        description: "Play the library in a random order rather than the order the manifest lists.",
        default: true,
        restartNeeded: false,
    },
    vibeFollowScans: {
        type: OptionType.BOOLEAN,
        description:
            "Start playing when a scan starts and stop when the queue empties. Off by default: " +
            "music that starts on its own is a surprise, and a scan is not always something you " +
            "are sitting in front of.",
        default: false,
        restartNeeded: false,
    },

    // ----------------------------------------------------------------------
    // [scan] — how long findings are kept.
    //
    // Every other retention control in this file is a cap on *how much*. These
    // two are the only ones that decide *how long*, and they are the only place
    // in the plugin where the 24-hour ceiling moves at all.
    // ----------------------------------------------------------------------
    retainBeyondTerms: {
        type: OptionType.BOOLEAN,
        description:
            "Keep looked-up findings past the 24-hour window Rotector's Terms of Use allow, " +
            "instead of letting the caches expire them. Off, and it should stay off unless you " +
            "have decided otherwise on purpose. Rotector's Terms of Use #1 forbid retaining their " +
            "responses longer than 24 hours, so switching this on is a choice to hold their data " +
            "outside the terms you agreed to. It is also a fairness question rather than only a " +
            "licensing one: flag types change, appeals succeed, and an account that cleared its " +
            "violations yesterday still reads as flagged in a cache that never expires. Anything " +
            "served from beyond the window is labelled stale, with its age, everywhere it is " +
            "shown or written - in the mark's tooltip, in the report, in the ledger, in the scan " +
            "history and in every exported file - and that labelling is not optional.",
        default: false,
        restartNeeded: false,
        // The ceiling on reads and sweeps is re-read from these values every
        // time, so it moves the moment this is flipped; the live client holds
        // its own clamped TTL, so it has to be rebuilt.
        onChange: rebuildClient,
    },
    retentionHours: {
        type: OptionType.NUMBER,
        description:
            "How long to keep findings when the setting above is on, in hours. Ignored while it " +
            "is off, when the 23-hour ceiling applies instead. Between 1 and 8760 - a year is " +
            "where both clients clamp, because past that it is a memory leak as well as a " +
            "licensing problem and a year-old flag status is not evidence about anybody. The " +
            "default of 168 is one week. While the ceiling is lifted this also becomes the cache " +
            "lifetime, because a one-hour cache would otherwise throw the data away long before " +
            "the window you asked for was up.",
        default: RETENTION_HOURS_DEFAULT,
        restartNeeded: false,
        onChange: rebuildClient,
    },

    // ----------------------------------------------------------------------
    // [history] — finished scans, written down.
    //
    // This is the one part of the plugin that puts findings on disk, and by
    // default it is stricter than Rotector's terms require rather than looser:
    // the terms forbid retaining their responses beyond 24 hours, and every
    // record is deleted 23 hours after its scan started — enforced on write,
    // enforced again on every read, and swept on a timer so the database itself
    // stops holding it.
    //
    // Neither setting below can raise that. `historyEnabled` decides whether a
    // record is written at all and `historyLimit` decides how many are kept,
    // which are both questions about *how much*, never about *how long*. The
    // only setting that answers the second question is `retainBeyondTerms`
    // above, and when it is on these records live to the same ceiling as the
    // in-memory cache — with every finding past 24 hours labelled stale, with
    // its age, wherever it is listed.
    // ----------------------------------------------------------------------
    historyEnabled: {
        type: OptionType.BOOLEAN,
        description:
            "Keep finished scans so they survive a restart. Records are dropped after 24 hours " +
            "unless you have switched `retain beyond terms` on: Rotector's terms forbid keeping " +
            "their data longer, and a flag status changes - yesterday's finding can be wrong " +
            "today. Switching this off stops new scans being written down; it does not delete " +
            "what is already stored, which is what the purge below is for.",
        default: true,
        restartNeeded: false,
    },
    historyLimit: {
        type: OptionType.NUMBER,
        description:
            "How many past scans are kept. Older scans are deleted first, and the count is " +
            "clamped between 1 and 200. This is a cap on how many, never on how long: how long " +
            "is decided by `retain beyond terms` and nothing else, and with it off every record " +
            "is deleted 23 hours after its scan started whatever this is set to.",
        default: 25,
        restartNeeded: false,
    },
    historyPurge: {
        type: OptionType.COMPONENT,
        description: "Delete every stored scan from this computer now.",
        // The same control appears in the status panel further down, beside the
        // in-memory cache's purge, because the two belong together there. It is
        // repeated here because this is where the decision to store scans is
        // made, and a settings screen that offers the switch without the eraser
        // makes somebody hunt for it.
        component: HistoryPurgePanel,
    },

    // ----------------------------------------------------------------------
    // Status, and one piece of state that is not a preference.
    // ----------------------------------------------------------------------
    status: {
        type: OptionType.COMPONENT,
        description: "Host client, connectivity, and what is currently held — in memory and on disk.",
        component: StatusPanel,
    },
    /**
     * Whether the first-run notice has been shown. OptionType.CUSTOM renders
     * nothing in the settings screen, which is what this wants: it is a fact
     * about the install, not a preference anyone should be flipping.
     */
    noticeSeen: {
        type: OptionType.CUSTOM,
        description: "Internal: whether the first-run notice has been shown.",
        default: false,
    },
});

// --------------------------------------------------------------------------
// reading settings, safely
// --------------------------------------------------------------------------

/** The live settings object, or an empty one when the plugin is not registered yet. */
function stored(): Record<string, any> {
    try {
        return (settings.store as unknown as Record<string, any>) ?? {};
    } catch {
        return {};
    }
}

/** A finite number inside `[min, max]`, or the fallback. Never NaN, never negative by surprise. */
function num(value: unknown, fallback: number, min: number, max: number): number {
    const parsed = typeof value === "number" ? value : Number(value);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.min(max, Math.max(min, parsed));
}

function bool(value: unknown, fallback: boolean): boolean {
    return typeof value === "boolean" ? value : fallback;
}

/** verdict name -> verdict, so the stored value stays readable in settings.json */
const VERDICT_BY_NAME: Record<string, Verdict> = {};
for (const verdict of ALL_VERDICTS) VERDICT_BY_NAME[VERDICT_NAMES[verdict]] = verdict;

/**
 * The lowest verdict that earns a mark.
 *
 * Defaults to INFO, which is what "findings only" means: NO_DETECTIONS and
 * UNKNOWN draw nothing at all. An unrecognised stored value falls back to the
 * same default rather than to "show everything" — a settings file written by an
 * older version should not quietly turn the member list into a wall of marks.
 */
export function verdictFloor(): Verdict {
    const raw = stored().minVerdict;
    if (typeof raw === "number" && VERDICT_NAMES[raw as Verdict] != null) return raw as Verdict;
    const named = VERDICT_BY_NAME[String(raw)];
    return named ?? Verdict.INFO;
}

/** The operator's own bar, handed to `triage()`. */
export function policyFromSettings(): TriagePolicy {
    const s = stored();
    return {
        banOnCorroboratedSighting: bool(
            s.banOnCorroboratedSighting,
            DEFAULT_POLICY.banOnCorroboratedSighting
        ),
        minMococoScore: num(s.minMococoScore, DEFAULT_POLICY.minMococoScore, 0, 1_000_000),
    };
}

/** Which service a scan asks. Defaults to Rotector; an unrecognised value never scans. */
export function backendName(): BackendName {
    return backendFrom(stored());
}

/**
 * How the API client should be built.
 *
 * The per-backend mapping lives in api/backend.ts rather than here, and only
 * there: the two backends read different keys because their budgets are not
 * comparable numbers, and one pair of fields for both would mean switching
 * backend silently applied the other one's rate to it. Rotector's 50-per-10s
 * pointed at Okappiki would be a fifty-fold overspend of somebody else's budget
 * on the first scan, and it would happen before anyone saw a single result.
 *
 * The bounds `backendOptionsFrom` applies are guardrails rather than opinions:
 * a rate limit of 0 would stall every scan forever and a concurrency of 200
 * would spend a whole window in one burst, and both are one mistyped digit away
 * in a text field.
 */
export function clientOptions(): ClientOptions {
    const s = stored();
    return backendOptionsFrom(backendFrom(s), s);
}

/**
 * Whether members may be looked up simply because they scrolled past.
 *
 * `autoLookup` is the user's answer to that question and is honoured under
 * Rotector, where the queue coalesces a member list into one request per
 * hundred members. Under Okappiki there is no batch endpoint at all: the same
 * hundred rows are a hundred requests, four at a time, and every one of them
 * costs Okappiki a live Rotector lookup out of *their* budget. Scrolling a
 * member list is not a decision to scan a server, so it must not become one —
 * under Okappiki the lazy queue is off and a check has to be asked for.
 */
export function lazyLookupAllowed(): boolean {
    if (backendName() === "okappiki") return false;
    return String(stored().autoLookup ?? "visible") !== "off";
}

// --------------------------------------------------------------------------
// retention
//
// The arithmetic lives in api/backend.ts, which has no settings import and can
// therefore be called from store.ts and features/history.ts without either of
// them importing the module that imports them. These are the bound versions:
// they read the live store, they are cheap, and every caller is expected to
// call them fresh rather than hold the answer — flipping `retainBeyondTerms`
// has to take effect without a reload.
// --------------------------------------------------------------------------

/** Whether the operator has chosen to keep findings past the terms' window. */
export function retainBeyondTerms(): boolean {
    return retainBeyondTermsFrom(stored());
}

/** How long findings are kept once that ceiling is lifted, in hours. */
export function retentionHours(): number {
    return retentionHoursFrom(stored());
}

/**
 * The age past which a finding may not be held at all, in milliseconds.
 *
 * 23 hours by default; whatever `retentionHours` says once the ceiling has been
 * lifted, up to the year both clients clamp to. store.ts and features/history.ts
 * enforce this on every read, every write and every sweep.
 */
export function retentionCeilingMs(): number {
    const ceiling = retentionCeilingMsFrom(stored());
    // A sibling module that failed to evaluate hands back `undefined` here
    // rather than failing to build, and `undefined` would mean *no* ceiling.
    return Number.isFinite(ceiling) && ceiling > 0 ? ceiling : 23 * 3600 * 1000;
}

/**
 * The sentence a surface shows about how long findings are being kept.
 *
 * One string, so the status panel, the scan footer, the history list and the
 * exports all say the same thing — and so that the lifted case cannot be
 * described anywhere as though it were the default one.
 */
export function retentionNotice(): string {
    if (!retainBeyondTerms()) {
        return (
            "Findings are held in memory only and dropped within 24 hours - Rotector's terms " +
            "forbid keeping their data longer, and flag statuses change."
        );
    }
    const hours = retentionHours();
    const span = hours >= 48
        ? `${Math.round(hours / 24)} days`
        : `${hours} hour${hours === 1 ? "" : "s"}`;
    return (
        `Findings are being kept for ${span} by choice, past the 24-hour window Rotector's ` +
        "Terms of Use allow. Anything older than 24 hours is labelled stale with its age: flag " +
        "types change and appeals succeed, so re-check before acting on one."
    );
}

/** How many members one scan may look up. 0 means no cap. */
export function memberCap(): number {
    return num(stored().maxMembers, 0, 0, 10_000_000);
}

/** How many evidence lines the report shows per reason. */
export function evidenceCap(): number {
    return num(stored().evidenceCap, 6, 0, 100);
}

/**
 * Put the motion gate where style.css looks for it.
 *
 * One class on the document element, exactly as the project page does it, so
 * the whole animation vocabulary can be switched off in one place — and is,
 * by default.
 */
export function applyMotion(enabled?: boolean): void {
    try {
        const on = typeof enabled === "boolean" ? enabled : bool(stored().motion, false);
        document.documentElement.classList.toggle(MOTION_CLASS, on);
    } catch {
        // No document (or a hostile one). Motion is decoration; losing it is fine.
    }
}
