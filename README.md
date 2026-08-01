# rotector-selfbot

A terminal UI that lists your Discord **servers, friends, incoming friend
requests and group DMs**, reads the members of whichever you pick, and checks
every one against the [Rotector](https://rotector.com) database — surfacing
accounts with detected violations so you can tell who is a potential threat
before interacting.

```
 SERVERS                     │ RESULTS - Test Server A
 Server            Members   │ 45 scanned   THREAT 2  UNKNOWN 43     filter: All
 Test Server A          45   │ Member          Verdict   Flag        Category  Roblox      Srv
 Another Server          3   │ suspicious_guy  THREAT    Confirmed   Condo     Messipel90    2
                             │ User 5          THREAT    Confirmed   Sexual    Davicukis58   -
                             │ ─────────────────────────────────────────────────────────────
                             │ suspicious_guy  @user1  id 1
                             │ THREAT - Flagged or Confirmed violations. Safe to action per Rotector.
                             │ Linked Roblox accounts (1)
                             │   Confirmed (actionable)  Messipel90 (8423569713)  category Condo  confidence 1.00
                             │     detected via Profile - https://www.roblox.com/users/8423569713/profile
                             │     Condo Activity: [Trap3] Entered a condo game via unguessable entry code 8 time(s)
                             │       - Game: furre (v4 fork) (3 clicks)
 Scanned 45 members - 2 flagged as THREAT. Data: Rotector …   |   budget 43/45 (server: 44)
```

## Read this first

**Automating a user account is against Discord's Terms of Service**, whatever
the account is used for. Discord does not carve out an exception for
safety tooling, and accounts caught driving the API this way get disabled.
This tool only ever *reads* — it lists guilds, reads the member sidebar, and
looks ids up — but that is still automation of a user account, and the risk of
losing the account is real and entirely yours. If you have the option, a
regular bot with the `GUILD_MEMBERS` intent does the same job with no ToS
problem; the Rotector half of this codebase works unchanged either way.

**A verdict is not a background check.** `NO DETECTIONS` means Rotector has
not flagged the account *yet* — it is not a clean bill of health, and Rotector's
own terms forbid presenting it as "safe". Only `THREAT` (flag types *Flagged*
and *Confirmed*) is documented as safe to act on automatically.

## Install

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

## Configure

Settings get added between versions, and a config written against an older one
silently falls back to defaults for anything it does not mention. To bring an
existing file up to date:

```bash
./.venv/bin/python -m rsb migrate --dry-run   # list what is missing
./.venv/bin/python -m rsb migrate             # add it
```

It only *appends* what is absent, with each setting's explanatory comment. Your
values, comments and layout are untouched, nothing is reordered, and the
previous file is kept as `config.toml.bak`. Running it twice changes nothing.


```bash
cp config.example.toml config.toml   # then put your token in it
```

Or use the environment, which takes precedence:

```bash
export DISCORD_TOKEN='...'
export ROTECTOR_API_KEY='...'        # optional
```

`config.toml` is gitignored. The token is a full-access credential — treat it
like a password.

An API key is optional: without one you get the standard tier, which is enough
for normal use. Request one at [panel.rotector.com](https://panel.rotector.com)
for elevated limits.

## Run

```bash
./.venv/bin/python -m rsb            # the scanner
./.venv/bin/python -m rsb proxies    # the proxy tester
```

| Key | Action |
|-----|--------|
| `↑` `↓` | Move between servers / results |
| `s` / `enter` | Scan the selected source |
| `f` | Cycle filter: Findings → Threats → Caution+ → Tracked servers → Everything |
| `/` | Search by name, Discord id, or linked Roblox username |
| `e` | Export — opens a dialog for formats, scope and columns |
| `c` | Copy the selected member's full findings to the clipboard |
| `p` | Purge your own messages in the conversation with them |
| `o` / `O` | Sort by next column / reverse — or click a header |
| `k` / `b` | Act on the selected member (meaning depends on the source) |
| `x` | Stop a running scan |
| `L` | Leave the selected group DM |
| `[` `]` | Narrow / widen the sources pane (or drag the divider) |
| `ctrl+r` | Reload the source list |
| `q` | Quit |

The divider between the two panes is **draggable** — grab it with the mouse, or
use `[` and `]`. It clamps so neither pane can be squashed out of existence.

Highlighting a row fills the detail pane with every linked Roblox account, its
flag type, category, confidence, and the full reason text and evidence Rotector
returned.

### What gets listed

By default the table lists **findings only**. In a real server the
overwhelming majority of members have nothing against them, and listing all of
them buries the few that matter — a scan of 45 test members lists 2 rows, not
45. Two groups are hidden:

- `NO DETECTIONS` — Rotector has not flagged them.
- `UNKNOWN` — no Rotector-known Roblox account at all. In practice this is the
  larger group by far, so hiding only the first would not have solved much.

Nothing is discarded. Full counts stay in the summary line, which also says how
many are hidden (`filter: Findings  (43 hidden)`), `f` cycles to `Everything`,
and exports always contain every scanned member. Either group can be brought
back permanently with `hide_no_detections` / `hide_unknown` in `config.toml`.

### The status bar

The bottom line always names the step currently in flight, with a spinner and
an elapsed clock once a step passes ~1.5 s:

```
/ Opening member list via #general (channel 2/6)  7s              |  budget 43/45 (server: 44)
\ Reading #general members 400-599 - 380 / 1,204 members  4s left |  budget 41/45
| Looking up Discord accounts - 300 / 1,204  ETA 1m 20s (14/s)    |  routes 4/5   budget 38/45
/ Checking 87 linked Roblox accounts for flags - 900 / 1,204      |  holding for rate limit window, 4.2s
```

Every long step announces itself *before* it blocks, not after it finishes.
That matters most when opening the member list: a channel that exposes no
sidebar costs a full 6 s timeout before we learn that, and with several such
channels the app used to sit on one stale message for half a minute. The right
of the bar shows the live Rotector budget, and says so explicitly when the
limiter is holding for a window to roll over.

CLI flags (`--token`, `--api-key`, `--rate-limit`, `--window`, `--reserve`,
`--max-members`, `--include-bots`) override the config file. See `-m rsb --help`.

## What you can scan

The left pane lists four kinds of source:

| Source | Members from | Coverage |
|--------|--------------|----------|
| **Incoming friend requests** | one REST call | complete |
| **Friends** | one REST call | complete |
| **Group DMs** | the channel's recipient list | complete |
| **Servers** | the gateway member list | best effort — see below |

Friend requests are listed first: someone who has just added you is exactly who
you want checked *before* deciding, and the list is short. Blocked users and
your own outgoing requests are not listed; 1:1 DMs are not either, since their
recipient is normally a friend already.

Friends and group DMs come back whole from a single request — no sidebar, no
permission questions, no coverage caveat. They are also near-instant.

## How a scan works

Rotector's Discord endpoint does not itself return a flag status, so a verdict
takes two hops:

1. `POST /v1/lookup/discord/user` — up to 100 Discord ids per call, returning
   each user's tracked server memberships and linked Roblox accounts.
2. `POST /v1/lookup/roblox/user` — the Roblox ids that surfaced, returning flag
   type, category, confidence and reasons.

Hop 2 runs per batch rather than after the whole guild, so results stream into
the table while the scan is still going.

The two phases are **one pipeline, not two steps**. Members are pushed into the
Rotector lookups the moment the gateway reveals them, rather than after the
whole member list has loaded — so findings appear while reading is still in
progress, and the two phases overlap instead of running back to back. The
status bar says `still reading members...` until the list is complete, at which
point the ETA becomes meaningful.

Members themselves come from the gateway, the same way the real client reads
them: **OP 14** subscribes to slices of the member sidebar and the server
answers with `GUILD_MEMBER_LIST_UPDATE`.

### Reading *all* the members

A member the sidebar never shows is a member never checked, so coverage is
treated as the priority:

- **Channels `@everyone` can view are always tried first.** A sidebar only
  lists members who can see its channel — scrape a staff-only channel and you
  get a partial member list with no indication anything is missing. Visibility
  is resolved the way Discord resolves it: the `@everyone` role's base
  permissions, then the category's overwrite, then the channel's own. A single
  overwrite lookup is not enough, and getting this wrong silently excludes
  people.
- **Results are unioned across channels, not replaced**, so anyone visible
  through only one channel is still picked up.
- **If the union still falls short** of the guild's member count, the **OP 8**
  search (`REQUEST_GUILD_MEMBERS`) sweeps for the remainder and is unioned in
  too — an open query where permissions allow, otherwise a prefix scan.

If no channel is visible to `@everyone` at all, the status bar says so, because
the resulting list may be partial through no fault of the tool.

> **Coverage caveat, inherited from Discord:** in large guilds the member
> sidebar only lists non-offline members. A scan covers who is *visible*, which
> in a big server is not everyone. The member count in the status line tells you
> what was actually read.

Draining gateway events uses an overall deadline rather than a per-event
timeout, and each scraper subscribes only to the dispatch types it consumes. A
live account receives unrelated traffic (presence, messages, typing) from every
server it is in; with a per-event timeout that traffic resets the clock forever
and the scrape never completes. Only a member-list update for the guild being
scanned may extend a wait, and never past a hard per-round cap.

If no channel exposes a sidebar, the OP 8 prefix sweep stops early as soon as
the guild's member count is accounted for, rather than always running all 38
prefixes.

### Verdicts

| Verdict | Flag types | Meaning |
|---------|-----------|---------|
| `THREAT` | Flagged (1), Confirmed (2) | Detected violations. The only tier documented as safe to action. |
| `CAUTION` | Provisional (4), Mixed (5) | Some signal, insufficient evidence. Review manually. |
| `INFO` | Queued (3), Past Offender (6), Redacted (8) | Informational. **Not** an accusation. |
| `NO DETECTIONS` | Unflagged (0) | Nothing detected *yet*. Not "safe". |
| `UNKNOWN` | — | No linked Roblox account known to Rotector. |

A member with several linked accounts takes the worst verdict among them.

Tracked-server membership is shown as its own column and in the detail pane,
but deliberately does **not** feed the verdict — being in a monitored server is
a signal worth seeing, not a finding about the person.

## Exports

`e` opens a dialog: which formats, what scope, how to segment, and which of the
17 columns to include.

**Scope defaults to the current filter.** If you have narrowed the table to
threats, that is what gets written — exporting everyone when you asked for a
subset is both surprising and hands on far more personal data than you wanted.
`Everything scanned` is one radio button away.

Each export lands in its own dated folder:

```
exports/My-Server-20260801T120000Z/
  My-Server-20260801T120000Z.part1-of-3.csv
  My-Server-20260801T120000Z.part2-of-3.csv
  My-Server-20260801T120000Z.part3-of-3.csv
  My-Server-20260801T120000Z.txt
  README.txt
```

- **CSV** — segmented at `segment_size` rows (default 1000). Every part is a
  complete CSV with its own header, so each opens on its own; a 40,000-member
  scan becomes 40 files a spreadsheet will actually load rather than one it
  won't.
- **TXT** — a readable per-member report rather than a table, for reading or
  pasting into a ticket.
- **JSON** — the full structured data.
- **README.txt** — what this is, the scope it was taken at, and the 24-hour
  expiry.

Ticking *Remember these settings* writes them to `[export]` in `config.toml`.
Only that section is rewritten — comments elsewhere in the file survive.

### Sorting

Click any column header in either table to sort by it; click the same header
again to reverse. `o` moves to the next column on whichever table has focus and
`O` flips the direction, so it works without a mouse. The sorted column carries
a `^` / `v` marker in its header.

Results sort worst-verdict-first by default, which is the point of the tool.
Sorting by **Category** groups findings by kind — every Condo case together,
every CSAM case together. Members with no value in the sorted column always
sink to the bottom rather than floating to the top in one direction.

### Retention

Rotector's terms forbid keeping their responses beyond 24 hours, so export
folders older than that are cleared automatically when you next export.
*Preserve older exports* in the dialog (or `export.preserve` in the config)
turns that off and makes retention your responsibility.

The cleanup is deliberately timid, since it deletes files. A folder is only
removed if it sits directly under the export directory, its name carries a
timestamp this tool wrote, **and** its `README.txt` contains our marker line.
Anything else — a folder you made, an unrelated directory, a loose file — is
left alone no matter how old it is.

## Acting on a member

`c` copies the selected member's findings — verdict, every linked Roblox
account, reasons, evidence, profile links — as plain text with attribution.

`k` and `b` act on the member, and mean different things per source — kicking
a friend is meaningless, as is banning someone from a group DM:

| Source | `k` | `b` |
|--------|-----|-----|
| Server | kick | ban |
| Friends | remove friend | block |
| Friend requests | decline request | block |
| Group DM | remove from group | block |

`L` leaves the selected group DM, with a **silent** option that suppresses the
"left the group" message the others would otherwise see. Tick *Remember* and it
becomes the default via `moderation.silent_leave`.

For servers, the reason is either generated from the Rotector finding or
written by you. Both open a dialog showing the exact text
that will land in the audit log. Two things are enforced rather than left to
judgement:

- **Attribution is always appended.** Rotector's terms require that anyone
  actioned on their data can appeal, so `Appeal at rotector.com` is added to
  every reason including your own — and the appeal link is never what gets
  trimmed when a reason is too long for Discord's 512-character limit.
- **Non-actionable findings are refused — for kicks and bans.** Rotector
  documents only *Flagged* and *Confirmed* as safe to action. Anything else,
  including `NO DETECTIONS` and `UNKNOWN`, is blocked by
  `moderation.require_threat`. Turning that off still shows what is wrong with
  the action and still requires an explicit per-action acknowledgement; it
  never becomes routine.

  The gate deliberately does **not** apply to unfriending, blocking, declining
  a request or removing someone from your own group DM. Those restrict nobody's
  access to a community — they are your own boundaries to set, and requiring a
  database's endorsement before you may block someone would be absurd. The
  verdict is still shown as context.

Actioned rows are struck through and tagged `[kicked]` / `[banned]`.

## Purging your messages

`p` deletes **your own** messages from the conversation with the selected
member — a DM, or the group DM you are viewing.

> Only your messages. Discord provides no way to remove someone else's messages
> from a conversation, so their side stays exactly where it is. What this
> removes is your contributions, which is the part you actually control.

The flow is deliberately two-step, because deletion cannot be undone:

1. **Preview.** You choose how far back to look (days, or a cap on messages)
   and the history is read without deleting anything. You then see how many of
   your messages matched, out of how many scanned, with the oldest few shown.
2. **Confirm.** You type `DELETE`. Only then does anything get removed.

Deletion runs oldest-first, so an interrupted purge leaves the recent tail
rather than a random scatter, and is paced by `purge.delete_delay` (default 1s)
because Discord's per-channel delete limit is strict. A single failure is
recorded and skipped rather than aborting the run.

Looking up the DM is a lookup, not a create — if you have never had a DM with
someone, this reports that rather than opening a conversation in order to
delete one that never existed. In a group DM the dialog is explicit that this
removes *everything* you sent to the group, not only messages aimed at one
person.

## Time estimates

Highlighting a server shows a predicted scan time before you commit to it,
derived from its member count and the request budget across every usable route:

```
Estimated scan time  4m 38s
across 5/5 usable routes
```

The prediction covers the Rotector lookups only — reading the member list from
the gateway happens first and is not included, since its duration depends on
Discord rather than on any budget. The Discord hop is exact (one lookup per
member); the Roblox hop assumes a typical share of members have a linked
account, because that is unknowable up front.

Once a scan starts, a live `ETA` replaces it, measured from actual throughput
over a trailing window rather than a whole-run average. That matters: after ten
fast minutes, a whole-run average would barely notice a stall, whereas the
windowed estimate reflects a rate-limit hold or a dead proxy within seconds.

## Proxies

> **Rotector's Terms of Use prohibit "circumventing rate limits through
> multiple keys, rotating IPs, or other means",** and the stated penalty is
> revocation of API access. Spreading a scan across proxy IPs is exactly that.
> The sanctioned way to go faster is an **API key** from
> [panel.rotector.com](https://panel.rotector.com), which raises the limit on a
> single connection — you can suggest the limit you need when requesting one.
>
> Proxy routing is therefore **off by default**. Turning it on is your call.

Because the rate limit is scoped per IP, each route carries its own budget and
capacity adds up. Enable with `proxy.enabled = true`, or `--proxies` for a
single run.

Extra routes only help if there is more than one request in flight to put on
them, so batches are dispatched **concurrently**, with worker count sized from
the pool. Measured against a mock API enforcing a real per-route rate limit,
behind real local proxies:

| Routes | 12,000 ids | |
|--------|-----------|---|
| 1 (direct only) | 10.5s | |
| 4 (direct + 3 proxies) | 2.1s | **5.1× faster** |

Both runs were genuinely rate limited, so that ratio reflects capacity actually
scaling rather than the work simply fitting inside one window.

### The proxy tester

```bash
./.venv/bin/python -m rsb proxies
```

| Key | Action |
|-----|--------|
| `t` | Test every proxy |
| `r` | Retest the highlighted row |
| `a` | Add a proxy |
| `d` | Remove the highlighted row |
| `s` | Save working proxies back to `proxies.txt` |
| `x` | Stop testing |

Every proxy is tested **against the Rotector API itself** — no third-party IP
echo service is involved, so what is measured is exactly what the scanner
depends on. A proxy that reaches the internet but gets a 403 from Rotector is
worse than useless, and shows as `NO API` rather than `OK`.

Whether a proxy brings its own rate budget is read from Rotector's own
`X-RateLimit-*` headers. The probe is a single request, so an exit reporting
more than one request already spent in the current window is sharing that
budget — with another proxy in the pool behind the same exit, or with unrelated
traffic from that IP. Those are marked `SHARED`: they look like extra capacity
but are not. The header line totals the pool's real combined budget.

Your own connection is listed as `direct` and tested alongside the proxies.

Every form proxy vendors hand out is accepted: `host:port`,
`user:pass@host:port`, `host:port:user:pass`, and `scheme://…` for http, https,
socks4 and socks5. Credentials are stripped from anything displayed.

### Failover and halting

Routing is deliberate about failure:

- Healthy proxies are preferred, ordered by whichever has the most rate-limit
  headroom, so work spreads instead of hammering the first entry.
- A route is parked on its **first** failure with escalating backoff (5s, 10s,
  20s… capped at 5 minutes), and the request retries elsewhere immediately.
  Parking on the first strike rather than the third matters: a proxy that
  *hangs* costs a full connect timeout every time it is tried.
- A `429` is backpressure, not a fault, and costs a route no health.
- Proxy-shaped failures — `403`, `407`, `502`, `503`, or a non-JSON body from an
  intercepting proxy — park the route rather than failing the scan.
- Your own connection is a **co-equal route** by default — it carries work
  alongside the proxies, so its budget is used too. Set
  `direct_as_fallback = true` to hold it back as a standby instead.
- **If every route including your own connection fails, the scan halts** and the
  detail pane names each route with its own error, plus what it means — rather
  than a single "network error". Parked routes are retried after backoff, so
  scanning again later may simply work.

The status bar shows `routes 4/5` whenever proxies are configured.

## Rate limiting

The documented budget is **50 requests per 10 seconds, per IP** (per key with an
API key). Batch endpoints cost more than one request; measured against the live
API the cost is `ceil(ids / 50)`, so a 100-id batch costs 2 units.

Three layers keep the client inside that window:

1. **Local sliding window** — never spends more than `limit - reserve` units in
   any 10 seconds. This is what actually paces requests.
2. **Header sync** — every response carries `X-RateLimit-Remaining` and
   `X-RateLimit-Reset`. If the server has a tighter view than we do (another
   process on the same IP, clock skew), its view wins and the client stops until
   the window resets.
3. **`Retry-After`** — honoured exactly on a 429, blocking every in-flight task.

The live budget is shown in the status bar the whole time. Verified against the
real API: 6,001 ids in 62 requests over 42 s, zero 429s, with the throttle
visibly stalling at window boundaries.

Tune `reserve` in `config.toml` — raise it to be more conservative, lower it for
speed.

## Rotector Terms of Use

The client is built to honour them, but they bind *you*:

- **Responses may not be kept longer than 24 hours.** The cache is in-memory
  only and clamped below that ceiling; nothing is written to disk except when
  you press `e`. Exports are stamped with an expiry — delete them.
- **Attribute Rotector** for any action you take on this data, or link
  [rotector.com](https://rotector.com) so people can appeal. The attribution
  line is in the UI and in every export.
- **`Unflagged` must never be presented as "Safe."** Hence `NO DETECTIONS`.
- **No reselling, republishing, or bulk scraping**, and no working around rate
  limits with multiple keys or rotating IPs. The [proxy support](#proxies) in
  this tool is capable of exactly that, which is why it ships disabled; an API
  key is the sanctioned way to raise throughput.
- Flag statuses are informational, with no guarantee of accuracy. Anything you
  do with them is on you.

## Tests

```bash
./.venv/bin/python tests/run_all.py            # everything
./.venv/bin/python tests/run_all.py --offline  # no network
```

- `test_units.py` — no network: sidebar range maths, `GUILD_MEMBER_LIST_UPDATE`
  op folding, channel permission checks, flag→verdict mapping, batch cost, and
  the limiter's throttling, header-sync, `Retry-After` and concurrency paths.
- `test_gateway_scrape.py` — no network: drives the real scrape logic against a
  deliberately noisy gateway. Asserts a small guild still scrapes promptly while
  unrelated dispatches stream in, that a silent channel still gives up, and that
  filtered subscriptions drop irrelevant events. Reverting either fix makes this
  suite hang, which is the bug it exists to catch.
- `test_purge.py` — no network: that planning collects only your own messages
  and deletes nothing, pagination and both limits, oldest-first deletion order,
  survival of individual failures, mid-run stopping, and that the DM lookup
  never opens a conversation that did not exist.
- `test_sorting.py` — every column of both tables sorts by click and by
  keyboard, direction toggles, blanks sink, and no row is lost or duplicated.
- `test_layout_retention.py` — pane resizing and clamping, leaving a group DM
  (silently, remembered, and removed from the list), the export cleanup's safety
  rules, and config migration: that user values and comments survive, every
  schema setting ends up present, and a second run is a no-op.
- `test_sources.py` — friends/requests/group DMs end to end against the live
  API: that blocked users and outgoing requests are excluded, 1:1 DMs are not
  listed as groups, scanning needs no gateway, and that `k`/`b` resolve to
  unfriend/block/remove-from-group and hit the right endpoint.
- `test_export.py` — no network: CSV segmentation (2,500 rows → 3 self-contained
  parts, all rows preserved), TXT/JSON/README contents, column selection, and
  config round-tripping that proves comments elsewhere in the file survive.
  Also reason construction — attribution never duplicated, never trimmed away,
  512-char limit respected — and that every non-actionable flag type is refused.
- `test_moderation_tui.py` — the kick/ban flow end to end with Discord stubbed:
  that an acknowledgement is genuinely required, that `require_threat` blocks
  even an acknowledged action, that a custom reason keeps its attribution, and
  that a 403 is explained rather than swallowed.
- `test_streaming.py` — a gateway that reveals members in slow waves against the
  live API, asserting on *ordering*: rows must exist before the member list has
  finished loading. Wiring the phases back up sequentially makes it fail with
  "first row appeared 0.24s AFTER reading finished".
- `test_proxy_speedup.py` — no network: a mock Rotector enforcing a real
  per-bucket rate limit, behind real local forward proxies. Asserts every id is
  answered (cross-checked against what the server actually received), that all
  routes including `direct` carry work, that broken proxies lose no queries, and
  that 4 routes are proportionally — not just nominally — faster than 1.
- `test_routing.py` — proxy parsing, route selection, backoff, capacity maths and
  the ETA estimator; then live failover — real dead proxies with a real Rotector
  fallback, and a forced total failure asserting the halt carries each route's
  error.
- `test_proxy_tui.py` — headless run of the proxy tester against a closed local
  port and the live network: probing, verdicts, add/remove, and that saving
  refuses to clobber the list when nothing works.
- `test_progress.py` — no network: asserts the gateway reports before it blocks
  (first update inside 200 ms against a socket that never answers, no silent gap
  longer than the blocking timeout) and that the app renders a changing label
  with an animated spinner throughout a scan.
- `test_live_api.py` — drives the real Rotector API hard and asserts zero 429s.
- `test_tui.py` — headless full-app run against the real API with Discord
  stubbed: scanning, streaming results, filters, search, export, detail pane.

## Layout

```
rsb/
  __main__.py      CLI entry point
  config.py        TOML + env + flags
  ratelimit.py     sliding window, header sync, Retry-After
  proxy.py         route pool, health, failover, proxy probing
  export.py        CSV/TXT/JSON renderers and segmentation
  moderation.py    reason construction and actionability rules
  sources.py       servers, friends, requests and group DMs as one type
  migrate.py       config schema and the upgrade path for older files
  purge.py         planning and deleting your own messages
  eta.py           throughput measurement and time estimates
  rotector.py      API client, batching, two-hop scan
  verdict.py       flag-type semantics and verdict mapping
  discord/
    http.py        REST: identity, guilds, channels
    gateway.py     websocket, OP 14 sidebar scrape, OP 8 fallback
  tui/app.py       Textual UI - scanner
  tui/proxies.py   Textual UI - proxy tester
  tui/dialogs.py   export options and kick/ban modals
```

Data: [Rotector](https://rotector.com).
