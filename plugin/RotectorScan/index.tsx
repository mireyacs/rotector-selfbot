/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The wiring, and nothing else. Every surface here is a declarative plugin
 * field, because this plugin is loaded by the DynamicPluginLoader and is never
 * built: `patches` only apply at Discord's startup, and calling
 * `addMemberListDecorator` by hand would leave the decorator behind when the
 * plugin is disabled. PluginManager wires and unwires each field below, so
 * `stop()` only has to deal with what this plugin owns itself.
 *
 * Nothing here touches the network. The first lookup is issued by a surface
 * that a member actually appears on, or by a scan somebody started.
 */

import {
    ApplicationCommandInputType,
    ApplicationCommandOptionType,
    findOption,
    sendBotMessage,
} from "@api/Commands";
import { findGroupChildrenByChildId } from "@api/ContextMenu";
import type { NavContextMenuPatchCallback } from "@api/ContextMenu";
import { popNotice, showNotice } from "@api/Notices";
import { Logger } from "@utils/Logger";
import definePlugin from "@utils/types";
import type { Channel, Guild, User } from "@vencord/discord-types";
import { Menu, SettingsRouter } from "@webpack/common";

import { openHistory } from "./components/HistoryModal";
import { Mark } from "./components/Mark";
import { openReport, ProfileReportSection } from "./components/ReportModal";
import { openScan, openScanQueue, recordFinishedJobs, resetHistoryRecorder } from "./components/ScanModal";
import { scanHistory } from "./features/history";
import { jobQueue } from "./features/jobs";
import { existingDmChannelId, openPurgeModal } from "./features/purge";
import { noteScanActivity, vibePlayer } from "./features/vibe";
import type { MemberSource } from "./members";
import { harvestMemberListUpdate } from "./members";
import { applyMotion, settings } from "./settings";
import { reportStore } from "./store";
import styles from "./style.css?managed";

const PLUGIN_NAME = "RotectorScan";
// Logger's default badge is black on white, which is this project's own
// inversion device. A verdict hue here would be colour spent on chrome.
const logger = new Logger(PLUGIN_NAME);

/**
 * Discord's channel types, declared locally.
 *
 * `@vencord/discord-types/enums` does not resolve in a dynamic plugin and the
 * bare `@vencord/discord-types` is an empty stub, so an imported enum would be
 * `undefined` at runtime. Only the two values this file compares against are
 * declared; guessing at the rest would be inventing an API.
 */
const enum ChannelType {
    DM = 1,
    GROUP_DM = 3,
}

/** Run `fn`, or hand back the fallback. A context menu that throws takes the whole menu with it. */
function attempt<T>(fn: () => T, fallback: T): T {
    try {
        const value = fn();
        return value === undefined ? fallback : value;
    } catch (error) {
        return fallback;
    }
}

/** A settings read that can never be the reason a decorator fails to render. */
function flag(key: string, fallback: boolean): boolean {
    try {
        const value = (settings.store as unknown as Record<string, any>)[key];
        return typeof value === "boolean" ? value : fallback;
    } catch {
        return fallback;
    }
}

function autoLookupMode(): string {
    try {
        const value = (settings.store as unknown as Record<string, any>).autoLookup;
        return typeof value === "string" ? value : "visible";
    } catch {
        return "visible";
    }
}

// --------------------------------------------------------------------------
// the one-time notice
// --------------------------------------------------------------------------

/**
 * Open this plugin's own settings modal.
 *
 * `@components/settings` does not resolve in a dynamic plugin, so the modal is
 * reached through the `Vencord` global instead of imported. Every step is
 * optional-chained: an older build without it falls back to the plugins tab,
 * and a build without that does nothing rather than throwing inside a notice
 * button.
 */
function openOwnSettings(): void {
    try {
        const host = (window as any).Vencord;
        const plugin = host?.Plugins?.plugins?.[PLUGIN_NAME];
        const openPluginModal = host?.Components?.openPluginModal;
        if (plugin && typeof openPluginModal === "function") {
            openPluginModal(plugin);
            return;
        }
    } catch (error) {
        logger.debug("Could not open the plugin modal directly", error);
    }

    try {
        SettingsRouter?.openUserSettings?.("equicord_plugins_panel");
    } catch (error) {
        logger.debug("Could not open the plugins tab either", error);
    }
}

const FIRST_RUN_NOTICE =
    "RotectorScan is on. A verdict is not a background check: NO DETECTIONS means Rotector " +
    "has not flagged that account yet, not that it is safe, and only THREAT - flag types " +
    "Flagged and Confirmed - is documented as safe to act on. Findings are kept for at most " +
    "24 hours - in memory while you use the client, and in a small database on this computer " +
    "for scans that have finished.";

let noticeShown = false;
/**
 * The handle for the deferred first-run notice, so `stop()` can cancel it.
 *
 * Without this, a plugin enabled and then disabled inside ten seconds still
 * fires the timer: it marks the notice seen — burning the one chance the user
 * had to read it — and pushes a notice into the client on behalf of a plugin
 * that is no longer running.
 */
let noticeTimer: ReturnType<typeof setTimeout> | null = null;

/**
 * The handle for the deferred second attempt at the motion gate.
 *
 * `applyMotion()` reads `settings.store`, which throws until the loader has
 * finished registering the plugin — and `start()` can run inside that window.
 * It swallows the failure, correctly, because motion is decoration; but the
 * consequence is that the html class never lands and motion looks dead until
 * somebody toggles the setting. So it is asked once more on the next tick, by
 * which time registration has certainly completed. The handle is kept for the
 * same reason the notice's is: a plugin disabled in that gap must not go on to
 * put a class on the document element on behalf of something that is not
 * running.
 */
let motionTimer: ReturnType<typeof setTimeout> | null = null;

/**
 * The queue subscriber that drives `vibeFollowScans`, so `stop()` can drop it.
 *
 * `jobQueue.reset()` clears the jobs but not the listener set, and a listener
 * left behind by a disabled plugin would still be told about a queue the next
 * enable creates.
 */
let unsubscribeJobs: (() => void) | null = null;

/**
 * Show the first-run notice once, ever.
 *
 * Deferred rather than shown from `start()`: the Notices module is resolved
 * from webpack asynchronously, and showing a notice before it arrives throws.
 * Both callers are guarded by the same two flags, so whichever gets there first
 * wins and the other is a no-op.
 */
function maybeShowFirstRunNotice(): void {
    if (noticeShown) return;

    let seen = false;
    try {
        seen = Boolean((settings.store as unknown as Record<string, any>).noticeSeen);
    } catch {
        // Settings are not ready, which means the plugin is not ready either.
        return;
    }
    if (seen) {
        noticeShown = true;
        return;
    }

    noticeShown = true;
    try {
        showNotice(FIRST_RUN_NOTICE, "Open settings", () => {
            popNotice();
            openOwnSettings();
        });
        // Marked seen only once it has actually been shown. Setting the flag
        // first meant a `showNotice` that threw — the case this try exists for —
        // permanently retired a notice nobody ever read.
        (settings.store as unknown as Record<string, any>).noticeSeen = true;
    } catch (error) {
        logger.warn("Could not show the first-run notice", error);
    }
}

// --------------------------------------------------------------------------
// context menus
// --------------------------------------------------------------------------

const userContextPatch: NavContextMenuPatchCallback = (children, props: any) => {
    const user: User | undefined = props?.user;
    if (!user?.id) return;

    children.push(
        <Menu.MenuItem
            id="rsb-check-user"
            label="Rotector: check user"
            action={() => openReport(user.id, props?.guildId ?? props?.guild?.id)}
        />
    );

    // The purge item appears only when a DM with this person already exists.
    // `existingDmChannelId` is a lookup, never an open: substituting
    // `openPrivateChannel` to make the item always appear would mean opening a
    // conversation in order to delete one that never happened, which is exactly
    // what rsb/discord/http.py refuses to do. No DM, no item, correctly.
    const dm = attempt(() => existingDmChannelId(user.id), undefined);
    if (dm) {
        children.push(
            <Menu.MenuItem
                id="rsb-purge-dm"
                // The possessive is the scope statement in miniature, and it is
                // the first thing anyone reads about the feature: this deletes
                // your own messages and Discord offers no way to delete anyone
                // else's from a DM.
                label="Rotector: purge my messages"
                action={() => openPurgeModal(dm)}
            />
        );
    }
};

const guildContextPatch: NavContextMenuPatchCallback = (children, props: any) => {
    const guild: Guild | undefined = props?.guild;
    if (!guild?.id) return;

    const source: MemberSource = {
        kind: "guild",
        id: guild.id,
        guildId: guild.id,
        label: guild.name ?? "this server",
    };

    // Lands beside Discord's own privacy group where there is one; a guild the
    // menu built without that group still gets the items, at the end.
    const group = findGroupChildrenByChildId("privacy", children) ?? children;
    group.push(
        <Menu.MenuItem
            id="rsb-scan-guild"
            label="Rotector: scan server"
            action={() => openScan(source)}
        />
    );
    group.push(
        <Menu.MenuItem
            id="rsb-scan-queue"
            label="Rotector: scan queue"
            action={() => openScanQueue()}
        />
    );
    // Past scans are not per-server — the record names its own source — but this
    // is the menu somebody is in when they wonder whether they already scanned
    // this place, so it is the menu that answers.
    group.push(
        <Menu.MenuItem
            id="rsb-scan-history"
            label="Rotector: past scans"
            action={() => openHistory()}
        />
    );
};

const channelContextPatch: NavContextMenuPatchCallback = (children, props: any) => {
    const channel: Channel | undefined = props?.channel;
    if (!channel?.id) return;

    const isGroupDm = channel.type === ChannelType.GROUP_DM;

    // A 1:1 DM has exactly one other recipient and no member list worth the
    // word — the user context menu already covers that person — but purging
    // your own messages is about the conversation rather than the roster, so
    // that item belongs here for every channel kind.
    if (channel.type !== ChannelType.DM) {
        const source: MemberSource = {
            kind: isGroupDm ? "gdm" : "channel",
            id: channel.id,
            guildId: (channel as any).guild_id ?? props?.guild?.id,
            label: channel.name ? `#${channel.name}` : "this conversation",
        };

        children.push(
            <Menu.MenuItem
                id="rsb-scan-members"
                label="Rotector: scan members"
                action={() => openScan(source)}
            />
        );
    }

    children.push(
        <Menu.MenuItem
            id="rsb-purge-channel"
            label="Rotector: purge my messages"
            action={() => openPurgeModal(channel.id)}
        />
    );
};

/**
 * The same purge entry, from a message.
 *
 * The modal is channel-scoped rather than message-scoped — it plans, shows what
 * would go, and only then deletes — so a right-click on a message is just
 * another way to name the conversation.
 */
const messageContextPatch: NavContextMenuPatchCallback = (children, props: any) => {
    const channelId = props?.channel?.id ?? props?.message?.channel_id;
    if (!channelId) return;

    children.push(
        <Menu.MenuItem
            id="rsb-purge-message-channel"
            label="Rotector: purge my messages"
            action={() => openPurgeModal(String(channelId))}
        />
    );
};

// --------------------------------------------------------------------------
// the plugin
// --------------------------------------------------------------------------

export default definePlugin({
    name: PLUGIN_NAME,
    description:
        "Checks Discord members against the Rotector database and marks the ones with findings, " +
        "with the evidence behind each one.",
    authors: [{ name: "azula", id: 0n }],
    tags: ["Utility", "Privacy", "Servers"],
    dependencies: ["MemberListDecoratorsAPI", "MessageDecorationsAPI", "ProfileSectionsAPI"],
    settings,
    managedStyle: styles,

    renderMemberListDecorator(props: any) {
        const user: User | undefined = props?.user;
        if (!user?.id) return null;

        const inDmList = props?.type === "dm";
        if (inDmList ? !flag("markDms", true) : !flag("markMemberList", true)) return null;

        return <Mark userId={user.id} where={inDmList ? "dm" : "member"} />;
    },

    renderMessageDecoration(props: any) {
        if (!flag("markMessages", true)) return null;
        const authorId = props?.message?.author?.id;
        if (!authorId) return null;
        return <Mark userId={authorId} where="message" />;
    },

    renderProfileSection: {
        render(props: any) {
            if (!flag("profileSection", true)) return null;
            const userId = props?.userId;
            if (!userId) return null;
            return <ProfileReportSection userId={userId} isSideBar={Boolean(props?.isSideBar)} />;
        },
        priority: 0,
    },

    contextMenus: {
        "user-context": userContextPatch,
        "user-profile-actions": userContextPatch,
        "user-profile-overflow-menu": userContextPatch,
        "guild-context": guildContextPatch,
        "guild-header-popout": guildContextPatch,
        "gdm-context": channelContextPatch,
        "channel-context": channelContextPatch,
        "message": messageContextPatch,
    },

    commands: [
        {
            name: "rotector",
            description: "Check a member against Rotector, or open the scan window",
            // BUILT_IN: the command does its own work and sends nothing to the
            // channel. Everything it replies with is a Clyde message only you
            // can see.
            inputType: ApplicationCommandInputType.BUILT_IN,
            options: [
                {
                    name: "check",
                    description: "Check one member against Rotector",
                    type: ApplicationCommandOptionType.SUB_COMMAND,
                    options: [
                        {
                            name: "user",
                            description: "The member to check",
                            type: ApplicationCommandOptionType.USER,
                            required: true,
                        },
                    ],
                },
                {
                    name: "scan",
                    description: "Open the scan window for this server or conversation",
                    type: ApplicationCommandOptionType.SUB_COMMAND,
                    options: [],
                },
                {
                    name: "queue",
                    description: "Show the scan queue and what each scan is spending",
                    type: ApplicationCommandOptionType.SUB_COMMAND,
                    options: [],
                },
                {
                    name: "history",
                    description: "Show past scans, kept for 23 hours and then deleted",
                    type: ApplicationCommandOptionType.SUB_COMMAND,
                    options: [],
                },
                {
                    name: "export",
                    description: "Open the scan window, where a finished scan can be exported",
                    type: ApplicationCommandOptionType.SUB_COMMAND,
                    options: [],
                },
                {
                    name: "purge",
                    description: "Delete your own messages from this conversation, after a preview",
                    type: ApplicationCommandOptionType.SUB_COMMAND,
                    options: [],
                },
            ],
            execute(args: any[], ctx: any) {
                const channelId = ctx?.channel?.id;
                const sub = args?.[0]?.name;
                const subArgs = args?.[0]?.options ?? [];

                if (sub === "check") {
                    const userId = findOption<string>(subArgs, "user");
                    if (!userId) {
                        if (channelId) {
                            sendBotMessage(channelId, {
                                content: "Rotector: no member was given to check.",
                            });
                        }
                        return;
                    }
                    openReport(String(userId), ctx?.guild?.id);
                    return;
                }

                if (sub === "scan") {
                    openScan(scanPresetFor(ctx));
                    return;
                }

                if (sub === "queue") {
                    openScanQueue();
                    return;
                }

                if (sub === "history") {
                    openHistory();
                    return;
                }

                if (sub === "export") {
                    // Deliberately not an export: there is nothing to export
                    // until a scan has run, and a command that silently wrote
                    // an empty file would be worse than one that opens the
                    // window where the results actually are.
                    openScan(scanPresetFor(ctx));
                    return;
                }

                if (sub === "purge") {
                    if (!channelId) return;
                    openPurgeModal(String(channelId));
                    return;
                }

                if (channelId) {
                    sendBotMessage(channelId, {
                        content:
                            "Rotector: use `/rotector check`, `/rotector scan`, `/rotector queue`, "
                            + "`/rotector history`, `/rotector export` or `/rotector purge`.",
                    });
                }
            },
        },
    ],

    flux: {
        /**
         * The gateway is up, so the Notices module certainly is too. Note that
         * a plugin enabled while Discord is already running never sees this —
         * `start()` schedules the same call for that case.
         */
        CONNECTION_OPEN() {
            maybeShowFirstRunNotice();
        },

        /**
         * Free coverage. Discord sends this as the member sidebar scrolls, and
         * every id in it is a member the client now knows about, so queueing
         * them costs one batched request per hundred members rather than one
         * per member.
         *
         * Note for anyone looking for the neighbouring events: `GUILD_MEMBERS_CHUNK`
         * is not a real Flux event (`GUILD_MEMBERS_CHUNK_BATCH` is) and neither
         * is `PRESENCE_UPDATE` (`PRESENCE_UPDATES` is). Subscribing to a name
         * that does not exist fails silently, which is the worst way to be
         * wrong.
         */
        GUILD_MEMBER_LIST_UPDATE(payload: any) {
            try {
                const ids = harvestMemberListUpdate(payload);
                if (ids.length) reportStore.requestMany(ids);
            } catch (error) {
                logger.debug("Could not harvest a member list update", error);
            }
        },

        MESSAGE_CREATE({ optimistic, type, message }: any) {
            try {
                if (optimistic || type !== "MESSAGE_CREATE" || message?.state === "SENDING") return;
                if (autoLookupMode() !== "visible+messages") return;

                const author = message?.author;
                if (!author?.id || author.bot) return;

                reportStore.request(author.id);
            } catch (error) {
                logger.debug("Could not queue a message author", error);
            }
        },
    },

    start() {
        applyMotion();
        // ...and again once the current tick is over, because the first call can
        // land before `settings.store` is readable and silently do nothing.
        if (motionTimer !== null) clearTimeout(motionTimer);
        motionTimer = setTimeout(() => {
            motionTimer = null;
            applyMotion();
        }, 0);

        // Start from a clean sheet of "what has already been written down".
        // `stop()` empties the queue, so any job id remembered from a previous
        // enable refers to nothing — and if one were reused, a real scan would
        // be silently skipped by the recorder's own duplicate check.
        try {
            resetHistoryRecorder();
        } catch (error) {
            logger.debug("Could not reset the history recorder", error);
        }

        // `vibeFollowScans`. The queue is the only thing that knows a scan has
        // started, and features/vibe.ts deliberately does not import it — the
        // player has to stay loadable with no scan machinery behind it — so the
        // one subscriber that joins them lives here. `noteScanActivity` is
        // edge-triggered and does nothing unless both vibe mode and
        // "follow scans" are switched on.
        try {
            unsubscribeJobs?.();
            unsubscribeJobs = jobQueue.subscribe(() => {
                // Write down whatever has just finished. This is the recorder
                // that matters: a ten-thousand-member scan ends half an hour
                // after the window was closed, and a history that only held the
                // scans somebody sat and watched would be a history of nothing.
                // Writes are deduplicated and land on a record id derived from
                // the job id, so the scan modal's own save and this one are the
                // same record rather than two.
                try {
                    recordFinishedJobs();
                } catch (error) {
                    logger.debug("Could not write a finished scan to the history", error);
                }

                try {
                    const stats = jobQueue.stats();
                    noteScanActivity(stats.running + stats.queued > 0);
                } catch (error) {
                    logger.debug("Could not tell the player about the queue", error);
                }
            });
        } catch (error) {
            logger.debug("Could not subscribe to the scan queue", error);
        }

        // Deferred, not immediate: the Notices module is resolved from webpack
        // asynchronously and showing a notice before it lands throws. Ten
        // seconds is long past both that and Discord's own startup noise. The
        // handle is kept because `stop()` has to be able to cancel it.
        if (noticeTimer !== null) clearTimeout(noticeTimer);
        noticeTimer = setTimeout(() => {
            noticeTimer = null;
            maybeShowFirstRunNotice();
        }, 10_000);
    },

    stop() {
        // The one timer this plugin owns itself. A plugin turned off within ten
        // seconds must not go on to mark the notice seen and push it anyway.
        if (noticeTimer !== null) {
            clearTimeout(noticeTimer);
            noticeTimer = null;
        }

        // The other one. A gate that landed after the plugin was switched off
        // would leave `html.rsb-motion` behind with nothing left to read it.
        if (motionTimer !== null) {
            clearTimeout(motionTimer);
            motionTimer = null;
        }

        try {
            unsubscribeJobs?.();
        } catch (error) {
            logger.debug("The queue subscriber did not unsubscribe cleanly", error);
        }
        unsubscribeJobs = null;

        // Stops every scan and forgets the queue. Without this a scan keeps
        // running behind a disabled plugin — which means it keeps spending the
        // rate limit, with no surface left to see it on or stop it from — and
        // the jobs go on holding member ids and counts derived from Rotector's
        // answers, which is retention with nothing left to serve.
        try {
            jobQueue.reset();
        } catch (error) {
            logger.warn("The scan queue did not reset cleanly", error);
        }

        // Aborts in-flight lookups and clears the store's timers. The
        // declarative fields above unwire themselves, and the managed style is
        // disabled by PluginManager, so there is nothing else to undo.
        try {
            reportStore.stop();
        } catch (error) {
            logger.warn("The report store did not stop cleanly", error);
        }

        // Clears the history's in-memory mirror and its sweep timer. It
        // deliberately does *not* delete the database: unlike the report cache,
        // those records are meant to survive a restart, and switching the plugin
        // off for a minute is not a request to delete them. The 23-hour ceiling
        // still governs what sits on disk — the next hydrate drops whatever aged
        // out meanwhile — and a user who wants it gone now has the purge button
        // in the settings panel and in the history window.
        try {
            scanHistory.stop();
        } catch (error) {
            logger.warn("The scan history did not stop cleanly", error);
        }

        // The recorder's bookkeeping is about jobs the queue has just thrown
        // away, so it goes with them.
        try {
            resetHistoryRecorder();
        } catch (error) {
            logger.debug("The history recorder did not reset cleanly", error);
        }

        // Disabling the plugin must not leave music playing and a blob URL
        // alive with no surface left to stop it from.
        try {
            vibePlayer.stop();
        } catch (error) {
            logger.warn("The player did not stop cleanly", error);
        }

        applyMotion(false);
    },
});

/** The source a bare `/rotector scan` means, read off where it was typed. */
function scanPresetFor(ctx: any): MemberSource | undefined {
    const guild = ctx?.guild;
    if (guild?.id) {
        return {
            kind: "guild",
            id: guild.id,
            guildId: guild.id,
            label: guild.name ?? "this server",
        };
    }

    const channel = ctx?.channel;
    if (channel?.id && channel.type === ChannelType.GROUP_DM) {
        return {
            kind: "gdm",
            id: channel.id,
            label: channel.name || "this group DM",
        };
    }

    // A 1:1 DM or somewhere with no roster of its own: let the scan window ask.
    return undefined;
}
