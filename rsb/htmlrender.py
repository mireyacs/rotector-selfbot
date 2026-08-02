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

from .export import COLUMNS, ExportRow, valid_columns
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
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px;
  background: #121418; color: #dee2e6;
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
h1 { margin: 0 0 4px; font-size: 20px; }
.sub { color: #8a929c; margin-bottom: 18px; }
.bar { display: flex; gap: 10px; align-items: center; margin-bottom: 16px;
       flex-wrap: wrap; position: sticky; top: 0; background: #121418;
       padding: 8px 0; z-index: 5; }
input[type=search], select {
  background: #1c2026; color: #dee2e6; border: 1px solid #343a42;
  border-radius: 6px; padding: 7px 10px; font: inherit; min-width: 220px;
}
button {
  background: #1c2026; color: #dee2e6; border: 1px solid #343a42;
  border-radius: 6px; padding: 7px 12px; font: inherit; cursor: pointer;
}
button.on { background: #2b62d9; border-color: #2b62d9; color: #fff; }
.count { color: #8a929c; margin-left: auto; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid #23272e;
         vertical-align: top; }
th { position: sticky; top: 52px; background: #1c2026; cursor: pointer;
     user-select: none; white-space: nowrap; }
th:hover { background: #262b33; }
tr:nth-child(even) td { background: #171a1f; }
td.verdict { font-weight: 600; white-space: nowrap; }
td.wrap { max-width: 460px; }
.cards { display: grid; gap: 16px;
         grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); }
.card { background: #1c2026; border-radius: 10px; overflow: hidden;
        border: 1px solid #23272e; }
.banner { height: 84px; background: #5865f2; background-size: cover;
          background-position: center; }
.card .top { display: flex; gap: 12px; padding: 0 16px; margin-top: -34px; }
.avatar { width: 68px; height: 68px; border-radius: 50%; border: 4px solid #1c2026;
          background: #5865f2; flex: none; display: grid; place-items: center;
          font-weight: 700; font-size: 22px; color: #fff; overflow: hidden; }
.avatar img { width: 100%; height: 100%; object-fit: cover; }
.who { padding-top: 38px; min-width: 0; }
.who .name { font-size: 17px; font-weight: 700; }
.who .handle { color: #8a929c; font-size: 12px; word-break: break-all; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 999px;
         font-size: 11px; font-weight: 700; color: #0c0e11; }
.card section { padding: 10px 16px; border-top: 1px solid #23272e; }
.card h3 { margin: 0 0 6px; font-size: 11px; letter-spacing: .06em;
           color: #8a929c; text-transform: uppercase; }
.card .line { margin-bottom: 3px; word-wrap: break-word; }
.muted { color: #8a929c; }
a { color: #7aa2f7; }
footer { margin-top: 24px; color: #8a929c; font-size: 12px;
         border-top: 1px solid #23272e; padding-top: 12px; }
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
    colour = VERDICT_CSS.get(verdict, "#8a929c")
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
                f"<div class='line'><b style='color:{VERDICT_CSS.get(account.verdict, '#dee2e6')}'>"
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
      </div>
    </div>
  </div>
  {''.join(sections)}
</article>"""


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
) -> list[Path]:
    """Write one self-contained page holding both the table and the cards."""
    columns = valid_columns(columns)
    profiles = profiles or {}
    esc = html.escape

    head = "".join(f"<th>{esc(c.replace('_', ' ').title())}</th>" for c in columns)

    body_rows = []
    for row in rows:
        verdict = row.report.verdict
        cells = []
        for column in columns:
            value = COLUMNS[column](row)
            classes = []
            if column == "verdict":
                classes.append("verdict")
            if len(value) > 60:
                classes.append("wrap")
            style = (
                f" style=\"color:{VERDICT_CSS.get(verdict, '#dee2e6')}\""
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
<style>{_STYLE}</style>
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
  Delete this file within 24 hours &mdash; Rotector's terms forbid keeping their
  data longer than that, and flag statuses change.
</footer>
<script>{_SCRIPT}</script>
</body></html>"""

    path = directory / f"{stem}.html"
    path.write_text(page, encoding="utf-8")
    return [path]
