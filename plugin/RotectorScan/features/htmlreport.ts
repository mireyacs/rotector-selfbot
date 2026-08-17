/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The results as one self-contained HTML page. A port of rsb/htmlrender.py.
 *
 * Deliberately one file, not segmented: a browser has no trouble with ten
 * thousand rows, and splitting would only make the thing harder to search. The
 * page carries both views — the table and the profile cards — with a filter box
 * and a toggle, so one file answers both "show me everything" and "show me this
 * person".
 *
 * Two departures from the Python, both forced by where this runs:
 *
 *  - Avatars are referenced by their CDN URL rather than inlined as data URIs.
 *    The Python has already downloaded them; a renderer would have to fetch a
 *    few thousand images through a client whose CSP it does not control, which
 *    is a lot of requests to make on somebody's account for a decoration. The
 *    page degrades to initials when it is opened offline, which is what the
 *    Python's own no-avatar path draws.
 *  - The Python's profile blocks (About me, Account, badges, connections) are
 *    not here, because the plugin never fetches a profile. What it knows is what
 *    the client already holds, and inventing the rest would be worse than
 *    leaving the section out.
 *
 * ---------------------------------------------------------------------------
 * Scope: this file follows rsb/palette.py, not DESIGN.md. Please read this
 * before "fixing" the styling.
 *
 * The 6px/10px/999px radii below, the Discord-blurple banner fill and the
 * circular avatar are not the plugin's two-value design language, and they are
 * not meant to be. They are the *export skin*, ported line for line from
 * rsb/htmlrender.py, whose colours come from rsb/palette.py — one palette
 * shared by the HTML and the PNG so that "match the app's theme" stopped being
 * a change in two places that could disagree (palette.py's own docstring).
 *
 * DESIGN.md is not the authority here and says so itself: its scope block
 * states that the system governs `docs/index.html`, the og image and "any
 * future web surface", and that it "does not govern the application", whose
 * one sanctioned borrowing is the two-value palette travelling *out* to
 * rsb/tui/theme.py — page to app, never the reverse. An export is the
 * application's output, and this renderer is the plugin's copy of it.
 *
 * So the consistency that matters for these two files is with the other
 * exports, not with the plugin's chrome: a scan exported from the terminal app
 * and the same scan exported from this plugin must be the same document, or a
 * moderator holding both has to work out which tool drew which. If the export
 * skin is to change, it changes in rsb/palette.py first and both renderers
 * follow. style.css already records the plugin-side half of this, where it
 * notes that the export's round avatar is "the old export skin, not this
 * language".
 *
 * What is *not* negotiable in either direction: the five verdict hexes. They
 * are evidence, they are the same five everywhere in the project, and
 * palette.py fixes them even when the chrome follows a theme.
 * ---------------------------------------------------------------------------
 */

import {
    asRows,
    cellValue,
    columnLabel,
    countStale,
    DEFAULT_PALETTE,
    ExportInput,
    ExportOptions,
    ExportPalette,
    ExportRow,
    expiryText,
    headerLines,
    RETENTION_HOURS,
    resolvePalette,
    retainedBeyondTerms,
    retentionWindowHours,
    timestamp,
    validColumns,
    verdictColour,
    verdictLegend,
} from "./export";
import { accountVerdict, describeAge, isStale, profileUrl, reportAge, reportVerdict, sortedAccounts } from "../api/types";
import {
    ALL_VERDICTS,
    ATTRIBUTION,
    categoryName,
    flagIsActionable,
    flagName,
    Verdict,
    verdictLabel,
    verdictMeaning,
    verdictName,
} from "../api/verdict";

/** how many evidence lines a card prints per reason (rsb/htmlrender.py:256) */
const CARD_EVIDENCE = 4;

/**
 * Escape text for HTML.
 *
 * Every string that reaches the page goes through this, without exception.
 * Evidence, detector messages and Roblox usernames are third-party free text —
 * rsb/tui/app.py renders them as text and never as markup for exactly this
 * reason, and a report page that executed a finding's contents would be the
 * worst possible failure of a tool that exists to look at hostile accounts.
 */
function esc(value: unknown): string {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

/**
 * `1234` -> `1,234`.
 *
 * The Python writes the member count with `{:,}` (rsb/htmlrender.py:396), which
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

function initials(name: string): string {
    const parts = String(name ?? "")
        .split("")
        .map(c => (/[A-Za-z0-9]/.test(c) ? c : " "))
        .join("")
        .split(/\s+/)
        .filter(Boolean);
    if (!parts.length) return "?";
    return (parts[0][0] + (parts.length > 1 ? parts[1][0] : "")).toUpperCase();
}

function reportVerdictOf(row: ExportRow): Verdict {
    try {
        return reportVerdict(row.report);
    } catch {
        return Verdict.UNKNOWN;
    }
}

function accountsOf(row: ExportRow) {
    try {
        return sortedAccounts(row.report);
    } catch {
        return [];
    }
}

// --------------------------------------------------------------------------
// the stylesheet (rsb/htmlrender.py:41-98)
// --------------------------------------------------------------------------

const STYLE = `
%ROOT%
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px;
  background: var(--bg); color: var(--fg);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
h1 { margin: 0 0 4px; font-size: 20px; }
.sub { color: var(--muted); margin-bottom: 8px; }
.legend { color: var(--muted); font-size: 12px; margin-bottom: 18px; }
.legend b { color: var(--fg); }
.bar { display: flex; gap: 10px; align-items: center; margin-bottom: 16px;
       flex-wrap: wrap; position: sticky; top: 0; background: var(--bg);
       padding: 8px 0; z-index: 5; }
input[type=search], select {
  background: var(--panel); color: var(--fg); border: 1px solid var(--grid);
  border-radius: 6px; padding: 7px 10px; font: inherit; min-width: 220px;
}
button {
  background: var(--panel); color: var(--fg); border: 1px solid var(--grid);
  border-radius: 6px; padding: 7px 12px; font: inherit; cursor: pointer;
}
button.on { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
.count { color: var(--muted); margin-left: auto; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--rule);
         vertical-align: top; }
th { position: sticky; top: 52px; background: var(--panel); cursor: pointer;
     user-select: none; white-space: nowrap; }
th:hover { background: var(--grid); }
tr:nth-child(even) td { background: var(--stripe); }
td.verdict { font-weight: 600; white-space: nowrap; }
td.wrap { max-width: 460px; }
.cards { display: grid; gap: 16px;
         grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); }
.card { background: var(--panel); border-radius: 10px; overflow: hidden;
        border: 1px solid var(--rule); }
.banner { height: 84px; background: #5865f2; background-size: cover;
          background-position: center; }
.card .top { display: flex; gap: 12px; padding: 0 16px; margin-top: -34px; }
.avatar { width: 68px; height: 68px; border-radius: 50%; border: 4px solid var(--panel);
          background: #5865f2; flex: none; display: grid; place-items: center;
          font-weight: 700; font-size: 22px; color: #fff; overflow: hidden; }
.avatar img { width: 100%; height: 100%; object-fit: cover; }
.who { padding-top: 38px; min-width: 0; }
.who .name { font-size: 17px; font-weight: 700; }
.who .handle { color: var(--muted); font-size: 12px; word-break: break-all; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 999px;
         font-size: 11px; font-weight: 700; color: var(--on-badge); }
/* Staleness is a finding about the evidence rather than a verdict of its own,
   so it takes the CAUTION hue and never a sixth colour (rsb/htmlrender.py:93). */
.badge.stale { background: var(--stale); color: var(--on-badge); }
.stale-line { color: var(--stale); font-size: 12px; margin-top: 4px; }
td.stale-cell { color: var(--stale); white-space: nowrap; }
.card section { padding: 10px 16px; border-top: 1px solid var(--rule); }
.card h3 { margin: 0 0 6px; font-size: 11px; letter-spacing: .06em;
           color: var(--muted); text-transform: uppercase; }
.card .line { margin-bottom: 3px; word-wrap: break-word; }
.muted { color: var(--muted); }
a { color: var(--link); }
footer { margin-top: 24px; color: var(--muted); font-size: 12px;
         border-top: 1px solid var(--rule); padding-top: 12px; }
footer b { color: var(--fg); }
.hidden { display: none !important; }
`;

/**
 * The page's own behaviour: filter, view toggle, sort.
 *
 * Written without template placeholders on purpose — the whole thing is
 * interpolated into the page below, and a `$`-brace in here would be evaluated
 * by this module instead of shipped to the file.
 */
const SCRIPT = `
const rows = () => [...document.querySelectorAll('[data-hay]')];
const q = document.getElementById('q');
const vf = document.getElementById('vf');
const count = document.getElementById('count');

function apply() {
  const needle = q.value.trim().toLowerCase();
  const want = vf.value;
  let shown = 0;
  for (const el of rows()) {
    const okText = !needle || el.dataset.hay.includes(needle);
    const okVerdict = want === 'all' || el.dataset.verdict === want;
    const visible = okText && okVerdict;
    el.classList.toggle('hidden', !visible);
    if (visible) shown++;
  }
  // Every member carries two [data-hay] elements — a table row and a card — so
  // both sides are halved to count members rather than elements. The Python
  // halves only the denominator (rsb/htmlrender.py:117), which reads as twice
  // as many members shown as there are; corrected here rather than copied.
  count.textContent = (shown / 2) + ' of ' + (rows().length / 2) + ' shown';
}
q.addEventListener('input', apply);
vf.addEventListener('change', apply);

const tableBtn = document.getElementById('t-table');
const cardBtn = document.getElementById('t-cards');
function view(which) {
  document.getElementById('tableview').classList.toggle('hidden', which !== 'table');
  document.getElementById('cardview').classList.toggle('hidden', which !== 'cards');
  tableBtn.classList.toggle('on', which === 'table');
  cardBtn.classList.toggle('on', which === 'cards');
}
tableBtn.onclick = () => view('table');
cardBtn.onclick = () => view('cards');

// click a header to sort by that column
document.querySelectorAll('th').forEach((th, index) => {
  let asc = true;
  th.onclick = () => {
    const body = th.closest('table').querySelector('tbody');
    const sorted = [...body.rows].sort((a, b) => {
      const x = a.cells[index] ? a.cells[index].innerText.trim() : '';
      const y = b.cells[index] ? b.cells[index].innerText.trim() : '';
      const n = parseFloat(x) - parseFloat(y);
      return (isNaN(n) ? x.localeCompare(y, undefined, {sensitivity: 'base'}) : n)
             * (asc ? 1 : -1);
    });
    asc = !asc;
    sorted.forEach(r => body.appendChild(r));
  };
});
view('table');
apply();
`;

/**
 * The `:root` block, built from the palette (rsb/htmlrender.py:301-324).
 *
 * Substituted rather than formatted, for the same reason the Python substitutes:
 * the sheet is mostly braces.
 */
function styleFor(palette: ExportPalette): string {
    const root = [
        ":root {",
        `  color-scheme: ${palette.dark ? "dark" : "light"};`,
        `  --bg: ${palette.background};`,
        `  --fg: ${palette.text};`,
        `  --panel: ${palette.panel};`,
        `  --stripe: ${palette.stripe};`,
        `  --grid: ${palette.grid};`,
        `  --rule: ${palette.rule};`,
        `  --muted: ${palette.muted};`,
        `  --accent: ${palette.accent};`,
        `  --on-accent: ${palette.onAccent};`,
        `  --on-badge: ${palette.background};`,
        `  --link: ${palette.link};`,
        // The stale marker's hue, taken from the verdict table rather than
        // written as a literal: it is the CAUTION colour, and those five are
        // fixed even when the chrome follows a theme.
        `  --stale: ${verdictColour(Verdict.CAUTION, palette)};`,
        "}",
    ].join("\n");
    return STYLE.replace("%ROOT%", root);
}

/**
 * Verdict -> CSS colour.
 *
 * Fixed even when the chrome follows a theme, exactly as
 * rsb/htmlrender.py:327-334 fixes it: the badge colour is the finding, and
 * draining it would remove the distinction a reader scans for.
 */
export function verdictCss(palette?: ExportPalette | null): Record<number, string> {
    const out: Record<number, string> = {};
    for (const verdict of ALL_VERDICTS) out[verdict] = verdictColour(verdict, palette);
    return out;
}

// --------------------------------------------------------------------------
// one card
// --------------------------------------------------------------------------

function card(row: ExportRow, palette: ExportPalette): string {
    const verdict = reportVerdictOf(row);
    const colour = verdictColour(verdict, palette);
    const accounts = accountsOf(row);
    const sections: string[] = [];

    if (accounts.length) {
        const lines = accounts.map(account => {
            const actionable = flagIsActionable(account.flagType) ? "actionable" : "not actionable";
            const category = categoryName(account.category);
            let url = "";
            try {
                url = profileUrl(account);
            } catch {
                url = "";
            }
            const name = url
                ? `<a href="${esc(url)}" rel="noreferrer noopener">${esc(account.username)}</a>`
                : esc(account.username);
            let tier = verdict;
            try {
                tier = accountVerdict(account);
            } catch {
                // the account's own tier is unreadable; the member's stands
            }
            return (
                `<div class="line"><b style="color:${esc(verdictColour(tier, palette))}">` +
                `${esc(flagName(account.flagType))}</b> ` +
                `<span class="muted">(${esc(actionable)})</span> ${name} ` +
                `<span class="muted">(${esc(account.userId)})</span>` +
                (category ? ` &middot; ${esc(category)}` : "") +
                "</div>"
            );
        });
        sections.push(`<section><h3>Linked Roblox accounts</h3>${lines.join("")}</section>`);
    }

    const reasons: string[] = [];
    for (const account of accounts) {
        for (const [name, detail] of Object.entries(account.reasons ?? {})) {
            const message = String((detail as any)?.message ?? "").split(/\s+/).filter(Boolean).join(" ");
            reasons.push(`<div class="line"><b>${esc(name)}</b>: ${esc(message)}</div>`);
            const evidence = ((detail as any)?.evidence ?? []) as unknown[];
            for (const item of evidence.slice(0, CARD_EVIDENCE)) {
                reasons.push(`<div class="line muted">&mdash; ${esc(item)}</div>`);
            }
        }
    }
    if (reasons.length) sections.push(`<section><h3>Why</h3>${reasons.join("")}</section>`);

    if (row.report?.servers?.length) {
        const names = row.report.servers.map(s => esc(s?.serverName ?? "")).join(", ");
        sections.push(
            `<section><h3>Tracked servers</h3><div class="line">${names}</div>` +
            "<div class=\"line muted\">Membership is a signal, not a verdict.</div></section>"
        );
    }

    if (row.report?.error) {
        sections.push(
            `<section><h3>Lookup</h3><div class="line" style="color:${esc(
                verdictColour(Verdict.CAUTION, palette)
            )}">This member could not be checked in full: ${esc(row.report.error)}</div>` +
            "<div class=\"line muted\">A lookup that did not answer is not a clean result.</div></section>"
        );
    }

    // The avatar is a remote URL; if it will not load — no network, an avatar
    // since deleted — it removes itself and the initials underneath stand.
    const avatar = row.avatarUrl
        ? `<img src="${esc(row.avatarUrl)}" alt="" onerror="this.remove()">`
        : esc(initials(row.displayName || row.username));

    // A card is the form somebody screenshots and forwards, so the age goes on
    // it twice: as a badge beside the verdict, where the eye already is, and as
    // the sentence saying what an old answer does and does not mean.
    let staleBadge = "";
    let staleLine = "";
    if (isStale(row.report)) {
        const age = describeAge(reportAge(row.report));
        staleBadge = `<span class="badge stale">STALE ${esc(age)}</span>`;
        staleLine =
            `<div class="stale-line">Answered ${esc(age)} ago, past the ${RETENTION_HOURS}-hour ` +
            "window Rotector's Terms of Use allow. Flag types change and appeals succeed, so " +
            "re-check before acting on this.</div>";
    }

    const hay = [row.discordId, row.username, row.displayName, verdictLabel(verdict), ...accounts.map(a => a.username)]
        .join(" ")
        .toLowerCase();

    return (
        `<article class="card" data-hay="${esc(hay)}" data-verdict="${esc(verdictName(verdict))}">` +
        "<div class=\"banner\"></div>" +
        "<div class=\"top\">" +
        `<div class="avatar">${avatar}</div>` +
        "<div class=\"who\">" +
        `<div class="name">${esc(row.displayName)}</div>` +
        `<div class="handle">${row.username ? `@${esc(row.username)}` : "unknown handle"}</div>` +
        `<div class="handle">ID ${esc(row.discordId)}</div>` +
        "<div style=\"margin-top:6px\">" +
        `<span class="badge" style="background:${esc(colour)}" title="${esc(verdictMeaning(verdict))}">` +
        `${esc(verdictLabel(verdict))}</span>` +
        staleBadge +
        "</div>" +
        staleLine +
        "</div></div>" +
        sections.join("") +
        "</article>"
    );
}

// --------------------------------------------------------------------------
// the page
// --------------------------------------------------------------------------

/**
 * One self-contained page holding both the table and the cards.
 *
 * The footer is not decoration: the attribution, the appeal route and the
 * 24-hour expiry are what Rotector's terms require of any file that leaves with
 * their data in it.
 */
export function renderHtml(reports: ExportInput, opts: ExportOptions = {}): string {
    const rows = asRows(reports);
    const columns = validColumns(opts.columns);
    const palette = opts.palette ?? resolvePalette() ?? DEFAULT_PALETTE;
    const stamp = opts.stamp || timestamp();
    const label = opts.label || "Rotector scan";

    const head = columns.map(c => `<th>${esc(columnLabel(c))}</th>`).join("");

    // The Stale column is on by default but the operator can deselect it, and a
    // stale row that says so nowhere is the failure this whole feature exists
    // to prevent — so when the column is gone the verdict cell carries the age
    // instead (rsb/htmlrender.py:374-377).
    const markVerdict = !columns.includes("stale");

    const bodyRows = rows.map(row => {
        const verdict = reportVerdictOf(row);
        const stale = isStale(row.report);
        const cells = columns.map(column => {
            let value = cellValue(row, column);
            const classes: string[] = [];
            if (column === "verdict") {
                classes.push("verdict");
                if (stale && markVerdict) value += `  · stale ${describeAge(reportAge(row.report))}`;
            }
            if (column === "stale" && value) classes.push("stale-cell");
            if (value.length > 60) classes.push("wrap");
            const style = column === "verdict"
                ? ` style="color:${esc(verdictColour(verdict, palette))}"`
                : "";
            const cls = classes.length ? ` class="${classes.join(" ")}"` : "";
            const title = column === "verdict" ? ` title="${esc(verdictMeaning(verdict))}"` : "";
            return `<td${cls}${style}${title}>${esc(value)}</td>`;
        });
        const hay = columns.map(c => cellValue(row, c)).join(" ").toLowerCase();
        return `<tr data-hay="${esc(hay)}" data-verdict="${esc(verdictName(verdict))}">${cells.join("")}</tr>`;
    });

    const cards = rows.map(row => card(row, palette)).join("");

    const options = [...ALL_VERDICTS]
        .reverse()
        .map(v => `<option value="${esc(verdictName(v))}">${esc(verdictLabel(v))}</option>`)
        .join("");

    const legend = verdictLegend(rows)
        .map(line => {
            const split = line.indexOf(":");
            const word = split > 0 ? line.slice(0, split) : line;
            const meaning = split > 0 ? line.slice(split + 1).trim() : "";
            return `<div><b>${esc(word)}</b> &mdash; ${esc(meaning)}</div>`;
        })
        .join("");

    const provenance = headerLines(rows, { ...opts, stamp })
        .map(line => `<div>${esc(line)}</div>`)
        .join("");

    // What this page's own retention actually is. A page kept past the window
    // says so and still names the terms; a page inside them says exactly what
    // it has always said.
    const retained = retainedBeyondTerms(opts);
    const hours = retentionWindowHours(opts);
    const staleCount = countStale(rows);
    let retentionHtml = retained
        ? `This file is being kept for ${hours} hours by the operator's choice. Rotector's terms ` +
          `allow ${RETENTION_HOURS} hours, and flag statuses change well inside that &mdash; anything ` +
          "marked stale here describes the day it was answered, not today."
        : `Delete this file within ${RETENTION_HOURS} hours &mdash; Rotector's terms forbid keeping their\n  ` +
          "data longer than that, and flag statuses change.";
    if (staleCount) {
        retentionHtml +=
            `<br>${esc(count(staleCount))} of these ${esc(count(rows.length))} finding(s) were already ` +
            `older than ${RETENTION_HOURS} hours when this page was written; each one's age is on its ` +
            "row and its card. Re-check before acting on those.";
    }

    return `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>${esc(label)} - Rotector scan</title>
<style>${styleFor(palette)}</style>
</head><body>
<h1>${esc(label)}</h1>
<div class="sub">${esc(opts.scope || "filtered")} &middot; generated ${esc(stamp)} &middot;
  ${esc(count(rows.length))} member${rows.length === 1 ? "" : "s"} &middot;
  ${retained
        ? `kept until ${esc(expiryText(stamp, hours))} by choice`
        : `delete by ${esc(expiryText(stamp))}`}
  ${staleCount ? `&middot; ${esc(count(staleCount))} stale` : ""}</div>
<div class="legend">${legend}</div>

<div class="bar">
  <button id="t-table">Table</button>
  <button id="t-cards">Cards</button>
  <input type="search" id="q" placeholder="Filter by name, ID or Roblox account">
  <select id="vf"><option value="all">All verdicts</option>${options}</select>
  <span class="count" id="count"></span>
</div>

<div id="tableview">
  <table><thead><tr>${head}</tr></thead><tbody>${bodyRows.join("")}</tbody></table>
</div>
<div id="cardview" class="hidden"><div class="cards">${cards}</div></div>

<footer>
  ${provenance}
  <div style="margin-top:8px"><b>${esc(ATTRIBUTION)}</b></div>
  <div>Anyone listed here can appeal at <a href="https://rotector.com" rel="noreferrer noopener">rotector.com</a>.</div>
  <div>${retentionHtml}</div>
  <div>NO DETECTIONS means nothing has been detected yet. It is not a clean bill of health,
  and it is never a background check.</div>
  ${palette.source ? `<div>Chrome follows ${esc(palette.source)}; verdict colours never do.</div>` : ""}
</footer>
<script>${SCRIPT}</script>
</body></html>
`;
}
