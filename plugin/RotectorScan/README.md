# RotectorScan

A Discord client plugin that checks the people around you against the
[Rotector](https://rotector.com) database and marks the ones with findings —
in the member list, beside messages, on profiles, and in a scan window that
works through a whole server, a role, your friends, your incoming friend
requests or a group DM.

It is the same evidence the terminal app
[rotector-selfbot](https://github.com/mireyacs/rotector-selfbot) shows, in the
client you already have open. Findings only by default: in a real server the
overwhelming majority of members have nothing against them, and marking all of
them buries the few that matter.

## Read this first

**A verdict is not a background check.** `NO DETECTIONS` means Rotector has not
flagged that account *yet* — it is not a clean bill of health, and Rotector's
own terms forbid presenting it as "safe". `UNKNOWN` means Rotector has never
heard of a linked Roblox account at all, which supports nothing whatsoever.
Only `THREAT` — flag types *Flagged* (1) and *Confirmed* (2) — is documented as
safe to act on. Types 3, 4, 5, 6 and 8 are explicitly not, and the plugin says
so on every screen that shows them rather than leaving you to remember it.

**This is a client mod, and Discord does not sanction client mods.** The
distinction from the terminal app is real and worth stating plainly: the
selfbot logs into your account with your token and drives Discord's API itself —
subscribing to the member sidebar over the gateway, paging REST endpoints — and
that is automation of a user account, which Discord's Terms of Service prohibit
outright. This plugin does none of that. It reads the stores the client has
already filled for its own use, and the only Discord calls it makes at all are
three the client itself makes for the same screens, each of them driven by
something you clicked:

* the role roster (`GET /guilds/:id/roles/:id/member-ids`) — what Discord
  fetches when you open a role's member list;
* a channel preload, when you press **Load more members** — what Discord does
  when you open a channel;
* `REQUEST_GUILD_MEMBERS` for ids it *already holds*, to turn snowflakes into
  names — what Discord does to render any list of members.

None of the three can discover a member the client could not otherwise see, none
runs on a timer, and none adds traffic Discord did not already expect from a
person scrolling a member list.

That is a meaningfully smaller thing to be doing. It is not nothing. Modifying
the client is against Discord's terms too, they do not carve out an exception
for safety tooling, and the risk of losing your account is real and entirely
yours. If you have the option, a regular bot with the `GUILD_MEMBERS` intent
does the same job with no such problem, and the Rotector half of this codebase
works unchanged either way.

**There is now one exception, and it is off by default.** The gateway member
scanner (below) can open its own connection, and one of the two ways to do that
is with your own account's token — which *is* automation of a user account, the
same thing the terminal app does and the same thing Discord's Terms prohibit.
It stays off until you switch it on and choose that token explicitly, the UI
states the risk in these words before it connects, and a bot token does the same
job better. Everything else in the plugin still reads the client's own stores.

The Rotector lookups are identical to the terminal app's, and so are Rotector's
terms about them — attribution, the appeal route, and the 24-hour retention
ceiling all still bind you. The plugin holds to that ceiling out of the box and
one opt-in setting lifts it; see *Keeping findings longer*.

## Install

The plugin is loaded by
[DynamicPluginLoader](https://github.com/mireyacs/equicord-dynamic-plugin-loader),
so there is nothing to build and no Equicord rebuild to do. Copy the
`RotectorScan` folder — the whole folder, with its `api/`, `components/`,
`features/`, `gateway/` and `theme/` subfolders — into the loader's plugin
directory:

| Platform | Directory |
|---|---|
| Linux | `~/.config/Equicord/dynamicPlugins/` |
| Linux (Flatpak Discord) | `~/.var/app/com.discordapp.Discord/config/Equicord/dynamicPlugins/` |
| macOS | `~/Library/Application Support/Equicord/dynamicPlugins/` |
| Windows | `%APPDATA%\Equicord\dynamicPlugins\` |

Then open **Settings → Dynamic Plugins → Reload all**, and enable
**RotectorScan** in the plugins list.

### Or let the script do it

`install.sh`, in this folder, finds every Equicord or Vencord data directory on
the machine — including a Flatpak Discord's, which lives inside the sandbox's
own config tree — and installs the plugin and both themes into each one. It is a
sync rather than an append, so the same command installs and updates.

```bash
./install.sh                 # install or update everywhere it finds a client
./install.sh --pull          # git pull --ff-only first, then install
./install.sh --dry-run       # print what it would do, change nothing
./install.sh --dir PATH      # one specific data directory
./install.sh --uninstall     # remove the plugin and the themes again
```

It replaces the plugin folder rather than merging into it, because a file left
behind by an older version would still be compiled and still be mirrored into
`settings.json`. `--uninstall` only removes theme files this plugin ships, so a
theme you wrote yourself is never collateral.

One failure worth naming, because it is silent: the loader treats **every
top-level entry** in `dynamicPlugins/` as its own plugin, so copying this
folder's *contents* there rather than the folder itself does not install one
plugin — it installs nine broken ones, and RotectorScan never appears in the
list at all. The script cannot get that wrong.

**The first load will ask for a restart, once.** The member-list, message and
profile surfaces are provided by Equicord's own API plugins
(`MemberListDecoratorsAPI`, `MessageDecorationsAPI`, `ProfileSectionsAPI`),
those only attach at Discord's startup, and enabling one that was off marks the
plugin *Needs reload* until you fully close and reopen Discord. Everything else
— the context menus, the scan window, the commands, the styles — works
immediately. After that first restart, editing the plugin and hitting *Reload
all* takes effect straight away.

No API key is needed. Adding one under **Settings → Plugins → RotectorScan**
raises your rate limit on this one connection; request one at
[panel.rotector.com](https://panel.rotector.com).

## What it adds

- **A mark in the member list, the DM list and beside message authors.** Drawn
  geometry rather than an icon: a stripe field whose colour *and* density both
  encode the tier, so it does not depend on colour alone. Members at
  `NO DETECTIONS` and `UNKNOWN` get no mark at all — that is what "findings
  only" means, and the count of what was hidden is always stated where the
  numbers are.
- **A report modal**, from the mark, the user context menu, or
  `/rotector check`. Identity, the verdict and its meaning, the triage call
  with the id of the rule that decided it, every linked Roblox account
  worst-first with flag type, category, confidence and the full reason text and
  evidence, the tracked-server memberships, and a copy button that produces the
  same plain-text summary the terminal app copies.
- **A findings section on user profiles**, present only when there is something
  to report.
- **A scan window**, from the server context menu, a group DM's context menu,
  or `/rotector scan`. Pick a source, see how many members it can actually
  reach and what that is a fraction of, then watch the ledger fill as results
  stream in.
- **A scan queue.** Scans are jobs: they keep running when you close the
  window, several can be queued, and `/rotector queue` (or the server menu)
  brings the list back. See *The queue and the budget it splits* below for why
  more scans do not mean more throughput.
- **A scan history**, from `/rotector history`, the server menu, or the scan
  window. Finished scans are written to a small database on this computer so
  they survive a restart — and each record is deleted 23 hours after its scan
  started, because Rotector's terms forbid keeping their responses longer
  (unless you lift that ceiling yourself; see *Keeping findings longer*). Only
  the findings are stored; the members with nothing against them are counted and
  dropped, and the record says how many.
- **Kick and ban, from the findings.** Select rows in the ledger and act on
  them behind a confirmation, with the appeal route written into every
  audit-log reason. See *Acting on a finding*.
- **Exports** — CSV (split into numbered parts), TXT, JSON, a styled HTML
  report and a PNG. Every file carries the attribution, states its own expiry,
  and dates any finding in it that is already past the 24-hour window.
- **Purge your own messages** from a DM, a group DM or a channel, from a
  context menu or `/rotector purge`. It plans first, shows you what would go,
  and only then deletes.
- **Vibe mode**, off by default: a small player for a scan that takes half an
  hour.

## Coverage: read this before trusting a server scan

**Your Discord client does not know who is in a large server, and neither does
this plugin.** It reads `GuildMemberStore`, which is a cache of what the client
has been sent — roughly the slice of the member sidebar you have scrolled
through, plus your friends. In a 10,000-member server that is normally a few
hundred people, and a member the client has never been told about is a member
this plugin cannot check.

So every count is shown as a fraction of the guild's real member count, and the
scan window says in a sentence what the list is and is not. Three things help:

- **Load more members.** The scan window can warm the sidebar, which makes
  Discord send more of it, and re-collect. It is slow, it is paced deliberately
  so it does not look like abuse, and it still stops well short of everyone in
  a big server.
- **Scan a role instead.** This is the one complete enumeration available
  without a gateway connection: Discord will hand over *every* holder of a role
  in one REST request, uncapped and regardless of who is online. If the people
  you want checked share a role — members, verified, a join gate, anything —
  scanning that role is a genuinely complete list.
- **Switch the gateway member scanner on**, which is the complete answer for a
  whole server. See below.

## The gateway member scanner

The terminal app gets a real roster because it drives the gateway itself: it
subscribes to member-list ranges (OP 14), asks for members by name prefix
(OP 8), and reports the fraction it reached. A renderer can open a WebSocket
too, so the plugin now does the same thing — `gateway/` is a port of the
selfbot's own scanner, heartbeat, resume, close-code table, send meter and all.

It is **off by default** and there are two ways to run it:

| Connection | What it does | Terms of Service |
|---|---|---|
| **A bot token** (recommended) | Asks Discord for `{ query: "", limit: 0 }` and receives the entire roster, offline members included, in one request. No sidebar scraping, no prefix sweep, no sampling. | Fine. This is what bot applications are for. |
| **Your own account** | Opens a second gateway session on your account, subscribes to the member sidebar range by range and sweeps for the rest. | **Against Discord's Terms of Service.** Discord does not carve out an exception for safety tooling, and accounts caught driving the API this way get disabled. The risk is entirely yours. |

To use a bot: create an application at
[discord.com/developers](https://discord.com/developers/applications), open
**Bot → Privileged Gateway Intents** and switch **SERVER MEMBERS INTENT** on,
invite the bot to the server you want to scan, and paste its token into
`botToken` in the plugin's settings with `gatewayToken` set to *bot*. **If the
intent is off, Discord closes the connection with code 4014 and no members
arrive** — the plugin says exactly that when it happens rather than reporting an
empty server.

The token is used for one thing: Discord's own gateway. It is never logged,
never put in an error message, never written anywhere, and never sent anywhere
else.

`coverageTarget` (default 99.5%) is what counts as a finished scrape — the last
handful of members can cost as much as the first ninety percent, and Discord's
member count is itself approximate. Whatever is actually reached is what gets
reported: the scan window says which path ran, and *"found by the gateway"* and
*"loaded in this client"* are deliberately different sentences, because they are
different claims.

## Two backends

A scan asks **one** service. They are alternatives rather than layers.

| | Rotector | Okappiki |
|---|---|---|
| Requests for 10,000 members | ~218 | 10,000 |
| Batch endpoint | 100 ids per request | none — one request per member |
| Concurrency | 3 | 4, capped at 5 |
| Sources in the answer | Rotector | Okappiki's own list, Rotector, and mococo |

**Okappiki is reachable, contrary to what earlier versions of this file said.**
`https://okappiki.com/backend/api.php` answers `vencord_full_check` and
`vencord_check_flag` with HTTP 200 and `access-control-allow-origin: *`. The
earlier "no CORS" conclusion came from probing an *invalid* action, whose error
path returns before the headers are written; the real actions are fine. Choosing
Okappiki is what makes triage rules `R2`, `R3` and `R6` able to fire at all,
because they need a second and third database to have answered.

Three things about it are not preferences:

- **One request per member.** A 10,000-member server is 10,000 requests where
  Rotector would take about 218. The scan window says so before the button.
- **Concurrency 4, capped at 5.** The Python project measured the curve:
  throughput plateaus at 4–5 and *falls* at 6 while latency climbs.
- **Every Okappiki request costs Okappiki a live Rotector lookup out of their
  budget**, so spreading a scan over more connections is not a tuning knob, it
  is the rate-limit circumvention Rotector's terms prohibit.

Only Rotector's flag type can reach `THREAT`. Okappiki's and mococo's sightings
cap at `CAUTION` by construction: neither publishes a claim that its sighting is
safe to act on.

Because there is no batch endpoint, **`autoLookup` is ignored while the backend
is Okappiki**. Under Rotector a scrolled member list costs one request per
hundred rows; under Okappiki it would be one per row, four at a time, each one
spending somebody else's budget. Scrolling a member list is not a decision to
scan a server, so under Okappiki nothing is looked up until you ask for a scan
or open a report.

## The queue and the budget it splits

Scans are jobs. `maxConcurrentJobs` (default 2) decides how many run at once —
and that is a concurrency limit only. **The rate limiter is shared per backend**,
because Rotector's terms forbid working around the window and a limiter per scan
would multiply the request rate by however many scans were queued. Two scans on
one backend therefore each go at roughly half speed; the queue says so on screen
rather than leaving you to wonder whether it has stalled.

Jobs outlive the window that started them, so closing the scan modal does not
stop a scan. The queue — `/rotector queue`, or the server context menu — is
where a running scan is stopped, and disabling the plugin stops all of them.

## Acting on a finding

Select rows in the ledger and use **Kick** or **Ban**. Nothing is sent until the
dialog's own confirm step, and two rules are enforced rather than left to you:

- **Every audit-log reason carries the appeal route**, including a reason you
  typed yourself. Rotector's terms require that an action taken on their data
  attributes them or links rotector.com so the affected person can appeal. You
  can add an appeal route of your own (`appealInvite`, `appealContact`,
  `appealNote`) — it is added to Rotector's, never instead of it.
- **Only flag types 1 and 2 are documented as safe to action.** Acting on
  anything else is possible and is reported as a deliberate override, named as
  one, in front of you. `requireThreat` gates it; `allowCaution` decides whether
  `CAUTION` findings may be actioned at all, and each of those needs its own
  second confirmation.

Bulk actions are spaced by `bulkDelay` (default 1s): bulk kicks and bans are the
fastest way to trip Discord's abuse heuristics, and the pause costs nothing next
to having the account flagged. Permissions are checked first and missing ones
are stated plainly instead of firing requests that will 403.

## Exports

CSV (split into numbered parts by `exportSegmentSize` so every part still opens
in a spreadsheet), TXT, JSON, a styled HTML report, and a PNG drawn from the
project's own palette. Verdict colours never follow your theme even with
`exportFollowTheme` on — a verdict hue that changes with a theme stops being
evidence.

**Every file carries the attribution line and states its own expiry, and
deleting them is yours to do.** The terminal app sweeps its own export folder;
a plugin cannot reach your downloads folder, so the rule is carried by a notice
in every file rather than enforced. That notice is the 24-hour expiry the terms
set — or, if you switched `retainBeyondTerms` on, how long this file is actually
being kept *and* what the terms allow, because repeating a rule the run did not
follow would be theatre. Every format also carries a `Stale` column and dates
each finding that was already past the window when the file was written.

## Purging your own messages

From a user, channel, group DM or message context menu, or `/rotector purge`.
**Only your own messages** — Discord provides no way to remove someone else's
from a DM, and the plugin does not pretend otherwise. It runs in two phases,
always in this order: it reads the history and shows you exactly what would go,
deleting nothing; only then, behind a typed confirmation, does it delete,
spaced by `purgeDelay`. Message deletion cannot be undone.

The DM entry appears only when a DM with that person already exists. It is a
lookup, never an open: opening a conversation in order to delete one that never
happened is not something this will do.

## Vibe mode

Off by default. A small player for a scan that takes half an hour: the library
is `mireyacs/openlofi`'s `index.json` and nothing is bundled or written to disk.
Audio is fetched over `connect-src` and played from a blob URL, because
`media-src` cannot be granted through `VencordNative.csp.requestAddOverride`
(Equicord's wildcard covers it, Vencord's allowlist does not) — so on Vencord
`raw.githubusercontent.com` may need the host-permission grant the player
offers. Each track is released from memory when it ends. Licences are shown,
because royalty-free is not the same as free of obligations. The barcode
visualiser is gated on the plugin's `motion` setting *and* on
`prefers-reduced-motion`.

## Optional: the client to match

`theme/` holds two Discord themes — `ten-thousand.theme.css` and
`ten-thousand-light.theme.css` — that restyle Discord itself into the same
language, so the client, the plugin's panels and the project's page read as one
surface. Install one of them through **Settings → Themes**; see
`theme/README.md`. Neither the plugin nor the theme needs the other.

## What is still not here

- **No proxies.** They exist in the terminal app and do not belong in a client
  mod. Proxy fan-out in particular is rate-limit circumvention under Rotector's
  terms whichever program does it.
- **Rate-limit headers cannot be read.** Rotector exposes only
  `X-Token-Expires` cross-origin, so `X-RateLimit-Remaining`, `-Reset` and
  `Retry-After` are invisible to a script no matter what the server sends. The
  limiter is therefore purely local — a sliding window sized from your settings
  — and because `Retry-After` is one of the headers it cannot read, a 429 is
  answered by holding *every* caller for one full window and then retrying, a
  flat wait rather than an escalating one — the window is the same length every
  time, so there is nothing to escalate. (The exponential backoff, capped at 15
  seconds, is what a 5xx gets instead: a server that is failing is worth backing
  away from progressively.) Retries of both kinds are capped, and a lookup that
  runs out of them raises rather than returning empty, because an empty result
  would read as a clean bill of health for everyone in the batch. This is the
  one place the port diverges from the terminal app, which syncs against those
  headers and honours the exact `Retry-After` they carry.

  One local window covers everything the plugin sends to Rotector, the status
  panel's connectivity check included — a diagnostic that spent units nothing
  else was counting would be the same overspend by a politer name.

## Findings are dropped within 24 hours

Rotector's Terms of Use forbid retaining their responses beyond 24 hours. There
are exactly two places a finding is held, and by default both are under one
ceiling of 23 hours — enforced when the data is written, checked again on every
single read, and swept on a timer, so a frozen timer or a laptop asleep for
eight hours cannot make the plugin serve something it should already have
dropped. One setting moves that ceiling and it is off; see *Keeping findings
longer* below.

**The lookup cache** is in memory only. Nothing about it reaches your disk or
your settings file, it is hard-clamped to 23 hours whatever `cacheTtl` you
configure, and it goes entirely when Discord is closed or the plugin is
disabled. **Purge cached findings** in the plugin's settings clears it
immediately.

**The scan history** is the one thing that is written down: when a scan
finishes, a record of it goes into a small IndexedDB database of this plugin's
own, so you can still read what a scan found after restarting Discord. That is
allowed — the terms forbid keeping their responses past 24 hours, not writing
them down inside that window — and the record is deleted 23 hours after the scan
*started*, not after it was saved. Only the findings are stored: the members who
came back with nothing are counted and dropped, and the record says how many, so
a short list is never mistaken for a short scan. `historyEnabled` turns the
whole thing off, `historyLimit` caps how many scans are kept (never how long),
and **Purge stored scans** — in the settings panel or in the history window —
deletes the database. Neither of those two settings can raise the ceiling —
they answer *how many*, never *how long*. Exactly one setting answers the second
question, and it is the next section.

Reach the history from `/rotector history`, the server context menu, the scan
window's footer, or the plugin's settings.

### Keeping findings longer

`retainBeyondTerms` lifts that ceiling. **It is off, it ships off, and leaving
it off is the only setting that keeps the plugin inside Rotector's Terms of Use
#1.** Switching it on means both stores hold findings for `retentionHours`
instead — 168, a week, by default, and a year at most — which is a decision to
keep somebody else's data outside the terms you agreed to. Both settings take
effect on the running client: switching retention back off drops everything past
23 hours on the next read rather than waiting for a restart.

It is not only a licensing question, and this is the part worth reading twice.
Flag types change and appeals succeed. An account that cleared its violations
yesterday still reads as flagged out of a cache that was told never to expire,
and a moderator acting on that record is acting on something that is no longer
true of the person it names. So **anything answered more than 24 hours ago is
labelled stale, with its age, everywhere it is shown or written** — whether or
not you switched the setting on, because results left on screen for two days go
stale by themselves:

- the mark's tooltip and the report say how old the answer is and that a
  re-check is due before acting on it;
- the scan ledger and the scan history mark the row `STALE 3d`, and the summary
  counts how many rows are stale beside the count of lookups that did not
  answer;
- the status panel names the ceiling actually in force, says plainly when it is
  the lifted one, and counts how much of what is held is already past the
  window;
- the kick and ban confirmation names every member whose finding is stale, with
  its age, before you send anything — that is the one action here that cannot be
  undone by re-checking afterwards;
- every export carries a `Stale` column (blank for findings inside the window),
  states its own retention in its header and its footer, and — when it was
  written past the terms — says so and still names them, because whoever is
  handed the file did not make that choice.

That labelling does not follow the setting and cannot be switched off. The
setting decides how long a finding is kept; it never decides how old it reads.

The other terms bind you the same way they bind the terminal app:

- **Attribute Rotector** for any action you take on this data, or link
  [rotector.com](https://rotector.com) so people can appeal. The attribution
  line and the appeal link are in the report and in the scan window's footer,
  and they are never what gets trimmed.
- **`Unflagged` must never be presented as "Safe."** Hence `NO DETECTIONS`.
- **No reselling, republishing or bulk scraping**, and no working around rate
  limits with multiple keys or rotating IPs.
- Flag statuses are informational, with no guarantee of accuracy. Anything you
  do with them is on you.

## Cautious, or ban? — the triage line

A verdict grades the *evidence*; it does not tell you what to do. So alongside
it the report shows a second answer from an ordered table of rules, evaluated
top to bottom, first match wins — and it names the rule that fired, so any
recommendation can be looked up and argued with.

| Rule | Condition | Recommends |
|---|---|---|
| `R0` | No database answered. | `NOTHING KNOWN` |
| `R1` | Rotector reports Flagged (1) or Confirmed (2). | **`BAN SUPPORTED`** |
| `R2` | Rotector flagged at an inconclusive type **and** another database independently flagged the same account. | **`BAN SUPPORTED`** |
| `R3` | Both unverified databases flagged, and the mococo score meets your threshold. *Off unless you switch it on.* | **`BAN SUPPORTED`** |
| `R4` | Flagged by real evidence, but nothing that supports acting alone. | `CAUTION` |
| `R5` | Flagged only as Queued (3), Past Offender (6) or Redacted (8). | `INFORMATIONAL` |
| `R6` | Nothing flagged, but a database did not answer. | `RECHECK` |
| `R7` | No database knows of a linked Roblox account. | `NOTHING KNOWN` |
| `R8` | Every database answered, none flagged. | `NO FINDING` |

`R2`, `R3` and `R6` need a second and third database to have answered, so they
fire only when the backend is **Okappiki**, whose single response carries
Okappiki's own list, Rotector's verdict and mococo's sightings together. Under
Rotector alone there is no second source: `R2` and `R3` have nothing to
corroborate with, and a database that did not answer means *no* database
answered, which `R0` catches first. The rules are shown under both backends,
because a table that quietly dropped the rules it could not reach would be lying
about what it checked, and the two settings behind `R3` say which backend they
apply to.

`R0` is also where a half-finished lookup lands, and that is the point of it.
A check takes two requests — who is linked to this Discord account, then what
is flagged about those Roblox accounts — and if the second one fails, the
linked accounts are known but their flag status is not. That is a check that
did not happen, not a member with nothing against them, so it is reported as
one and the plugin retries it rather than filing it under `NO FINDING`.

## Settings worth knowing about

| Setting | Default | What it does |
|---|---|---|
| `apiKey` | empty | Raises your rate limit on this one connection. |
| `autoLookup` | visible | Whether members are looked up as they appear, and whether message authors count. `off` means nothing is checked until you ask. |
| `minVerdict` | INFO | The lowest verdict that earns a mark. Below it, nothing is drawn — but it is still counted. |
| `showPending` | off | Draw a hairline outline while a lookup is in flight. |
| `cacheTtl` | 3600s | How long a lookup is reused. Clamped under 24 hours unless `retainBeyondTerms` is on. |
| `retainBeyondTerms` | **off** | Keep findings past the 24-hour window Rotector's terms allow. Off, and leaving it off is what keeps the plugin inside those terms. Anything past the window is labelled stale either way. |
| `retentionHours` | 168 (a week) | How long findings are kept once the setting above is on. Ignored while it is off; a year is the most either client will hold. |
| `rateLimit` / `window` / `reserve` | 50 / 10s / 5 | The local limiter. `reserve` is the only defence against another client on the same IP spending the same budget, because the server's own headers are unreadable here. |
| `motion` | off | Lets the stripe fields drift. Ignored when your system asks for reduced motion. |
| `backend` | rotector | Which service a scan asks. Okappiki adds two more sources and costs one request per member. |
| `okappiki*` | 6 / 1s / 0 / 4 | Okappiki's own limiter. Separate keys from Rotector's on purpose: the two budgets are not comparable numbers. |
| `gatewayEnabled` / `gatewayToken` | off / off | The member scanner, and which token it uses. A bot token has no Terms problem; your own account does. |
| `botToken` | empty | Needs **SERVER MEMBERS INTENT** on, or Discord closes the connection with 4014. |
| `coverageTarget` | 99.5% | What counts as a finished scrape. Whatever is reached is what gets reported. |
| `maxConcurrentJobs` | 2 | How many scans run at once. It splits one shared budget; it does not add one. |
| `onRescan` | ask | What starting the same scan twice on the same backend means. |
| `requireThreat` / `allowCaution` | on / on | Which findings a kick or ban is offered for. Anything else is an explicit override. |
| `defaultReason` | `Flagged by Rotector: {reason}` | The audit-log reason. The appeal link is always appended. |
| `bulkDelay` | 1s | Between members in a bulk action, so a bulk kick does not look like an attack. |
| `exportScope` / `exportSegmentSize` | filtered / 1000 | What an export contains and how many rows per CSV part. |
| `purgeDelay` / `purgeMaxMessages` / `purgeMaxAgeDays` | 1s / 0 / 0 | What the purge screen pre-fills. All three are editable before the preview runs. |
| `vibeEnabled` | off | The player in the scan window. |
| `historyEnabled` | on | Whether a finished scan is written down so it survives a restart. Records are deleted after 23 hours either way, unless `retainBeyondTerms` moves that ceiling. |
| `historyLimit` | 25 | How many past scans are kept, oldest deleted first. A cap on how many, never on how long. |

Everything is under **Settings → Plugins → RotectorScan**, and the status panel
at the bottom of that page names your client, checks whether the API is actually
reachable, and shows exactly how much is currently held — in memory, and in the
scan history on this computer — with a purge button for each. It also names the
retention ceiling in force, says plainly when that is the lifted one rather than
the default, and counts how many held findings are already past the 24-hour
window.

Data: [Rotector](https://rotector.com). Anyone listed can appeal at
[rotector.com](https://rotector.com).
