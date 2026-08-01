"""Textual UI: pick a server on the left, scan it, read verdicts on the right."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    ProgressBar,
    Static,
)

from ..config import Config
from ..discord import (
    DiscordAuthError,
    DiscordGateway,
    DiscordHTTP,
    GatewayError,
    Guild,
    GuildMember,
)
from ..discord.http import DiscordForbidden, DiscordNotFound
from ..eta import RateEstimator, estimate_scan_seconds, format_duration
from ..export import DEFAULT_COLUMNS, export as render_export, ExportRow
from ..moderation import build_reason, check_eligibility
from .dialogs import ExportDialog, ModerationDialog
from ..proxy import AllRoutesFailed
from ..ratelimit import RateLimiter
from ..rotector import MemberReport, RotectorClient, RotectorError
from ..verdict import (
    ATTRIBUTION,
    Verdict,
    category_name,
    flag_is_actionable,
    flag_name,
    source_names,
    verdict_label,
    verdict_meaning,
    verdict_style,
)


class FilterMode(Enum):
    """What the results table lists.

    ``FINDINGS`` is the default: in a real server the overwhelming majority of
    members have nothing against them, and listing them all buries the few that
    matter. Which verdicts count as noise is configurable -- see
    ``[scan] hide_no_detections`` / ``hide_unknown``.
    """

    FINDINGS = "Findings"
    THREATS = "Threats only"
    ATTENTION = "Caution and above"
    TRACKED = "In tracked servers"
    ALL = "Everything"


_FILTER_CYCLE = list(FilterMode)

#: braille spinner frames; animated whenever an activity is in flight
_SPINNER = "-\\|/"
#: only show an elapsed clock once something has run long enough to worry about
_ELAPSED_AFTER = 1.5


class _ScanAborted(Exception):
    """Raised inside the member sink to stop reading when the scan has died."""


@dataclass
class Row:
    member: GuildMember
    report: MemberReport
    #: "kicked" / "banned" once acted on, for the table to show
    actioned: str | None = None


class ScannerApp(App):
    TITLE = "rotector-selfbot"
    SUB_TITLE = "Discord member safety scanner"

    CSS = """
    Screen { layers: base overlay; }

    #body { height: 1fr; }

    #servers-pane {
        width: 42;
        border-right: solid $panel-lighten-2;
    }
    #servers-pane > .pane-title, #results-pane > .pane-title {
        background: $panel;
        color: $text-muted;
        padding: 0 1;
        text-style: bold;
    }
    #guilds { height: 1fr; }
    #results { height: 1fr; }

    #summary {
        height: auto;
        padding: 0 1;
        background: $panel-darken-1;
    }

    #detail {
        height: 14;
        border-top: solid $panel-lighten-2;
        padding: 0 1;
        background: $surface;
    }

    #search { display: none; }
    #search.visible { display: block; }

    #status {
        height: 1;
        background: $panel;
        color: $text-muted;
        padding: 0 1;
    }
    #progress { height: 1; display: none; }
    #progress.visible { display: block; }

    #boot { align: center middle; height: 1fr; }
    """

    BINDINGS = [
        ("s,enter", "scan", "Scan server"),
        ("f", "cycle_filter", "Filter"),
        ("slash", "search", "Search"),
        ("e", "export", "Export"),
        ("c", "copy", "Copy member"),
        ("k", "kick", "Kick"),
        ("b", "ban", "Ban"),
        ("ctrl+r", "reload_guilds", "Reload servers"),
        ("x", "stop_scan", "Stop scan"),
        ("escape", "close_search", "", ),
        ("q,ctrl+c", "quit", "Quit"),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.http: DiscordHTTP | None = None
        self.gateway: DiscordGateway | None = None
        self.rotector: RotectorClient | None = None

        self.guilds: list[Guild] = []
        self.rows: dict[str, Row] = {}
        self.current_guild: Guild | None = None
        self.filter_mode = FilterMode.FINDINGS
        # verdicts treated as noise; never dropped from the data, only hidden
        self.hidden_verdicts: set[Verdict] = set()
        if config.scan.hide_no_detections:
            self.hidden_verdicts.add(Verdict.NO_DETECTIONS)
        if config.scan.hide_unknown:
            self.hidden_verdicts.add(Verdict.UNKNOWN)
        self.search_term = ""
        self._shown: list[str] = []
        self._status_text = "Starting up..."
        self._status_style = ""
        # Current in-flight action. While set, the status bar animates a
        # spinner and an elapsed clock so a slow step never reads as a hang.
        self._activity: str | None = None
        self._activity_started = 0.0
        self._spinner_frame = 0
        # measures throughput of the phase in flight, for the live ETA
        self._eta = RateEstimator()

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="servers-pane"):
                yield Static("SERVERS", classes="pane-title")
                yield DataTable(id="guilds", cursor_type="row", zebra_stripes=True)
            with Vertical(id="results-pane"):
                yield Static("RESULTS", classes="pane-title", id="results-title")
                yield Static("", id="summary")
                yield Input(placeholder="Filter by name or ID...", id="search")
                yield DataTable(id="results", cursor_type="row", zebra_stripes=True)
                with VerticalScroll(id="detail"):
                    yield Static(self._welcome_text(), id="detail-body")
        yield ProgressBar(id="progress", show_eta=False)
        yield Static(self._status_text, id="status")
        yield Footer()

    def on_mount(self) -> None:
        guilds = self.query_one("#guilds", DataTable)
        guilds.add_column("Server", width=26)
        guilds.add_column("Members", width=9)

        results = self.query_one("#results", DataTable)
        results.add_column("Member", width=28)
        results.add_column("Verdict", width=15)
        results.add_column("Flag", width=17)
        results.add_column("Category", width=10)
        results.add_column("Roblox", width=24)
        results.add_column("Srv", width=4)

        self.set_interval(0.15, self._refresh_status)
        self.connect()

    def _welcome_text(self) -> Text:
        # Built as a Text rather than a markup string: everything rendered into
        # the detail pane goes through this widget, and Rotector reason strings
        # contain literal square brackets ("[Trap3] ...") that a markup parser
        # would choke on.
        text = Text()
        text.append("Pick a server on the left and press ", style="bold")
        text.append(" s ", style="reverse bold")
        text.append(" to scan it.\n\n", style="bold")
        text.append(
            "Members are read from the guild member list over the gateway, then "
            "checked against Rotector in batches of 100, inside the documented "
            "50 requests / 10 s window.\n\n"
        )
        text.append("Verdicts are not a clean bill of health. ", style="bold yellow")
        text.append("NO DETECTIONS", style="bold")
        text.append(" means Rotector has not flagged the account ")
        text.append("yet", style="italic")
        text.append(" - it does not mean the user is safe. Only ")
        text.append("THREAT", style="bold red")
        text.append(
            " (flag types Flagged and Confirmed) is documented as safe to act on "
            "automatically.\n\n"
        )
        text.append(ATTRIBUTION, style="dim")
        return text

    # -- connection --------------------------------------------------------

    @work(exclusive=True, group="connect")
    async def connect(self) -> None:
        self._set_activity("Authenticating with Discord...")
        try:
            self.http = DiscordHTTP(self.config.token or "")
            me = await self.http.me()

            limiter = RateLimiter(
                limit=self.config.rotector.rate_limit,
                window=self.config.rotector.window,
                reserve=self.config.rotector.reserve,
            )
            proxies = self.config.active_proxies()
            self.rotector = RotectorClient(
                api_key=self.config.rotector.api_key,
                limiter=limiter,
                cache_ttl=self.config.rotector.cache_ttl,
                concurrency=self.config.rotector.concurrency,
                proxies=proxies,
                direct_as_fallback=self.config.proxy.direct_as_fallback,
            )

            self._set_activity("Opening gateway connection...")
            self.gateway = DiscordGateway(self.config.token or "")
            await self.gateway.connect()

            name = me.get("global_name") or me.get("username") or "?"
            keyed = "API key" if self.config.rotector.api_key else "no API key"
            routing = f" - {len(proxies)} proxies" if proxies else ""
            self.sub_title = f"{name} - {keyed}{routing}"
            await self._load_guilds()
        except DiscordAuthError as exc:
            self._fatal(str(exc))
        except GatewayError as exc:
            self._fatal(f"Gateway: {exc}")
        except Exception as exc:  # noqa: BLE001
            self._fatal(f"{type(exc).__name__}: {exc}")

    async def _load_guilds(self) -> None:
        assert self.http is not None
        self._set_activity("Loading your server list...")
        self.guilds = await self.http.guilds()
        table = self.query_one("#guilds", DataTable)
        table.clear()
        for guild in self.guilds:
            count = f"{guild.member_count:,}" if guild.member_count else "?"
            table.add_row(
                Text(guild.name, overflow="ellipsis"),
                Text(count, justify="right"),
                key=guild.id,
            )
        table.focus()
        self._set_status(f"{len(self.guilds)} servers. Select one and press 's' to scan.")

    def _fatal(self, message: str) -> None:
        self._set_status(message, "bold red")
        body = Text()
        body.append("Connection failed\n\n", style="bold red")
        body.append(f"{message}\n\n")
        body.append(
            "Check that the token is a current user token and that you are not "
            "behind a proxy Discord blocks.",
            style="dim",
        )
        self.query_one("#detail-body", Static).update(body)

    # -- scanning ----------------------------------------------------------

    def action_scan(self) -> None:
        table = self.query_one("#guilds", DataTable)
        if not self.guilds or table.cursor_row < 0:
            return
        try:
            guild_id = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return
        guild = next((g for g in self.guilds if g.id == guild_id), None)
        if guild is None:
            return
        if self.gateway is None or self.rotector is None:
            self._set_status("Still connecting...", "yellow")
            return
        self.scan_guild(guild)

    def action_stop_scan(self) -> None:
        if self.workers.cancel_group(self, "scan"):
            self._set_status("Scan cancelled.", "yellow")
            self.query_one("#progress", ProgressBar).remove_class("visible")

    @work(exclusive=True, group="scan")
    async def scan_guild(self, guild: Guild) -> None:
        assert self.http and self.gateway and self.rotector
        self.current_guild = guild
        self.rows.clear()
        self._shown.clear()
        self.query_one("#results", DataTable).clear()
        self.query_one("#results-title", Static).update(f"RESULTS - {guild.name}")
        self._update_summary()

        progress = self.query_one("#progress", ProgressBar)
        progress.add_class("visible")
        progress.update(total=100, progress=0)

        try:
            self._set_activity(f"Reading channels of {guild.name}...")
            channels = await self.http.channels(guild.id)
            if not channels:
                self._set_status("No readable text channels in this server.", "yellow")
                progress.remove_class("visible")
                return

            self._eta.reset()

            def member_progress(found: int, total: int | None, note: str) -> None:
                if total:
                    progress.update(total=total, progress=min(found, total))
                if found:
                    seen = f"{found:,}{f' / {total:,}' if total else ''}"
                    plural = "" if found == 1 else "s"
                    self._set_activity(f"{note} - {seen} member{plural} found")
                else:
                    self._set_activity(note)

            # The member list and the Rotector lookups run as one pipeline:
            # members are pushed into the scan the moment the gateway reveals
            # them, so findings appear while the list is still being read
            # instead of after it finishes.
            queue: asyncio.Queue = asyncio.Queue()
            by_id: dict[str, GuildMember] = {}
            cap = self.config.scan.max_members
            truncated = 0
            reading_done = False
            self._eta.reset()

            def scan_progress(stage: str, done: int, of: int) -> None:
                progress.update(total=max(of, 1), progress=done)
                self._eta.update(done)
                if reading_done:
                    tail = f"   {self._eta.describe(done, of)}"
                else:
                    tail = "   still reading members..."
                self._set_activity(f"{stage} - {done:,} / {of:,}{tail}")

            def partial(reports: list[MemberReport]) -> None:
                for report in reports:
                    member = by_id.get(report.discord_id)
                    if member is None:
                        continue
                    self.rows[report.discord_id] = Row(member=member, report=report)
                    self._append_row(report.discord_id)
                self._update_summary()

            scan_task = asyncio.create_task(
                self.rotector.scan_stream(
                    queue, on_progress=scan_progress, on_partial=partial
                )
            )

            def on_members(new_members: list[GuildMember]) -> None:
                nonlocal truncated
                if scan_task.done():
                    # the scan died (e.g. every route failed); stop reading
                    raise _ScanAborted
                for member in new_members:
                    if self.config.scan.skip_bots and member.bot:
                        continue
                    if cap and len(by_id) >= cap:
                        truncated += 1
                        continue
                    by_id[member.id] = member
                    queue.put_nowait(member.id)

            try:
                await self.gateway.fetch_members(
                    guild.id,
                    channels,
                    expected=guild.member_count,
                    on_progress=member_progress,
                    on_members=on_members,
                )
            except _ScanAborted:
                pass
            finally:
                reading_done = True
                queue.put_nowait(None)

            await scan_task

            if not by_id:
                self._set_status(
                    "No members could be read. The member list may be hidden "
                    "for this account in this server.",
                    "yellow",
                )
                progress.remove_class("visible")
                return

            total = len(by_id)

            threats = sum(1 for r in self.rows.values() if r.report.verdict is Verdict.THREAT)
            note = f" ({truncated:,} skipped by max_members)" if truncated else ""
            verdict_note = (
                f"{threats} flagged as THREAT" if threats else "no THREAT verdicts"
            )
            plural = "" if total == 1 else "s"
            self._set_status(
                f"Scanned {total:,} member{plural}{note} - {verdict_note}. {ATTRIBUTION}",
                "bold red" if threats else "",
            )
            self.query_one("#results", DataTable).focus()
        except asyncio.CancelledError:
            raise
        except AllRoutesFailed as exc:
            self._halt_all_routes(exc)
        except RotectorError as exc:
            self._set_status(f"Rotector API: {exc}", "bold red")
        except GatewayError as exc:
            self._set_status(f"Gateway: {exc}", "bold red")
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"{type(exc).__name__}: {exc}", "bold red")
        finally:
            progress.remove_class("visible")

    # -- results table -----------------------------------------------------

    def _passes(self, row: Row) -> bool:
        verdict = row.report.verdict
        mode = self.filter_mode
        if mode is FilterMode.THREATS and verdict is not Verdict.THREAT:
            return False
        if mode is FilterMode.ATTENTION and verdict < Verdict.CAUTION:
            return False
        if mode is FilterMode.TRACKED and not row.report.servers:
            return False
        if mode is FilterMode.FINDINGS and verdict in self.hidden_verdicts:
            # still counted in the summary, just not listed
            return False
        if self.search_term:
            needle = self.search_term.lower()
            haystack = " ".join(
                [
                    row.member.display_name,
                    row.member.username,
                    row.member.id,
                    *(a.username for a in row.report.accounts),
                ]
            ).lower()
            if needle not in haystack:
                return False
        return True

    def _cells(self, row: Row) -> list[Text]:
        report = row.report
        verdict = report.verdict
        worst = report.worst_account

        if report.accounts:
            top = max(report.accounts, key=lambda a: (a.verdict, a.confidence or 0))
            extra = f" +{len(report.accounts) - 1}" if len(report.accounts) > 1 else ""
            roblox = Text(f"{top.username}{extra}", overflow="ellipsis")
        else:
            roblox = Text("-", style="dim")

        name = Text(f"{row.member.display_name}", overflow="ellipsis")
        if row.actioned:
            name.stylize("strike dim")
            name.append(f" [{row.actioned}]", style="bold green")

        return [
            name,
            Text(verdict_label(verdict), style=verdict_style(verdict)),
            Text(flag_name(worst.flag_type if worst else None)),
            Text(category_name(worst.category if worst else None) or "-"),
            roblox,
            Text(str(len(report.servers)) if report.servers else "-", justify="right"),
        ]

    def _append_row(self, discord_id: str) -> None:
        row = self.rows[discord_id]
        if not self._passes(row):
            return
        table = self.query_one("#results", DataTable)
        table.add_row(*self._cells(row), key=discord_id)
        self._shown.append(discord_id)

    def _rebuild_table(self) -> None:
        table = self.query_one("#results", DataTable)
        table.clear()
        self._shown.clear()
        ordered = sorted(
            self.rows.values(),
            key=lambda r: (-int(r.report.verdict), r.member.display_name.lower()),
        )
        for row in ordered:
            if not self._passes(row):
                continue
            table.add_row(*self._cells(row), key=row.member.id)
            self._shown.append(row.member.id)
        self._update_summary()

    def _update_summary(self) -> None:
        counts = {v: 0 for v in Verdict}
        for row in self.rows.values():
            counts[row.report.verdict] += 1

        text = Text(f"{len(self.rows):,} scanned   ")
        if any(counts.values()):
            for verdict in sorted(Verdict, reverse=True):
                if not counts[verdict]:
                    continue
                text.append(
                    f"{verdict_label(verdict)} {counts[verdict]}",
                    style=verdict_style(verdict),
                )
                text.append("  ")
        else:
            text.append("no results yet", style="dim")

        listed = len(self._shown)
        hidden = len(self.rows) - listed
        tail = f"   filter: {self.filter_mode.value}"
        if hidden > 0:
            tail += f"  ({hidden:,} hidden)"
        if self.search_term:
            tail += f'  search: "{self.search_term}"'
        text.append(tail, style="dim")
        self.query_one("#summary", Static).update(text)

    # -- detail pane -------------------------------------------------------

    @on(DataTable.RowHighlighted, "#results")
    def _show_detail(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value if event.row_key else None
        row = self.rows.get(key) if key else None
        if row is None:
            return
        self.query_one("#detail-body", Static).update(self._render_detail(row))

    def _render_detail(self, row: Row) -> Text:
        member, report = row.member, row.report
        verdict = report.verdict
        text = Text()

        text.append(member.display_name, style="bold")
        text.append(f"  {member.tag}  id {member.id}\n", style="dim")
        text.append(verdict_label(verdict), style=verdict_style(verdict))
        text.append(f" - {verdict_meaning(verdict)}\n\n")

        if report.accounts:
            text.append(f"Linked Roblox accounts ({len(report.accounts)})\n", style="bold")
            for acc in sorted(report.accounts, key=lambda a: -int(a.verdict)):
                actionable = (
                    "actionable" if flag_is_actionable(acc.flag_type) else "not actionable"
                )
                text.append("  ")
                text.append(flag_name(acc.flag_type), style=verdict_style(acc.verdict))
                text.append(f" ({actionable})", style="dim")
                text.append(f"  {acc.username} ")
                text.append(f"({acc.user_id})", style="dim")
                if (cat := category_name(acc.category)) is not None:
                    text.append(f"  category {cat}")
                if acc.confidence is not None:
                    text.append(f"  confidence {acc.confidence:.2f}")
                text.append("\n")
                text.append(
                    f"    detected via {source_names(acc.sources)} - {acc.profile_url}\n",
                    style="dim",
                )
                for name, detail in (acc.reasons or {}).items():
                    message = str(detail.get("message", "")).replace("\n", " / ")
                    text.append(f"    {name}: ", style="bold")
                    text.append(f"{message}\n")
                    for evidence in (detail.get("evidence") or [])[:6]:
                        text.append(f"      - {evidence}\n", style="dim")
        else:
            text.append(
                "No Roblox account linked to this Discord user is known to Rotector.\n",
                style="dim",
            )

        if report.servers:
            text.append(f"\nTracked server memberships ({len(report.servers)})\n", style="bold")
            text.append(
                "Servers Rotector monitors. Membership alone is a signal, not a verdict.\n",
                style="dim",
            )
            for srv in report.servers[:12]:
                tags = []
                if srv.is_tase:
                    tags.append("TASE")
                if srv.in_grace_period:
                    tags.append("grace period")
                text.append(f"  - {srv.server_name} ")
                text.append(srv.server_id, style="dim")
                if tags:
                    text.append(f" ({', '.join(tags)})", style="dim")
                text.append("\n")
            if len(report.servers) > 12:
                text.append(f"  ... and {len(report.servers) - 12} more\n", style="dim")

        text.append(f"\n{ATTRIBUTION}", style="dim")
        return text

    # -- actions -----------------------------------------------------------

    def action_cycle_filter(self) -> None:
        index = _FILTER_CYCLE.index(self.filter_mode)
        self.filter_mode = _FILTER_CYCLE[(index + 1) % len(_FILTER_CYCLE)]
        self._rebuild_table()

    def action_search(self) -> None:
        search = self.query_one("#search", Input)
        if "visible" in search.classes:
            search.remove_class("visible")
            search.value = ""
            self.search_term = ""
            self._rebuild_table()
            self.query_one("#results", DataTable).focus()
        else:
            search.add_class("visible")
            search.focus()

    @on(Input.Changed, "#search")
    def _on_search(self, event: Input.Changed) -> None:
        self.search_term = event.value.strip()
        self._rebuild_table()

    @on(Input.Submitted, "#search")
    def _on_search_submit(self) -> None:
        self.query_one("#results", DataTable).focus()

    def action_close_search(self) -> None:
        if "visible" in self.query_one("#search", Input).classes:
            self.action_search()

    def action_reload_guilds(self) -> None:
        if self.http:
            self.reload_guilds_worker()

    @work(exclusive=True, group="guilds")
    async def reload_guilds_worker(self) -> None:
        try:
            await self._load_guilds()
        except Exception as exc:  # noqa: BLE001
            self._set_status(str(exc), "bold red")

    # -- export ------------------------------------------------------------

    def _export_rows(self, scope: str) -> list[ExportRow]:
        """Rows for an export, honouring the table's filter unless told not to.

        ``filtered`` exports exactly what is on screen, in the order shown --
        exporting everyone when the operator has narrowed to threats is
        surprising, and hands on far more personal data than was asked for.
        """
        if scope == "all":
            source = sorted(
                self.rows.values(),
                key=lambda r: (-int(r.report.verdict), r.member.display_name.lower()),
            )
        else:
            source = [self.rows[key] for key in self._shown if key in self.rows]
        return [
            ExportRow(
                discord_id=row.member.id,
                username=row.member.username,
                display_name=row.member.display_name,
                report=row.report,
            )
            for row in source
        ]

    def action_export(self) -> None:
        if not self.rows or self.current_guild is None:
            self._set_status("Nothing to export yet.", "yellow")
            return
        self.run_export()

    @work(exclusive=True, group="export")
    async def run_export(self) -> None:
        settings = self.config.export
        choice = await self.push_screen_wait(
            ExportDialog(
                formats=settings.formats,
                scope=settings.scope,
                segment_size=settings.segment_size,
                columns=settings.columns or list(DEFAULT_COLUMNS),
                filter_name=self.filter_mode.value,
                filtered_count=len(self._shown),
                total_count=len(self.rows),
            )
        )
        if choice is None:
            self._set_status("Export cancelled.")
            return

        rows = self._export_rows(choice.scope)
        if not rows:
            self._set_status(
                "Nothing matches the current filter - nothing exported.", "yellow"
            )
            return

        guild = self.current_guild
        try:
            manifest = render_export(
                rows,
                guild_name=guild.name,
                guild_id=guild.id,
                base_directory=Path(settings.directory),
                formats=choice.formats,
                columns=choice.columns,
                scope=(
                    "everything scanned"
                    if choice.scope == "all"
                    else f"filter: {self.filter_mode.value}"
                ),
                segment_size=choice.segment_size,
            )
        except OSError as exc:
            self._set_status(f"Export failed: {exc}", "bold red")
            return

        if choice.remember:
            settings.formats = choice.formats
            settings.scope = choice.scope
            settings.segment_size = choice.segment_size
            settings.columns = choice.columns
            try:
                saved = self.config.save_export_settings()
                remembered = f" Saved defaults to {saved.name}."
            except OSError as exc:
                remembered = f" (could not save defaults: {exc})"
        else:
            remembered = ""

        try:
            shown = manifest.directory.relative_to(Path.cwd())
        except ValueError:
            shown = manifest.directory
        parts = (
            f" in {manifest.segments} segments" if manifest.segments > 1 else ""
        )
        self._set_status(
            f"Exported {manifest.rows:,} members{parts} to {shown}/ "
            f"({', '.join(manifest.formats)}).{remembered} Delete within 24h."
        )

    # -- member actions ----------------------------------------------------

    def _selected_row(self) -> Row | None:
        table = self.query_one("#results", DataTable)
        if table.cursor_row < 0:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None
        return self.rows.get(key)

    def action_copy(self) -> None:
        row = self._selected_row()
        if row is None:
            self._set_status("No member selected.", "yellow")
            return
        text = self._member_summary(row)
        try:
            self.copy_to_clipboard(text)
        except Exception as exc:  # noqa: BLE001 - terminal may not support OSC 52
            self._set_status(f"Could not copy: {exc}", "yellow")
            return
        self._set_status(
            f"Copied {row.member.display_name} ({len(text)} chars) to the clipboard."
        )

    def _member_summary(self, row: Row) -> str:
        """Plain-text summary of one member, for the clipboard."""
        report = row.report
        lines = [
            f"{row.member.display_name} ({row.member.tag})",
            f"Discord ID: {row.member.id}",
            f"Verdict: {verdict_label(report.verdict)}",
        ]
        for account in sorted(report.accounts, key=lambda a: -int(a.verdict)):
            lines.append(
                f"  Roblox {account.username} ({account.user_id}) - "
                f"{flag_name(account.flag_type)}"
                + (f" / {category_name(account.category)}" if account.category else "")
            )
            lines.append(f"    {account.profile_url}")
            for name, detail in (account.reasons or {}).items():
                message = str(detail.get("message", "")).replace("\n", " / ")
                lines.append(f"    {name}: {message}")
        if report.servers:
            lines.append(
                f"Tracked servers: "
                + ", ".join(s.server_name for s in report.servers[:8])
            )
        lines.append(ATTRIBUTION)
        return "\n".join(lines)

    def action_kick(self) -> None:
        self._moderate("kick")

    def action_ban(self) -> None:
        self._moderate("ban")

    def _moderate(self, action: str) -> None:
        row = self._selected_row()
        if row is None:
            self._set_status("No member selected.", "yellow")
            return
        if self.current_guild is None or self.http is None:
            return
        self.run_moderation(action, row)

    @work(exclusive=True, group="moderate")
    async def run_moderation(self, action: str, row: Row) -> None:
        guild = self.current_guild
        eligibility = check_eligibility(
            row.report, require_threat=self.config.moderation.require_threat
        )
        choice = await self.push_screen_wait(
            ModerationDialog(
                action=action,
                member_label=f"{row.member.display_name} ({row.member.tag})",
                member_id=row.member.id,
                report=row.report,
                eligibility=eligibility,
                template=self.config.moderation.default_reason,
                delete_message_seconds=self.config.moderation.delete_message_seconds,
            )
        )
        if choice is None:
            self._set_status(f"{action.capitalize()} cancelled.")
            return

        verb = "Kicking" if action == "kick" else "Banning"
        self._set_activity(f"{verb} {row.member.display_name}...")
        try:
            if action == "kick":
                await self.http.kick(guild.id, row.member.id, choice.reason)
            else:
                await self.http.ban(
                    guild.id,
                    row.member.id,
                    choice.reason,
                    choice.delete_message_seconds,
                )
        except DiscordForbidden:
            self._set_status(
                f"Cannot {action} {row.member.display_name}: this account lacks "
                f"the permission, or the target outranks it.",
                "bold red",
            )
            return
        except DiscordNotFound:
            self._set_status(
                f"{row.member.display_name} is no longer in the server.", "yellow"
            )
            return
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"{action.capitalize()} failed: {exc}", "bold red")
            return

        done = "Kicked" if action == "kick" else "Banned"
        self._set_status(f"{done} {row.member.display_name}. Reason: {choice.reason}")
        self._mark_actioned(row, done.lower())

    def _mark_actioned(self, row: Row, what: str) -> None:
        """Note the action on the row so it is obvious what has been handled."""
        row.actioned = what
        try:
            table = self.query_one("#results", DataTable)
            for column, cell in zip(table.columns, self._cells(row)):
                table.update_cell(row.member.id, column, cell)
        except Exception:
            pass

    # -- status bar --------------------------------------------------------

    def _halt_all_routes(self, exc: AllRoutesFailed) -> None:
        """Every route is gone, including direct. Stop and explain each one."""
        self._set_status(
            "Halted - no usable connection to Rotector. See the detail pane.",
            "bold red",
        )
        text = Text()
        text.append("Scan halted: every route failed\n\n", style="bold red")
        text.append(
            "Rotector could not be reached over any configured proxy, nor over "
            "your own connection. Each route's own error:\n\n"
        )
        for name, error in exc.attempts:
            text.append(f"  {name}", style="bold")
            text.append(f"\n    {error}\n", style="red")

        direct_error = exc.direct_error
        text.append("\nWhat this means\n", style="bold")
        if direct_error:
            text.append(
                "  Your direct connection failed too, so this is not a proxy "
                "problem alone. Check that you are online and that Rotector "
                "(roscoe.rotector.com) is reachable and not blocked by a "
                "firewall, DNS filter or captive portal.\n"
            )
        else:
            text.append(
                "  The direct connection was never reached or is in backoff. "
                "Disable proxies in config.toml to scan over your own "
                "connection, or retest them with: python -m rsb proxies\n"
            )
        text.append(
            "\n  Proxies are retried automatically after a backoff, so pressing "
            "'s' again later may succeed without any change.\n",
            style="dim",
        )
        self.query_one("#detail-body", Static).update(text)

    @on(DataTable.RowHighlighted, "#guilds")
    def _estimate_guild(self, event: DataTable.RowHighlighted) -> None:
        """Predict scan duration for the highlighted server."""
        key = event.row_key.value if event.row_key else None
        guild = next((g for g in self.guilds if g.id == key), None)
        if guild is None or self.rotector is None:
            return
        if self.workers and any(w.group == "scan" for w in self.workers):
            return  # a scan is running; leave its output alone

        members = guild.member_count or 0
        text = Text()
        text.append(f"{guild.name}\n", style="bold")
        text.append(f"{members:,} members" if members else "member count unknown", style="dim")
        text.append("\n\n")

        seconds = estimate_scan_seconds(members, self.rotector.capacity_units_per_sec())
        if seconds is None:
            text.append("Press 's' to scan.\n")
        else:
            text.append("Estimated scan time  ", style="bold")
            text.append(format_duration(seconds), style="bold cyan")
            text.append("\n")
            routes = self.rotector.pool.available_count()
            total = len(self.rotector.pool.routes)
            if total > 1:
                text.append(f"across {routes}/{total} usable routes\n", style="dim")
            text.append(
                "\nRotector lookups only; reading the member list from the "
                "gateway happens first and is not included. Assumes a typical "
                "share of members have a linked Roblox account -- the live "
                "estimate corrects itself once the scan starts.\n",
                style="dim",
            )
        self.query_one("#detail-body", Static).update(text)

    def _set_status(self, text: str, style: str = "") -> None:
        # Plain text plus a style, never markup: status lines carry guild names
        # and exception strings, which may contain square brackets.
        # A settled status message ends whatever activity was running.
        self._activity = None
        self._status_text = text
        self._status_style = style
        self._paint_status()

    def _set_activity(self, text: str) -> None:
        """Announce what is happening *now*, before the step that blocks.

        Every long-running step calls this on the way in, so the bar always
        names the current action rather than the last one that finished.
        """
        if text != self._activity:
            self._activity = text
            self._activity_started = time.monotonic()
        self._paint_status()

    def _compose_status(self) -> Text:
        # One line tall, so wrapping would hide everything past the first word.
        if self._activity is not None:
            frame = _SPINNER[self._spinner_frame % len(_SPINNER)]
            text = Text(no_wrap=True, overflow="ellipsis")
            text.append(f"{frame} ", style="bold cyan")
            text.append(self._activity)
            elapsed = time.monotonic() - self._activity_started
            if elapsed >= _ELAPSED_AFTER:
                text.append(f"  {elapsed:.0f}s", style="dim")
        else:
            text = Text(
                self._status_text,
                style=self._status_style,
                no_wrap=True,
                overflow="ellipsis",
            )

        if self.rotector is None:
            return text
        state = self.rotector.limiter.snapshot()
        text.append("   |   ", style="dim")

        pool = self.rotector.pool
        if len(pool.routes) > 1:
            healthy = pool.available_count()
            total = len(pool.routes)
            text.append(
                f"routes {healthy}/{total}",
                style="bold red" if healthy == 0 else ("yellow" if healthy < total else ""),
            )
            text.append("   ", style="dim")

        if state.blocked_for > 0:
            text.append(
                f"holding for rate limit window, {state.blocked_for:.1f}s", style="yellow"
            )
        else:
            text.append(f"budget {state.available}/{state.limit - state.reserve}")
            if state.server_remaining is not None:
                text.append(f" (server: {state.server_remaining})", style="dim")
        return text

    def _paint_status(self) -> None:
        try:
            self.query_one("#status", Static).update(self._compose_status())
        except Exception:
            pass

    def _refresh_status(self) -> None:
        if self._activity is not None:
            self._spinner_frame += 1
        self._paint_status()

    # -- shutdown ----------------------------------------------------------

    async def on_unmount(self) -> None:
        if self.gateway:
            await self.gateway.close()
        if self.http:
            await self.http.aclose()
        if self.rotector:
            await self.rotector.aclose()
