/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The export dialog: pick what leaves the client, and in what shape.
 *
 * The terminal app's export screen writes a folder and then reports what it
 * wrote. A renderer cannot write a folder — it hands files to the download
 * manager one at a time — so this modal does the reporting *before* the files
 * exist: the exact list of names, the number of CSV segments, and the scope, so
 * that "Download" is never a surprise.
 *
 * The two rules from rsb/export.py travel with every file rather than living
 * here: the attribution line and the 24-hour expiry are written into the CSV
 * header comments, the TXT header, the JSON envelope, the HTML footer and the
 * PNG footer. This modal repeats them on screen because the person clicking the
 * button is the person who has to delete the files afterwards — a browser
 * cannot do it for them the way `purge_expired` does for the terminal app.
 */

import ErrorBoundary from "@components/ErrorBoundary";
import { Logger } from "@utils/Logger";
import { Modal, openModal, ScrollerThin, useEffect, useMemo, useRef, useState } from "@webpack/common";

import { MemberReport, reportVerdict } from "../api/types";
import { ATTRIBUTION, Verdict, verdictLabel } from "../api/verdict";
import {
    APPEAL_URL,
    asRows,
    DEFAULT_SEGMENT_SIZE,
    DownloadFile,
    downloadAll,
    ExportFormat,
    EXPORT_FORMATS,
    ExportOptions,
    ExportRow,
    exportCsv,
    exportJson,
    exportReadme,
    exportStem,
    exportTxt,
    expiryText,
    formatMime,
    RETENTION_HOURS,
    resolvePalette,
    retainedBeyondTerms,
    retentionWindowHours,
    segmentName,
    timestamp,
} from "../features/export";
import { renderHtml } from "../features/htmlreport";
import { pngAvailable, renderPngParts, unavailableReason } from "../features/pngreport";
import { settings, verdictFloor as configuredFloor } from "../settings";
import { Body, Btn, cl, Label, Micro, Node } from "./primitives";

const logger = new Logger("RotectorScan");

/** what each format actually is, in one sentence, at the point of choosing it */
const FORMAT_NOTE: Record<ExportFormat, string> = {
    csv: "One row per member, for a spreadsheet. Split into numbered parts when the list is long, so every part opens on its own.",
    txt: "One readable block per member. This is the one to paste into a ticket.",
    json: "The same columns, machine-readable, with the retention notice in the envelope.",
    html: "One self-contained page holding both a sortable table and a card per member, with a filter box.",
    png: "The table as a picture, for a channel where a screenshot is what people look at. Paged, because one tall image is unreadable.",
};

function attempt<T>(fn: () => T, fallback: T): T {
    try {
        const value = fn();
        return value === undefined || value === null ? fallback : value;
    } catch {
        return fallback;
    }
}

/** Read one setting. `settings.store` throws before the plugin is registered. */
function option<T>(key: string, fallback: T): T {
    return attempt(() => {
        const store = (settings as any)?.store;
        if (store == null) return fallback;
        const value = store[key];
        return value === undefined || value === null ? fallback : (value as T);
    }, fallback);
}

/** The lowest verdict that counts as a finding. Defaults to INFO, as the TUI does. */
function findingFloor(): Verdict {
    return attempt(() => {
        const floor = typeof configuredFloor === "function" ? configuredFloor() : undefined;
        return typeof floor === "number" ? (floor as Verdict) : Verdict.INFO;
    }, Verdict.INFO);
}

/** `"csv, txt"` -> the formats that exist. An unrecognised name is dropped, not guessed at. */
function configuredFormats(): ExportFormat[] {
    const raw = String(option("exportFormats", "csv,txt"));
    const wanted = raw.split(/[,\s]+/).map(f => f.trim().toLowerCase()).filter(Boolean);
    const out = EXPORT_FORMATS.filter(f => wanted.includes(f));
    return out.length ? out : ["csv"];
}

function count(n: number): string {
    return attempt(() => n.toLocaleString(), String(n));
}

function isFinding(report: MemberReport, floor: Verdict): boolean {
    // A lookup that did not answer stays in every scope: a database that was
    // silent is a fact about the evidence, and dropping it from an export would
    // let an outage read as a clean list.
    if (report?.error) return true;
    return attempt(() => reportVerdict(report), Verdict.UNKNOWN) >= floor;
}

// --------------------------------------------------------------------------

export function openExportMenu(
    reports: MemberReport[],
    label: string,
    extra?: { guildId?: string; note?: string; scanned?: number; }
): void {
    openModal(props => (
        <ExportMenu
            modalProps={props}
            reports={Array.isArray(reports) ? reports : []}
            label={String(label || "Rotector scan")}
            extra={extra}
        />
    ));
}

function ExportMenu({ modalProps, reports, label, extra }: {
    modalProps: any;
    reports: MemberReport[];
    label: string;
    extra?: { guildId?: string; note?: string; scanned?: number; };
}) {
    const [formats, setFormats] = useState<ExportFormat[]>(() => configuredFormats());
    const [findingsOnly, setFindingsOnly] = useState(() => option("exportScope", "filtered") !== "all");
    const [busy, setBusy] = useState(false);
    const [done, setDone] = useState<string | undefined>(undefined);
    const [error, setError] = useState<string | undefined>(undefined);

    // One stamp for the whole export, so every file in it agrees about when it
    // was made and therefore about when it expires.
    const stampRef = useRef<string>(timestamp());
    const stamp = stampRef.current;

    /**
     * An export runs for seconds — a segmented PNG yields a frame per page and
     * the download loop waits 220ms per file — and this modal is dismissable
     * throughout, so every state write after an await has to ask whether the
     * dialog is still there. The sibling modals (ScanModal, PurgeModal) use the
     * same flag for the same reason.
     */
    const aliveRef = useRef(true);
    useEffect(() => {
        aliveRef.current = true;
        return () => {
            aliveRef.current = false;
        };
    }, []);

    const floor = findingFloor();
    const segmentSize = Math.max(0, Math.trunc(Number(option("exportSegmentSize", DEFAULT_SEGMENT_SIZE))) || 0);
    const followTheme = Boolean(option("exportFollowTheme", false));
    const canDrawPng = attempt(() => pngAvailable(), false);

    const selected = useMemo<MemberReport[]>(
        () => (findingsOnly ? reports.filter(report => isFinding(report, floor)) : [...reports]),
        [reports, findingsOnly, floor]
    );
    const hidden = Math.max(0, reports.length - selected.length);

    const rows = useMemo<ExportRow[]>(() => attempt(() => asRows(selected), [] as ExportRow[]), [selected]);
    const stem = useMemo(() => attempt(() => exportStem(label, stamp), `rotector-scan-${stamp}`), [label, stamp]);

    const csvParts = segmentSize > 0 && rows.length > segmentSize ? Math.ceil(rows.length / segmentSize) : 1;

    const options: ExportOptions = {
        label,
        scope: findingsOnly ? "filtered (findings only)" : "all scanned members",
        stamp,
        segmentSize,
        guildId: extra?.guildId,
        note: extra?.note,
        scanned: typeof extra?.scanned === "number" ? extra.scanned : reports.length,
    };

    /**
     * What these files will say about their own retention.
     *
     * The plugin cannot sweep a downloads folder, so the statement inside each
     * file is the whole of the enforcement — and the dialog that produces them
     * has to say the same thing. With `retainBeyondTerms` on, the files are
     * written claiming the longer window, so a dialog still promising a 24-hour
     * expiry would be contradicting the README it is about to generate.
     */
    const retained = attempt(() => retainedBeyondTerms(options), false);
    const retentionHoursHere = attempt(() => retentionWindowHours(options), RETENTION_HOURS);

    /** The names this export will produce, listed before anything is generated. */
    const names = useMemo(() => {
        const out: string[] = [];
        for (const format of formats) {
            if (format === "csv") {
                for (let i = 1; i <= csvParts; i++) out.push(segmentName(stem, i, csvParts, "csv"));
            } else if (format === "png") {
                // the page count depends on the drawing, so it is stated as a
                // range rather than promised exactly
                out.push(`${stem}.png${rows.length > 60 ? "  (paged)" : ""}`);
            } else {
                out.push(`${stem}.${format}`);
            }
        }
        if (out.length) out.push(`${stem}.README.txt`);
        return out;
    }, [formats, csvParts, stem, rows.length]);

    function toggle(format: ExportFormat) {
        setDone(undefined);
        setError(undefined);
        setFormats(current => (current.includes(format)
            ? current.filter(f => f !== format)
            : EXPORT_FORMATS.filter(f => f === format || current.includes(f))));
    }

    async function run() {
        if (busy || !formats.length) return;
        setBusy(true);
        setDone(undefined);
        setError(undefined);
        try {
            // Resolved once, so the HTML and the PNG in one export cannot end up
            // drawn from two different readings of the theme.
            const palette = attempt(() => resolvePalette(), null);
            const opts: ExportOptions = { ...options, palette };
            const files: DownloadFile[] = [];

            if (formats.includes("csv")) {
                const parts = exportCsv(rows, opts);
                parts.forEach((text, index) => files.push({
                    name: segmentName(stem, index + 1, parts.length, "csv"),
                    data: text,
                    mime: formatMime("csv"),
                }));
            }
            if (formats.includes("txt")) {
                files.push({ name: `${stem}.txt`, data: exportTxt(rows, opts), mime: formatMime("txt") });
            }
            if (formats.includes("json")) {
                files.push({ name: `${stem}.json`, data: exportJson(rows, opts), mime: formatMime("json") });
            }
            if (formats.includes("html")) {
                files.push({ name: `${stem}.html`, data: renderHtml(rows, opts), mime: formatMime("html") });
            }
            if (formats.includes("png") && canDrawPng) {
                const images = await renderPngParts(rows, opts);
                images.forEach((blob, index) => files.push({
                    name: segmentName(stem, index + 1, images.length, "png"),
                    data: blob,
                    mime: formatMime("png"),
                }));
            }

            // The README goes last so it can name every file that came before
            // it, which is the list the reader has to delete within 24 hours.
            files.push({
                name: `${stem}.README.txt`,
                data: exportReadme(rows, { ...opts, files: files.map(f => f.name) }),
                mime: formatMime("txt"),
            });

            // The delivery itself is deliberately not cut short when the modal
            // goes away: the files are already generated, and stopping halfway
            // would hand over a partial set without the README that names what
            // has to be deleted. Only the reporting back into the tree stops.
            const delivered = await downloadAll(files);
            if (!aliveRef.current) return;
            // The deadline these files actually carry. With retention past the
            // terms switched on the files say they are kept longer, and a toast
            // still promising tomorrow would contradict the README inside them.
            const retained = attempt(() => retainedBeyondTerms(opts), false);
            const gone = attempt(() => expiryText(stamp, retentionWindowHours(opts)), expiryText(stamp));
            setDone(retained
                ? `${count(delivered)} file(s) sent to your downloads. They state that they are kept until ${gone}, past the 24 hours Rotector's terms allow — deleting them is yours to do.`
                : `${count(delivered)} file(s) sent to your downloads. Delete them by ${gone}.`);
        } catch (err: any) {
            logger.error("export failed", err);
            if (!aliveRef.current) return;
            setError(String(err?.message ?? err));
        } finally {
            if (aliveRef.current) setBusy(false);
        }
    }

    return (
        <Modal
            {...modalProps}
            size="lg"
            title={<span className={`rsb ${cl("modal-title")}`}>Export findings</span>}
        >
            <ErrorBoundary>
                {/* `rsb--field` paints the two-value ground; a transparent
                    plugin surface puts white ink onto Discord's grey. */}
                <div className={`rsb rsb--field ${cl("scan")}`}>
                    <Node label="Scope" count={rows.length}>
                        <div className={cl("stack")}>
                            <div className={cl("actions")}>
                                <Btn quiet={!findingsOnly} onClick={() => { setFindingsOnly(true); setDone(undefined); }}>
                                    Findings
                                </Btn>
                                <Btn quiet={findingsOnly} onClick={() => { setFindingsOnly(false); setDone(undefined); }}>
                                    Everything
                                </Btn>
                            </div>
                            {/* always printed, hidden count included, even at zero */}
                            <Micro>
                                filter: {findingsOnly ? "findings" : "all"} ({count(hidden)} hidden) — exporting{" "}
                                {count(rows.length)} of {count(reports.length)} member(s)
                            </Micro>
                            <Body measure>
                                A member with no finding is not a member who has been cleared. Whichever scope you
                                pick, every file states which one it was.
                            </Body>
                            {extra?.note && <Body measure>{extra.note}</Body>}
                        </div>
                    </Node>

                    <Node label="Formats" count={formats.length}>
                        <div className={cl("stack")}>
                            <div className={cl("actions")}>
                                {EXPORT_FORMATS.map(format => {
                                    const on = formats.includes(format);
                                    const blocked = format === "png" && !canDrawPng;
                                    return (
                                        <button
                                            key={format}
                                            type="button"
                                            aria-pressed={on}
                                            disabled={blocked}
                                            aria-disabled={blocked ? "true" : undefined}
                                            className={`rsb ${cl("btn", !on && "btn--quiet")}`}
                                            onClick={() => {
                                                if (blocked) return;
                                                toggle(format);
                                            }}
                                        >
                                            {format.toUpperCase()}
                                        </button>
                                    );
                                })}
                            </div>
                            {formats.map(format => (
                                <Micro key={format}>{format.toUpperCase()} — {FORMAT_NOTE[format]}</Micro>
                            ))}
                            {!formats.length && (
                                <Body>Nothing selected. Pick at least one format above.</Body>
                            )}
                            {!canDrawPng && (
                                <Micro>{unavailableReason()}</Micro>
                            )}
                            {formats.includes("csv") && csvParts > 1 && (
                                <Micro>
                                    The CSV is split into {count(csvParts)} parts of up to {count(segmentSize)} rows,
                                    each with its own header, so a spreadsheet can open every one of them.
                                </Micro>
                            )}
                            <Micro>
                                {followTheme
                                    ? "Chrome follows this client's theme (exportFollowTheme is on). Verdict colours never do — a verdict hue that follows a theme stops being evidence."
                                    : "Drawn in the project's fixed export palette, so an export from the plugin and one from the terminal app are the same document. Turn on exportFollowTheme to make the chrome follow this client instead."}
                            </Micro>
                        </div>
                    </Node>

                    <Node label="Files" count={names.length}>
                        <div className={cl("stack")}>
                            {names.length === 0
                                ? <Body>No files: choose a format above.</Body>
                                : (
                                    <ScrollerThin className={cl("scroll")}>
                                        <div className={cl("stack")}>
                                            {names.map(name => <Micro key={name}>{name}</Micro>)}
                                        </div>
                                    </ScrollerThin>
                                )}
                            <Body measure>
                                {retained
                                    ? `Every file carries the attribution line and says it is being kept for `
                                      + `${retentionHoursHere} hours after ${stamp} by your choice, past the `
                                      + `${RETENTION_HOURS} hours Rotector's terms allow — and any finding in it `
                                      + "already older than a day is marked stale with its age. Nothing here can "
                                      + "delete them for you; that is the part you have to do."
                                    : `Every file carries the attribution line and says it expires ${RETENTION_HOURS} hours `
                                      + `after ${stamp}. Nothing here can delete them for you — that is the part you have to do.`}
                            </Body>
                        </div>
                    </Node>

                    {error && (
                        <div className={cl("error")}>
                            <Label>Export failed</Label>
                            <Body measure>{error}</Body>
                            <Micro>
                                Nothing was written. Try one format at a time; a very large PNG is the usual cause,
                                and the CSV of the same scan will always be smaller.
                            </Micro>
                        </div>
                    )}

                    <div className={cl("foot")}>
                        <div className={cl("actions")}>
                            <Btn disabled={busy || !formats.length || rows.length === 0} onClick={() => void run()}>
                                {busy ? "Preparing…" : "Download"}
                            </Btn>
                            <Btn quiet disabled={busy} onClick={() => modalProps?.onClose?.()}>Close</Btn>
                        </div>
                        {rows.length === 0 && (
                            <Micro>
                                Nothing to export under this scope. Switch to "Everything" to include members with no
                                finding — which is not the same as members who are clear.
                            </Micro>
                        )}
                        {done && <Micro>{done}</Micro>}
                        <Micro>{ATTRIBUTION}</Micro>
                        <Micro>Anyone listed in these files can appeal at {APPEAL_URL.replace("https://", "")}</Micro>
                        <Micro>
                            {retained
                                ? `These files are being kept for ${retentionHoursHere} hours by your choice; `
                                  + `Rotector's terms allow ${RETENTION_HOURS}, and flag statuses change well inside `
                                  + "that. Deleting them is yours to do. "
                                : `Delete the files within ${RETENTION_HOURS} hours — Rotector's terms forbid keeping their `
                                  + "data longer, and flag statuses change. "}
                            The verdict words in them mean what they mean:{" "}
                            {verdictLabel(Verdict.NO_DETECTIONS)} is not a clean bill of health.
                        </Micro>
                    </div>
                </div>
            </ErrorBoundary>
        </Modal>
    );
}
