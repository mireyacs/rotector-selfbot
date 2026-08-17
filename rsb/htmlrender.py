"""Rendering results to a single self-contained HTML page.

Deliberately one file, not segmented: a browser has no trouble with ten
thousand rows, and splitting would only make the thing harder to search. Images
are inlined as data URIs so the page can be sent on its own without a folder of
assets going missing.

The page carries both views -- the table and the profile cards -- with a filter
box and a toggle, so one file answers both "show me everything" and "show me
this person".
"""

from __future__ import annotations

import base64
import html
import json
from collections.abc import Sequence
from pathlib import Path

from .export import COLUMNS, ExportRow, RETENTION_HOURS, count_stale, valid_columns
from .palette import DEFAULT as DEFAULT_PALETTE, ExportPalette
from .rotector import describe_age, is_stale, report_age
from .verdict import (
    ATTRIBUTION,
    Verdict,
    category_name,
    flag_is_actionable,
    flag_name,
    verdict_label,
)

#: verdict -> CSS colour, matching the terminal and the PNG
VERDICT_CSS = {
    Verdict.THREAT: "#f43f5e",
    Verdict.CAUTION: "#facc15",
    Verdict.INFO: "#38bdf8",
    Verdict.NO_DETECTIONS: "#4ade80",
    Verdict.UNKNOWN: "#8a929c",
}

_STYLE = """
%ROOT%
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px;
  background: var(--bg); color: var(--fg);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
h1 { margin: 0 0 4px; font-size: 20px; }
.sub { color: var(--muted); margin-bottom: 18px; }
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
/* Stale findings wear the CAUTION amber wherever they appear -- the reader of
   this file did not choose to keep the data past the window it was served
   under, so the label has to be as visible as the verdict it qualifies. */
.badge.stale { background: #facc15; color: #1a1a1a; }
.stale-line { color: #facc15; font-size: 12px; margin-top: 4px; }
td.stale-cell { color: #facc15; white-space: nowrap; }
.card section { padding: 10px 16px; border-top: 1px solid var(--rule); }
.card h3 { margin: 0 0 6px; font-size: 11px; letter-spacing: .06em;
           color: var(--muted); text-transform: uppercase; }
.card .line { margin-bottom: 3px; word-wrap: break-word; }
.muted { color: var(--muted); }
a { color: var(--link); }
footer { margin-top: 24px; color: var(--muted); font-size: 12px;
         border-top: 1px solid var(--rule); padding-top: 12px; }
.hidden { display: none !important; }
"""

_SCRIPT = """
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
  count.textContent = shown + ' of ' + (rows().length / 2) + ' shown';
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
      const x = a.cells[index].innerText.trim();
      const y = b.cells[index].innerText.trim();
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
"""


def _data_uri(payload: bytes | None) -> str | None:
    if not payload:
        return None
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _initials(name: str) -> str:
    parts = [p for p in "".join(c if c.isalnum() else " " for c in name).split() if p]
    if not parts:
        return "?"
    return (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")).upper()


def _card(row: ExportRow, profile) -> str:
    verdict = row.report.verdict
    colour = VERDICT_CSS.get(verdict, "var(--muted)")
    esc = html.escape

    banner = _data_uri(getattr(profile, "banner_bytes", None))
    accent = getattr(profile, "accent_colour", None)
    if banner:
        banner_style = f"background-image:url('{banner}')"
    elif accent:
        banner_style = f"background:rgb{accent}"
    else:
        fallback = profile.fallback_colour if profile is not None else (88, 101, 242)
        banner_style = f"background:rgb{fallback}"

    avatar = _data_uri(getattr(profile, "avatar_bytes", None))
    avatar_html = (
        f"<img src='{avatar}' alt=''>"
        if avatar
        else esc(_initials(row.display_name or row.username))
    )

    sections = []
    if profile is not None and getattr(profile, "bio", ""):
        sections.append(
            f"<section><h3>About me</h3><div class='line'>{esc(profile.bio)}</div></section>"
        )

    if profile is not None:
        facts = []
        if profile.connections:
            linked = ", ".join(
                f"{esc(platform)} (<b>{esc(name)}</b>)"
                + ("" if verified else " <span class='muted'>unverified</span>")
                for platform, name, verified in profile.connections
            )
            facts.append(f"<div class='line'>Linked: {linked}</div>")
        if profile.badges:
            facts.append(
                "<div class='line'>Badges: "
                + ", ".join(esc(label) for _id, label in profile.badges)
                + "</div>"
            )
        bits = []
        if profile.guild_joined_at:
            bits.append(f"joined this server {esc(profile.guild_joined_at)}")
        if profile.mutual_guilds:
            bits.append(f"{profile.mutual_guilds} mutual server(s)")
        if profile.has_nitro:
            bits.append(f"Nitro since {esc(profile.premium_since)}")
        if bits:
            facts.append(f"<div class='line muted'>{' &middot; '.join(bits)}</div>")
        if profile.timed_out_until:
            facts.append(
                f"<div class='line' style='color:#facc15'>Currently timed out "
                f"until {esc(profile.timed_out_until)}</div>"
            )
        if facts:
            sections.append("<section><h3>Account</h3>" + "".join(facts) + "</section>")

    accounts = sorted(row.report.accounts, key=lambda a: -int(a.verdict))
    if accounts:
        lines = []
        for account in accounts:
            actionable = (
                "actionable" if flag_is_actionable(account.flag_type) else "not actionable"
            )
            category = category_name(account.category)
            lines.append(
                f"<div class='line'><b style='color:{VERDICT_CSS.get(account.verdict, 'var(--fg)')}'>"
                f"{esc(flag_name(account.flag_type))}</b> "
                f"<span class='muted'>({actionable})</span> "
                f"<a href='{esc(account.profile_url)}'>{esc(account.username)}</a> "
                f"<span class='muted'>({account.user_id})</span>"
                + (f" &middot; {esc(category)}" if category else "")
                + "</div>"
            )
        sections.append(
            "<section><h3>Linked Roblox accounts</h3>" + "".join(lines) + "</section>"
        )

    reasons = []
    for account in accounts:
        for name, detail in (account.reasons or {}).items():
            message = " ".join(str(detail.get("message", "")).split())
            reasons.append(
                f"<div class='line'><b>{esc(name)}</b>: {esc(message)}</div>"
            )
            for item in (detail.get("evidence") or [])[:4]:
                reasons.append(
                    f"<div class='line muted'>&mdash; {esc(str(item))}</div>"
                )
    if reasons:
        sections.append("<section><h3>Why</h3>" + "".join(reasons) + "</section>")

    if row.report.servers:
        names = ", ".join(esc(s.server_name) for s in row.report.servers)
        sections.append(
            f"<section><h3>Tracked servers</h3><div class='line'>{names}</div>"
            f"<div class='line muted'>Membership is a signal, not a verdict.</div>"
            f"</section>"
        )

    created = getattr(profile, "created_at", "") if profile is not None else ""
    handle = f"@{esc(row.username)}"
    pronouns = getattr(profile, "pronouns", "") if profile is not None else ""
    if pronouns:
        handle += f" &middot; {esc(pronouns)}"
    if created:
        handle += f" &middot; joined Discord {esc(created)}"

    hay = " ".join(
        [row.discord_id, row.username, row.display_name, verdict_label(verdict)]
        + [a.username for a in accounts]
    ).lower()

    stale_badge = stale_line = ""
    if is_stale(row.report):
        age = describe_age(report_age(row.report))
        stale_badge = f'<span class="badge stale">STALE {esc(age)}</span>'
        stale_line = (
            f'<div class="stale-line">Answered {esc(age)} ago, past the '
            f"{RETENTION_HOURS}-hour window Rotector's Terms of Use allow. Flag "
            "types change and appeals succeed, so re-check before acting.</div>"
        )

    return f"""<article class="card" data-hay="{esc(hay)}" data-verdict="{esc(verdict.name)}">
  <div class="banner" style="{banner_style}"></div>
  <div class="top">
    <div class="avatar">{avatar_html}</div>
    <div class="who">
      <div class="name">{esc(row.display_name)}</div>
      <div class="handle">{handle}</div>
      <div class="handle">ID {esc(row.discord_id)}</div>
      <div style="margin-top:6px">
        <span class="badge" style="background:{colour}">{esc(verdict_label(verdict))}</span>
        {stale_badge}
      </div>
      {stale_line}
    </div>
  </div>
  {''.join(sections)}
</article>"""


def _style(palette: ExportPalette | None = None) -> str:
    """The stylesheet, with its `:root` block built from ``palette``.

    Substituted rather than formatted: the sheet is mostly braces, and
    ``str.format`` would mean doubling every one of them.
    """
    source = palette or DEFAULT_PALETTE
    root = (
        ":root {\n"
        f"  color-scheme: {'dark' if source.dark else 'light'};\n"
        f"  --bg: {source.background};\n"
        f"  --fg: {source.text};\n"
        f"  --panel: {source.panel};\n"
        f"  --stripe: {source.stripe};\n"
        f"  --grid: {source.grid};\n"
        f"  --rule: {source.rule};\n"
        f"  --muted: {source.muted};\n"
        f"  --accent: {source.accent};\n"
        f"  --on-accent: {source.on_accent};\n"
        f"  --on-badge: {source.background};\n"
        f"  --link: {source.link};\n"
        "}"
    )
    return _STYLE.replace("%ROOT%", root)


def verdict_css(palette: ExportPalette | None = None) -> dict:
    """Verdict -> CSS colour.

    Fixed even when the chrome follows a theme: the badge colour is the
    finding, and draining it would remove the distinction a reader scans for.
    """
    source = palette or DEFAULT_PALETTE
    return {verdict: source.verdict_css(verdict) for verdict in Verdict}


def render_html(
    rows: Sequence[ExportRow],
    directory: Path,
    stem: str,
    columns: Sequence[str],
    *,
    guild_name: str = "",
    stamp: str = "",
    scope: str = "",
    profiles: dict | None = None,
    palette: ExportPalette | None = None,
    retain_beyond_terms: bool = False,
    retention_hours: int = RETENTION_HOURS,
) -> list[Path]:
    """Write one self-contained page holding both the table and the cards."""
    columns = valid_columns(columns)
    profiles = profiles or {}
    esc = html.escape
    # The Stale column is on by default but can be deselected, and a stale
    # finding with nothing next to it reads as a current one. When the column is
    # gone, the age rides along with the verdict instead of vanishing.
    mark_verdict = "stale" not in columns

    head = "".join(f"<th>{esc(c.replace('_', ' ').title())}</th>" for c in columns)

    body_rows = []
    for row in rows:
        verdict = row.report.verdict
        cells = []
        stale = is_stale(row.report)
        for column in columns:
            value = COLUMNS[column](row)
            classes = []
            if column == "verdict":
                classes.append("verdict")
                if stale and mark_verdict:
                    value += f"  · stale {describe_age(report_age(row.report))}"
            if column == "stale" and value:
                classes.append("stale-cell")
            if len(value) > 60:
                classes.append("wrap")
            style = (
                f" style=\"color:{VERDICT_CSS.get(verdict, 'var(--fg)')}\""
                if column == "verdict"
                else ""
            )
            cls = f' class="{" ".join(classes)}"' if classes else ""
            cells.append(f"<td{cls}{style}>{esc(value)}</td>")
        hay = " ".join(COLUMNS[c](row) for c in columns).lower()
        body_rows.append(
            f'<tr data-hay="{esc(hay)}" data-verdict="{esc(verdict.name)}">'
            + "".join(cells)
            + "</tr>"
        )

    stale_count = count_stale(rows)
    if retain_beyond_terms:
        # Saying "delete within 24 hours" over a file the run deliberately kept
        # longer would be repeating a rule nobody followed. What the reader
        # needs is what this file is, and what the terms say about it.
        retention_html = (
            f"This file is being kept for {retention_hours} hours by the "
            f"operator's choice. Rotector's terms allow {RETENTION_HOURS} hours "
            "&mdash; and flag statuses change well inside that, so anything "
            "marked stale here describes the day it was answered, not today."
        )
    else:
        retention_html = (
            f"Delete this file within {RETENTION_HOURS} hours &mdash; Rotector's "
            "terms forbid keeping their data longer than that, and flag statuses "
            "change."
        )
    if stale_count:
        retention_html += (
            f"<br>{stale_count} of these {len(rows):,} finding(s) were already "
            f"older than {RETENTION_HOURS} hours when this page was written; each "
            "carries its age. Re-check those before acting."
        )

    cards = "".join(_card(row, profiles.get(row.discord_id)) for row in rows)
    options = "".join(
        f'<option value="{v.name}">{esc(verdict_label(v))}</option>'
        for v in sorted(Verdict, reverse=True)
    )

    page = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(guild_name)} - Rotector scan</title>
<style>{_style(palette)}</style>
</head><body>
<h1>{esc(guild_name)}</h1>
<div class="sub">{esc(scope)} &middot; generated {esc(stamp)} &middot;
  {len(rows):,} members</div>

<div class="bar">
  <button id="t-table">Table</button>
  <button id="t-cards">Cards</button>
  <input type="search" id="q" placeholder="Filter by name, ID or Roblox account">
  <select id="vf"><option value="all">All verdicts</option>{options}</select>
  <span class="count" id="count"></span>
</div>

<div id="tableview">
  <table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>
</div>
<div id="cardview" class="hidden"><div class="cards">{cards}</div></div>

<footer>
  {esc(ATTRIBUTION)}<br>
  Anyone listed here can appeal at <a href="https://rotector.com">rotector.com</a>.
  {retention_html}
</footer>
<script>{_SCRIPT}</script>
</body></html>"""

    path = directory / f"{stem}.html"
    path.write_text(page, encoding="utf-8")
    return [path]
