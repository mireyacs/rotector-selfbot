/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The settings screen's status panel: which client is hosting the plugin,
 * whether the renderer can actually reach the backend the operator selected,
 * and what is currently held — in memory, and on this computer.
 *
 * It lives in its own file because settings.ts is a `.ts` module and this is
 * JSX, and because every other module imports settings.ts — keeping the render
 * code out of it keeps that import cheap.
 *
 * Connectivity is decided by a real request rather than by asking the host
 * whether the domain is allowed. On Equicord desktop the CSP allowlist carries
 * a wildcard that `isDomainAllowed` does not report, so the probe would say
 * "blocked" about a connection that works perfectly. See api/host.ts.
 *
 * `checkHost()` resolves its own target from the active backend, so the row it
 * fills is labelled from the active backend too. A row that said "rotector api"
 * while reporting a probe of okappiki.com would be the panel telling the
 * operator a fact about a service they are not using.
 *
 * There are two stores to report on rather than one. The lookup cache in
 * store.ts is memory only and goes when Discord does; the scan history in
 * features/history.ts is a database on this computer that deliberately survives
 * a restart. Both are swept against one ceiling, and the panel says which is
 * which — a page that still claimed "nothing reaches your disk" while a database
 * sat beside it would be the plugin's own copy misleading the reader.
 *
 * That ceiling is 23 hours until `retainBeyondTerms` moves it, which is why it
 * has a row of its own here rather than a sentence at the bottom. This is the
 * screen somebody opens to find out what the plugin is doing with Rotector's
 * data, so it has to name the ceiling actually in force, say plainly when that
 * is a choice somebody made rather than the default, and count how much of what
 * is held is already a claim about yesterday.
 */

import { useEffect, useRef, useState } from "@webpack/common";

import { backendFrom, backendLabel } from "../api/backend";
import { checkHost, cspSupported, hostClientName, requestHostPermission } from "../api/host";
import type { HostReport, HostState } from "../api/host";
import { historyRetentionNotice, scanHistory, useHistoryStats, useHistoryStatus } from "../features/history";
import { retainBeyondTerms, retentionHours, retentionNotice, settings } from "../settings";
import { reportStore, useStoreStats } from "../store";
import { Body, Btn, cl, KeyVal, Micro, Node } from "./primitives";

/** state -> the word shown beside the backend's connectivity row */
const STATE_LABEL: Record<string, string> = {
    ok: "reachable",
    "needs-permission": "blocked by this client",
    blocked: "unreachable",
    unknown: "unknown",
};

const EMPTY_STATS = { cached: 0, pending: 0, findings: 0, stale: 0 };
const EMPTY_HISTORY = { scans: 0, members: 0, findings: 0, oldest: undefined as number | undefined, stale: 0 };

/**
 * The loader hands every module a CommonJS `require`. It is used below for one
 * thing only: reaching the history modal from a click handler without importing
 * it at the top of this file. settings.ts imports this component at module
 * scope, so a static import here would pull the whole modal — and everything it
 * imports — into the settings module's own evaluation, which is a load-order
 * question nobody should have to think about to open a settings page. A require
 * inside a handler runs long after every module is loaded.
 */
declare const require: (specifier: string) => any;

function attempt<T>(fn: () => T, fallback: T): T {
    try {
        const value = fn();
        return value === undefined ? fallback : value;
    } catch {
        return fallback;
    }
}

/**
 * Open the scan history, or do nothing if the modal could not be loaded.
 *
 * `closePluginSettings` is handed to every COMPONENT setting by the host, and
 * it is called first when there is one: the history is a full modal, and
 * stacking it on top of the settings modal leaves the reader two Escape presses
 * from where they started.
 */
function openPastScans(props?: any): void {
    attempt(() => props?.closePluginSettings?.(), undefined);
    attempt(() => {
        require("./HistoryModal")?.openHistory?.();
    }, undefined);
}

/** `95` -> `"1m 35s"`. The shape rsb/eta.py's format_duration produces. */
function formatDuration(seconds: number | null | undefined): string {
    if (seconds == null || !isFinite(seconds)) return "?";
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

function count(n: number): string {
    return attempt(() => Number(n || 0).toLocaleString(), String(n || 0));
}

/**
 * Which retention ceiling is in force, in the words this panel prints.
 *
 * Read fresh on every render rather than captured: `retainBeyondTerms` takes
 * effect on the running client, and this is the screen somebody opens to find
 * out what the plugin is currently doing with Rotector's data. `lifted` is the
 * whole question — 23 hours is the terms' answer, and anything else is a
 * decision somebody made.
 */
function retentionState(): { lifted: boolean; ceiling: string; notice: string; } {
    const lifted = attempt(() => retainBeyondTerms(), false);
    const hours = Math.max(1, Math.round(attempt(() => retentionHours(), 168)));
    const span = hours >= 48 ? `${hours} hours (${Math.round(hours / 24)} days)` : `${hours} hours`;
    return {
        lifted,
        ceiling: lifted
            ? `${span} — past the 24 the terms allow`
            : "23 hours — Rotector's Terms of Use",
        notice: attempt(
            () => retentionNotice(),
            "Findings are held in memory only and dropped within 24 hours - Rotector's terms " +
            "forbid keeping their data longer, and flag statuses change."
        ),
    };
}

/**
 * Purge the scan database.
 *
 * The wording pairs with "Purge cached findings" above it on purpose: they are
 * the two places a finding can be, and a reader who has found one should not
 * have to guess that the other exists. The behaviour differs in one step, and
 * the reason is the difference between the two stores — the memory cache refills
 * itself from the next lookup, while a deleted record is gone and the scan that
 * produced it would have to be run again, spending the backend's budget a second
 * time. That is worth one click to confirm.
 */
export function HistoryPurge(): JSX.Element {
    const [confirming, setConfirming] = useState(false);
    const stats = useHistoryStats() ?? EMPTY_HISTORY;

    if (!confirming) {
        return (
            <Btn quiet disabled={!stats.scans} onClick={() => setConfirming(true)}>
                Purge stored scans
            </Btn>
        );
    }

    return (
        <>
            <Btn
                onClick={() => {
                    setConfirming(false);
                    attempt(() => { void scanHistory.purge(); }, undefined);
                }}
            >
                {`Delete all ${count(stats.scans)} — cannot be undone`}
            </Btn>
            <Btn quiet onClick={() => setConfirming(false)}>Keep them</Btn>
        </>
    );
}

/**
 * The `historyPurge` setting's own row.
 *
 * A COMPONENT setting renders inline in Discord's settings list, so this carries
 * the plugin's own ground (`rsb--field`) the way every other surface the plugin
 * owns does — white ink on a transparent root would be white on white the moment
 * somebody runs the light theme.
 */
export function HistoryPurgePanel(props?: any): JSX.Element {
    const stats = useHistoryStats() ?? EMPTY_HISTORY;
    const now = Date.now();

    return (
        <div className={`rsb rsb--field ${cl("status")}`}>
            <div className={cl("status__actions")}>
                <Btn quiet onClick={() => openPastScans(props)}>Past scans</Btn>
                <HistoryPurge />
            </div>
            <Micro>
                {stats.scans
                    ? `${count(stats.scans)} stored scan(s), ${count(stats.members)} member(s) answered, `
                      + `${count(stats.findings)} finding(s); oldest started `
                      + `${formatDuration(Math.max(0, now - Number(stats.oldest ?? now)) / 1000)} ago.`
                    : "No scans are stored."}
                {stats.stale ? ` ${count(stats.stale)} of the stored finding(s) are older than 24 hours and are marked stale.` : ""}
            </Micro>
            <Micro>{historyRetentionNotice()}</Micro>
        </div>
    );
}

/**
 * What each backend means for the person reading this panel.
 *
 * This is the sentence that used to claim Okappiki was unreachable. It was
 * wrong: both `vencord_full_check` and `vencord_check_flag` answer with
 * `access-control-allow-origin: *`, the earlier probe had simply asked for an
 * invalid action, whose error path omits the headers. What is left to say is
 * what choosing each backend actually costs.
 */
const BACKEND_NOTE: Record<string, string> = {
    rotector:
        "Scans ask Rotector directly, a hundred ids per request. Okappiki is the other " +
        "option in the settings above: it answers with Okappiki, Rotector and mococo " +
        "findings together, one member per request.",
    okappiki:
        "Scans ask Okappiki, whose response carries Okappiki, Rotector and mococo findings " +
        "together — the two extra sources are sightings and read as CAUTION, only Rotector's " +
        "flag types reach THREAT. There is no batch endpoint, so this is one request per " +
        "member, and every one of them costs Okappiki a live Rotector lookup out of their " +
        "budget. That is why it runs four at a time and must not be made to go faster.",
};

/**
 * Which service this panel is about.
 *
 * Read defensively and late: `settings.store` throws before the plugin is
 * registered, and the status panel is exactly the screen someone opens to fix a
 * broken configuration, so it may never be the thing that crashes.
 */
function activeBackend(): string {
    try {
        return backendFrom((settings as any)?.store);
    } catch {
        return "rotector";
    }
}

export function StatusPanel(props?: any): JSX.Element {
    const [report, setReport] = useState<HostReport | null>(null);
    const [checking, setChecking] = useState(false);
    const [permission, setPermission] = useState("");
    const [asking, setAsking] = useState(false);
    const stats = useStoreStats() ?? EMPTY_STATS;
    const history = useHistoryStats() ?? EMPTY_HISTORY;
    const historyStatus = useHistoryStatus() ?? { on: true, reason: "", hydrated: false };

    // A settings panel is closed by clicking away from it, which happens while
    // a probe is very much still in flight. Writing state into an unmounted
    // component is a React warning at best and a leak at worst.
    const alive = useRef(true);

    // Switching backend while this panel is open starts a second probe of a
    // different service, and the two can land out of order. Only the newest one
    // may write, or the row can end up reporting the backend the operator just
    // switched away from — the exact confusion this panel is meant to end.
    const probeId = useRef(0);

    function probe() {
        const id = ++probeId.current;
        const current = () => alive.current && probeId.current === id;

        setChecking(true);
        Promise.resolve()
            .then(() => checkHost())
            .then(result => {
                if (current()) setReport(result);
            })
            .catch(error => {
                if (current()) {
                    setReport({
                        state: "unknown" as HostState,
                        detail: `The connectivity check itself failed: ${String(error?.message ?? error)}`,
                    });
                }
            })
            .then(() => {
                if (current()) setChecking(false);
            });
    }

    useEffect(() => {
        alive.current = true;
        return () => {
            alive.current = false;
        };
    }, []);

    function grant() {
        setAsking(true);
        Promise.resolve()
            .then(() => requestHostPermission())
            .then(outcome => {
                if (alive.current) setPermission(String(outcome));
            })
            .catch(error => {
                if (alive.current) setPermission(String(error?.message ?? error));
            })
            .then(() => {
                if (alive.current) setAsking(false);
            });
    }

    const state: HostState = report?.state ?? "unknown";
    const needsPermission = state === "needs-permission";
    const backend = activeBackend();
    const label = backendLabel(backend);
    const retention = retentionState();

    // Probe on mount and again whenever the operator changes backend: the
    // answer is about one host, and `checkHost()` resolves that host from this
    // same setting.
    useEffect(() => {
        probe();
    }, [backend]);

    // The root carries `rsb--field` because this panel is a surface the plugin
    // owns end to end. Bare `.rsb` is deliberately transparent, so a decorator
    // does not paint a black box into someone else's member list; a panel left
    // on Discord's grey settings background would be reading as a tinted panel,
    // which is exactly the tone device the design bans. It paints the two-value
    // ground instead.
    return (
        <div className={`rsb rsb--field ${cl("status")}`}>
            <Node label="HOST" chip>
                <KeyVal k="client" v={hostClientName()} />
                <KeyVal k="backend" v={label} />
                <KeyVal
                    k={`${label.toLowerCase()} api`}
                    v={checking ? "checking…" : STATE_LABEL[state] ?? state}
                />
                {report != null && !checking ? <Body measure>{report.detail}</Body> : null}
                {report == null && !checking ? (
                    <Body measure>No check has run yet.</Body>
                ) : null}

                <div className={cl("status__actions")}>
                    <Btn quiet disabled={checking} onClick={probe}>
                        {checking ? "Checking" : "Re-check"}
                    </Btn>
                    {needsPermission && cspSupported() ? (
                        <Btn disabled={asking} onClick={grant}>
                            {asking ? "Asking" : "Grant host permission"}
                        </Btn>
                    ) : null}
                </div>

                {needsPermission && cspSupported() ? (
                    <Micro>
                        Your client will show an operating-system dialog, and the change only
                        takes effect after Discord is fully closed and reopened — not a reload.
                    </Micro>
                ) : null}
                {permission ? <Micro>{permission}</Micro> : null}
            </Node>

            {/*
              * How long findings are being kept, as a row of its own.
              *
              * This is the one screen where the consequence of lifting the
              * ceiling has to be unmissable, so it is a labelled node above
              * both stores rather than a footnote under them: everything below
              * — the memory cache and the database — is held to whichever
              * number this node names. When it is the lifted one it reads as
              * the decision it was, with the terms it stands outside of and
              * the way back, because a choice nobody can find again is a
              * choice nobody can undo.
              */}
            <Node label="RETENTION" chip>
                <KeyVal k="ceiling" v={retention.ceiling} />
                <KeyVal
                    k="past the terms"
                    v={retention.lifted ? "yes — by your choice" : "no"}
                />
                <KeyVal k="stale findings held" v={count(Number(stats.stale ?? 0) + Number(history.stale ?? 0))} />
                <Body measure>{retention.notice}</Body>
                {retention.lifted && (
                    <>
                        <Body measure>
                            You switched "retain beyond terms" on. Rotector's Terms of Use #1 forbid
                            keeping their responses beyond 24 hours, so this run is outside them, and
                            it stays that way until you switch it off — at which point everything past
                            23 hours is dropped on the next read. It is also a fairness question:
                            appeals succeed and flag types change, so a finding held past the window
                            can name somebody for something that is no longer true of them.
                        </Body>
                        <Micro>
                            Everything answered more than 24 hours ago is labelled stale, with its age,
                            wherever it is shown or written — the mark's tooltip, the report, the
                            ledger, the scan history and every exported file. That labelling does not
                            follow this setting and cannot be switched off.
                        </Micro>
                    </>
                )}
            </Node>

            <Node label="HELD IN MEMORY" count={stats.cached} chip>
                <KeyVal k="members cached" v={String(stats.cached)} />
                <KeyVal k="lookups in flight" v={String(stats.pending)} />
                <KeyVal k="with findings" v={String(stats.findings)} />
                {/* Always printed once the ceiling has moved, including at
                    zero: "none of what is held is stale yet" is an answer. */}
                {(retention.lifted || stats.stale > 0) && (
                    <KeyVal k="past 24 hours" v={count(stats.stale)} />
                )}

                <div className={cl("status__actions")}>
                    <Btn quiet onClick={() => reportStore.purge()}>
                        Purge cached findings
                    </Btn>
                </div>

                <Micro>
                    {retention.lifted
                        ? "This cache is memory only: it is swept on a timer, held to the ceiling "
                          + "above rather than the 23 hours the terms imply, and it goes entirely when "
                          + "Discord closes or the plugin is disabled. Nothing here reaches your "
                          + "settings file."
                        : "This cache is memory only: it is swept on a timer, hard-clamped to 23 hours "
                          + "whatever cache lifetime you set, and it goes entirely when Discord closes "
                          + "or the plugin is disabled. Nothing here reaches your settings file."}
                </Micro>
            </Node>

            {/* The second place a finding can be, and the reason the sentence
                above says "this cache" rather than "the plugin". A finished scan
                is written down so it survives a restart; the 23-hour ceiling is
                the same one, enforced on write, on every read, and by a sweep
                that deletes from the database and not only from memory. */}
            <Node label="STORED ON THIS COMPUTER" count={history.scans} chip>
                <KeyVal k="scans stored" v={count(history.scans)} />
                <KeyVal k="members answered" v={count(history.members)} />
                <KeyVal k="with findings" v={count(history.findings)} />
                <KeyVal
                    k="oldest record"
                    v={history.oldest
                        ? `started ${formatDuration(Math.max(0, Date.now() - Number(history.oldest)) / 1000)} ago`
                        : "none"}
                />
                {(retention.lifted || Number(history.stale ?? 0) > 0) && (
                    <KeyVal k="past 24 hours" v={count(Number(history.stale ?? 0))} />
                )}

                <div className={cl("status__actions")}>
                    <Btn quiet onClick={() => openPastScans(props)}>Past scans</Btn>
                    <HistoryPurge />
                </div>

                <Micro>{historyRetentionNotice()}</Micro>
                {!historyStatus.on && historyStatus.reason ? (
                    <Micro>{historyStatus.reason}</Micro>
                ) : null}
            </Node>

            <Micro>{BACKEND_NOTE[backend] ?? BACKEND_NOTE.rotector}</Micro>
        </div>
    );
}
