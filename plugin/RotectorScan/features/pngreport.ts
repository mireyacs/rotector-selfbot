/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The results table as a PNG. A port of rsb/imagerender.py's table renderer,
 * drawn on a <canvas> instead of with Pillow.
 *
 * The CSV is for machines and the TXT is for reading; this is for showing —
 * pasting a finding into a ticket, a report, or a moderator channel where a
 * screenshot is what people actually look at. Verdict colouring matches the
 * terminal UI and the HTML export, so a row means the same thing wherever it is
 * seen.
 *
 * Long lists are segmented exactly as the CSV is, because a single image of ten
 * thousand rows is both unopenable and unreadable — and, here, because a canvas
 * has a hard size ceiling that a five-figure scan would sail past. That ceiling
 * is the one thing this port has to handle that Pillow did not.
 *
 * Two pieces of the Python are deliberately absent:
 *
 *  - Its twenty-entry FONT_CANDIDATES list, which exists because Pillow needs a
 *    path to a font file. A browser takes a font *stack*, so the whole search is
 *    one string here.
 *  - The `cards` style, which draws a Discord profile popout per member from a
 *    fetched profile: avatar bytes, banner, bio, badges, connections. The plugin
 *    fetches no profiles, so a card would be a Discord-profile-shaped picture
 *    with the Discord profile missing. The HTML export's card view is the honest
 *    version of that and it is one file away.
 *
 * ---------------------------------------------------------------------------
 * Scope: this file follows rsb/palette.py, not DESIGN.md. Please read this
 * before "fixing" the colours.
 *
 * Every colour drawn below comes from an `ExportPalette` — the port of
 * rsb/palette.py, whose values are the export skin to the byte: `#121418`
 * ground, `#1c2026` panel bands, `#171a1f` stripes, `#343a42` rules, `#ffa62b`
 * heading. That is not the plugin's two-value language and it is not supposed
 * to be. palette.py exists precisely so that the PNG and the HTML cannot drift
 * apart, and rsb/imagerender.py is the renderer this one is a port of.
 *
 * DESIGN.md is not the authority here and says so itself: its scope block
 * governs `docs/index.html` and the project's web surfaces, and states that it
 * "does not govern the application" — the one sanctioned borrowing is the
 * two-value palette travelling *out* to rsb/tui/theme.py, page to app, never
 * the reverse. An export is the application's output. Restyling it into the
 * plugin's chrome would mean a scan exported from the terminal app and the same
 * scan exported from here were two different-looking documents, which is the
 * inconsistency that would actually cost a reader something. If the export skin
 * changes, it changes in rsb/palette.py first and both renderers follow.
 *
 * The five verdict hexes are the exception in both directions: they are
 * evidence, they are the same five everywhere in the project, and `verdictColour`
 * keeps them fixed even when `exportFollowTheme` lets the chrome follow a theme.
 * ---------------------------------------------------------------------------
 */

import { Logger } from "@utils/Logger";

import {
    asRows,
    cellValue,
    countStale,
    DEFAULT_PALETTE,
    ExportInput,
    ExportOptions,
    ExportPalette,
    ExportRow,
    expiryText,
    resolvePalette,
    retainedBeyondTerms,
    RETENTION_HOURS,
    retentionWindowHours,
    timestamp,
    validColumns,
    verdictColour,
    verdictLegend,
} from "./export";
import { isStale, reportAge, reportVerdict, shortAge } from "../api/types";
import { ATTRIBUTION, Verdict } from "../api/verdict";

const logger = new Logger("RotectorScan");

/** rows per image; a page that stays readable and openable (rsb/imagerender.py:33) */
export const DEFAULT_ROWS_PER_IMAGE = 60;

/** the widest one column may get, in characters (rsb/imagerender.py:195) */
export const DEFAULT_MAX_COLUMN_CHARS = 44;

export const DEFAULT_FONT_SIZE = 15;

/**
 * The single font stack, in place of the Python's per-platform file hunt.
 *
 * It must be monospace: the table is drawn on a fixed character grid, so a
 * proportional fallback would put every column out of line. Azeret Mono is
 * named first for the same reason the plugin's stylesheet names it — if the
 * machine happens to have it, the export looks like the rest of the project —
 * and everything after it is a face that actually exists somewhere.
 */
const FONT_STACK = "\"Azeret Mono\", ui-monospace, SFMono-Regular, Menlo, Consolas, \"DejaVu Sans Mono\", monospace";

/**
 * The largest edge a canvas may have.
 *
 * Chromium refuses to allocate past this and hands back a blank or a null
 * blob rather than an error, which would turn a 12,000-member export into a
 * silently empty picture. Everything below is clamped to it instead.
 */
const MAX_CANVAS_EDGE = 16000;

export interface PngOptions extends ExportOptions {
    rowsPerImage?: number;
    fontSize?: number;
    maxColumnChars?: number;
    /** device pixels per drawn pixel; 2 keeps text sharp when the image is zoomed */
    scale?: number;
    title?: string;
    subtitle?: string;
}

/** Whether this renderer can draw at all. Never throws. */
export function pngAvailable(): boolean {
    try {
        const canvas = document.createElement("canvas");
        return typeof canvas?.getContext === "function" && Boolean(canvas.getContext("2d"));
    } catch {
        return false;
    }
}

export function unavailableReason(): string {
    return pngAvailable() ? "" : "PNG export needs a canvas, which this client does not provide.";
}

/**
 * `1234` -> `1,234`.
 *
 * The Python writes its member count with `{:,}` (rsb/imagerender.py:304), which
 * is a comma whatever the machine's locale is. The locale is pinned here for the
 * same reason: two exports of one scan, one from the terminal app and one from
 * the plugin, should not differ in their punctuation.
 */
function count(value: number): string {
    try {
        return Number(value).toLocaleString("en-US");
    } catch {
        return String(value);
    }
}

/** Collapse whitespace and cut to `width` characters, as `_fit` does. */
function fit(text: string, width: number): string {
    const flat = String(text ?? "").split(/\s+/).filter(Boolean).join(" ");
    if (flat.length <= width) return flat;
    return `${flat.slice(0, Math.max(1, width - 1))}…`;
}

function rowVerdict(row: ExportRow): Verdict {
    try {
        return reportVerdict(row.report);
    } catch {
        return Verdict.UNKNOWN;
    }
}

function toBlob(canvas: HTMLCanvasElement): Promise<Blob> {
    return new Promise((resolve, reject) => {
        try {
            if (typeof canvas.toBlob === "function") {
                canvas.toBlob(blob => {
                    if (blob) resolve(blob);
                    else reject(new Error("the canvas produced no image"));
                }, "image/png");
                return;
            }
            // Very old hosts have no toBlob. toDataURL always exists, and the
            // base64 round-trip is worth it to avoid failing the export.
            const url = canvas.toDataURL("image/png");
            const binary = atob(url.slice(url.indexOf(",") + 1));
            const bytes = new Uint8Array(binary.length);
            for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
            resolve(new Blob([bytes], { type: "image/png" }));
        } catch (err) {
            reject(err);
        }
    });
}

/**
 * Draw the table, one image per page.
 *
 * The layout is the Python's, line for line: a header band in the panel colour,
 * a rule under it, alternating stripes, a three-pixel bar in the left margin in
 * the row's verdict colour so severity reads before any text does, and a footer
 * carrying the attribution and the expiry.
 */
export async function renderPngParts(reports: ExportInput, opts: PngOptions = {}): Promise<Blob[]> {
    const rows = asRows(reports);
    const columns = validColumns(opts.columns);
    const palette: ExportPalette = opts.palette ?? resolvePalette() ?? DEFAULT_PALETTE;
    const stamp = opts.stamp || timestamp();

    if (!pngAvailable()) throw new Error(unavailableReason());

    const fontSize = Math.max(8, Math.min(48, Number(opts.fontSize ?? DEFAULT_FONT_SIZE) || DEFAULT_FONT_SIZE));
    const maxColumnChars = Math.max(8, Math.min(200, Number(opts.maxColumnChars ?? DEFAULT_MAX_COLUMN_CHARS) || DEFAULT_MAX_COLUMN_CHARS));
    let scale = Math.max(1, Math.min(3, Number(opts.scale ?? 2) || 2));

    const title = opts.title ?? opts.label ?? "";
    const subtitle = opts.subtitle ?? `${opts.scope || "filtered"}  -  generated ${stamp}`;

    const font = `${fontSize}px ${FONT_STACK}`;
    const boldFont = `700 ${fontSize}px ${FONT_STACK}`;

    // The Stale column is on by default but the operator can deselect it, and a
    // stale row that says so nowhere is exactly what the labelling exists to
    // prevent — so when the column is absent the age rides on the verdict cell
    // instead (rsb/imagerender.py:241-247). Measured through the same function
    // that draws it, or the suffix would be cut off by a column sized without it.
    const markVerdict = !columns.includes("stale");
    const cellText = (row: ExportRow, column: string): string => {
        const value = cellValue(row, column);
        if (column !== "verdict" || !markVerdict) return value;
        return isStale(row.report) ? `${value}  stale ${shortAge(reportAge(row.report))}` : value;
    };

    // one throwaway context purely to measure the character cell
    const probe = document.createElement("canvas").getContext("2d");
    if (!probe) throw new Error(unavailableReason() || "the canvas gave up no drawing context");
    probe.font = font;
    const cellW = Math.max(1, probe.measureText("0").width);

    const lineH = fontSize + 8;
    const pad = 14;

    // width each column needs, capped so one verbose field cannot dominate
    const widths = columns.map(column => {
        let longest = column.length;
        for (const row of rows) {
            const value = cellText(row, column).split(/\s+/).filter(Boolean).join(" ");
            if (value.length > longest) longest = value.length;
        }
        return Math.min(Math.max(longest, column.length), maxColumnChars);
    });

    // Columns are dropped from the right rather than squeezed if the table
    // cannot fit a canvas at all. It is a blunt answer, but a picture with the
    // last column missing beats one the browser refused to allocate — and the
    // CSV beside it has every column.
    let visible = columns.length;
    const tableWidth = (upTo: number) =>
        pad * 2 + (widths.slice(0, upTo).reduce((a, b) => a + b, 0) + 3 * Math.max(0, upTo - 1)) * cellW + 2;
    while (visible > 1 && tableWidth(visible) > MAX_CANVAS_EDGE) visible--;
    const drawn = columns.slice(0, visible);
    const drawnWidths = widths.slice(0, visible);
    const dropped = columns.length - visible;

    const legend = verdictLegend(rows);

    // An image is the export most likely to be pasted somewhere and read months
    // later with no folder around it, so its own retention and its own
    // staleness are strips of the picture rather than metadata beside it.
    const retained = retainedBeyondTerms(opts);
    const retentionHours = retentionWindowHours(opts);
    const retentionLine = retained
        ? `Kept ${retentionHours}h by choice (until ${expiryText(stamp, retentionHours)}) - Rotector's terms allow ${RETENTION_HOURS}h. Appeals: rotector.com`
        : `Delete within ${RETENTION_HOURS}h (by ${expiryText(stamp)}) - Rotector's terms forbid keeping this longer. Appeals: rotector.com`;
    const staleRows = countStale(rows);
    const staleStrip = staleRows > 0
        ? `${count(staleRows)} finding(s) here are older than ${RETENTION_HOURS}h and describe the day they were answered - re-check before acting.`
        : "";

    const footerLines = [
        `${count(rows.length)} member${rows.length === 1 ? "" : "s"}  -  ${ATTRIBUTION}`,
        retentionLine,
        ...(staleStrip ? [staleStrip] : []),
        ...(dropped > 0 ? [`${dropped} column(s) omitted to keep the image within a drawable size; the CSV export has all of them.`] : []),
        ...legend,
    ];

    // The image is as wide as the wider of the table and the footer.
    //
    // The footer is not decoration: the attribution, the appeal route and the
    // expiry are what Rotector's terms require of any file carrying
    // their data, and the legend is what keeps NO DETECTIONS from being read as
    // "clean". A two-column export is narrower than any of those sentences, and
    // ellipsising a line that has to be there would leave the requirement
    // formally met and actually unreadable — so the canvas grows instead. The
    // Python never faced this because Pillow draws past the edge of the image;
    // it clips there too, just silently.
    const footerChars = footerLines.reduce((widest, line) => Math.max(widest, line.length), 0);
    const imageW = Math.min(
        MAX_CANVAS_EDGE,
        Math.ceil(Math.max(tableWidth(visible), pad * 2 + footerChars * cellW + 2))
    );

    const headerH = pad + lineH * ((title ? 1 : 0) + (subtitle ? 1 : 0) + 1);
    const footerH = lineH * (footerLines.length + 1) + pad;

    // rows per page, then clamped again so no single page exceeds the ceiling
    let perPage = Math.trunc(Number(opts.rowsPerImage ?? DEFAULT_ROWS_PER_IMAGE));
    if (!isFinite(perPage) || perPage < 0) perPage = DEFAULT_ROWS_PER_IMAGE;
    const roomForRows = Math.floor((MAX_CANVAS_EDGE / scale - headerH - footerH - lineH * 2) / lineH);
    const ceiling = Math.max(1, roomForRows);
    if (perPage === 0 || perPage > ceiling) perPage = Math.min(perPage === 0 ? rows.length || 1 : perPage, ceiling);

    if (imageW * scale > MAX_CANVAS_EDGE) scale = 1;

    const chunks: ExportRow[][] = [];
    if (rows.length === 0) {
        chunks.push([]);
    } else {
        for (let i = 0; i < rows.length; i += perPage) chunks.push(rows.slice(i, i + perPage));
    }

    const blobs: Blob[] = [];
    for (let index = 0; index < chunks.length; index++) {
        const chunk = chunks[index];
        const imageH = Math.min(
            MAX_CANVAS_EDGE,
            Math.ceil(headerH + lineH * (chunk.length + 2) + footerH)
        );

        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(imageW * scale));
        canvas.height = Math.max(1, Math.round(imageH * scale));
        const ctx = canvas.getContext("2d");
        if (!ctx) throw new Error("the canvas gave up no drawing context");
        ctx.scale(scale, scale);
        // Pillow's draw.text() places the top-left corner of the text box, so
        // the whole layout below is written in those coordinates.
        ctx.textBaseline = "top";
        ctx.textAlign = "left";

        ctx.fillStyle = palette.background;
        ctx.fillRect(0, 0, imageW, imageH);

        let y = pad;
        if (title) {
            const label = chunks.length > 1 ? `${title}   (part ${index + 1} of ${chunks.length})` : title;
            ctx.font = boldFont;
            ctx.fillStyle = palette.heading;
            ctx.fillText(label, pad, y);
            y += lineH;
        }
        if (subtitle) {
            ctx.font = font;
            ctx.fillStyle = palette.muted;
            ctx.fillText(subtitle, pad, y);
            y += lineH;
        }
        y += 4;

        // header band
        ctx.fillStyle = palette.panel;
        ctx.fillRect(0, y - 3, imageW, lineH);
        ctx.font = boldFont;
        ctx.fillStyle = palette.text;
        let x = pad;
        drawn.forEach((column, i) => {
            ctx.fillText(column.replace(/_/g, " ").toUpperCase().slice(0, drawnWidths[i]), x, y);
            x += (drawnWidths[i] + 3) * cellW;
        });
        y += lineH;
        ctx.fillStyle = palette.grid;
        ctx.fillRect(0, y - 2, imageW, 1);

        chunk.forEach((row, rowIndex) => {
            if (rowIndex % 2) {
                ctx.fillStyle = palette.stripe;
                ctx.fillRect(0, y - 2, imageW, lineH);
            }
            const accent = verdictColour(rowVerdict(row), palette);
            // a bar in the margin, so severity reads before any text does
            ctx.fillStyle = accent;
            ctx.fillRect(0, y - 2, 3, lineH);

            ctx.font = font;
            let cx = pad;
            drawn.forEach((column, i) => {
                // The stale cell takes the CAUTION hue, the same one the "did
                // not answer" line takes everywhere else in this project: it is
                // a finding about the evidence, which is what colour is for.
                ctx.fillStyle = column === "verdict"
                    ? accent
                    : column === "stale" && cellValue(row, column)
                        ? verdictColour(Verdict.CAUTION, palette)
                        : palette.text;
                ctx.fillText(fit(cellText(row, column), drawnWidths[i]), cx, y);
                cx += (drawnWidths[i] + 3) * cellW;
            });
            y += lineH;
        });

        if (!chunk.length) {
            ctx.font = font;
            ctx.fillStyle = palette.muted;
            ctx.fillText("(no members matched the selected filter)", pad, y);
            y += lineH;
        }

        y += 6;
        ctx.fillStyle = palette.grid;
        ctx.fillRect(0, y, imageW, 1);
        y += 6;

        ctx.font = font;
        for (const line of footerLines) {
            ctx.fillStyle = palette.muted;
            ctx.fillText(fit(line, Math.max(20, Math.floor((imageW - pad * 2) / cellW))), pad, y);
            y += lineH;
        }

        blobs.push(await toBlob(canvas));

        // Let the frame finish between pages. A twenty-page export drawn in one
        // synchronous run freezes the client, and the export modal is the thing
        // that would be frozen — including its stop button.
        await new Promise(resolve => setTimeout(resolve, 0));
    }

    return blobs;
}

/**
 * The whole table as one image.
 *
 * Kept separate from `renderPngParts` because most exports are one page and a
 * caller that wants a single blob should not have to unpack an array. A list
 * long enough to need paging is still paged — a canvas cannot be made taller
 * than the ceiling — and this returns the first page, which is why the menu
 * calls `renderPngParts` and offers all of them.
 */
export async function renderPng(reports: ExportInput, opts: PngOptions = {}): Promise<Blob> {
    const parts = await renderPngParts(reports, { ...opts, rowsPerImage: opts.rowsPerImage ?? 0 });
    if (!parts.length) throw new Error("nothing to draw");
    if (parts.length > 1) {
        logger.warn(`the list needed ${parts.length} pages; renderPng returned the first. Use renderPngParts for all of them.`);
    }
    return parts[0];
}
