"""Textual UI: pick a server on the left, scan it, read verdicts on the right."""

from __future__ import annotations

import asyncio
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.message import Message
from textual.widgets import (
    DataTable,
    RichLog,
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
from ..discord.http import DiscordForbidden, DiscordHTTPError, DiscordNotFound
from ..eta import RateEstimator, estimate_scan_seconds, format_duration
from ..export import DEFAULT_COLUMNS, export as render_export, ExportRow
from ..moderation import Eligibility, build_reason, check_eligibility
from ..profiles import fetch_profiles
from ..sources import (
    GROUPS,
    KIND_FRIENDS,
    KIND_INBOX,
    KIND_GROUP,
    KIND_GUILD,
    KIND_REQUESTS,
    ScanSource,
    build_sources,
    group_for,
)
from ..purge import (
    KIND_DM,
    KIND_GROUP as PURGE_GROUP,
    PurgeTarget,
    execute_purge,
    plan_purge,
)
from .commands import BindingCommands, ScrollableFooter, StatusStrip
from ..hotreload import HotReloader
from .settings import (
    Check as _Check,
    ErrorScreen,
    advisory_problems,
    blocking_problems,
    DiagnosticsScreen,
    SettingsScreen,
    SetupWizard,
    run_checks,
)
from .dialogs import (
    ExportDialog,
    RescanDialog,
    LeaveGroupDialog,
    ModerationDialog,
    PurgeConfirmDialog,
    PurgePlanDialog,
)
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


def _shorten(text: str, width: int) -> str:
    """Trim to ``width`` cells, marking that something was cut."""
    if width <= 1 or len(text) <= width:
        return text
    return text[: max(1, width - 1)].rstrip() + "\u2026"


@dataclass
class ActionSpec:
    """One member action, in the terms of the source it applies to.

    Kicking makes no sense on a friend and unfriending makes none in a server,
    so the same two keys mean different things per source rather than sitting
    there broken.
    """

    name: str
    gerund: str
    past: str
    run: "Callable"
    forbidden_hint: str
    uses_reason: bool = True
    wants_purge: bool = False
    note: str = ""
    #: whether moderation.require_threat applies.
    #: Kicking and banning restrict someone's access to a community, so they
    #: are gated on an actionable finding. Unfriending, blocking and leaving a
    #: private group are your own boundaries to set -- the finding is context
    #: there, not a precondition, and requiring Rotector's endorsement to
    #: block someone would be absurd.
    gated: bool = True


async def _do_kick(app, source, row, choice):
    await app.http.kick(source.id, row.member.id, choice.reason)


async def _do_ban(app, source, row, choice):
    await app.http.ban(
        source.id, row.member.id, choice.reason, choice.delete_message_seconds
    )


async def _do_unfriend(app, source, row, choice):
    await app.http.remove_friend(row.member.id)


async def _do_block(app, source, row, choice):
    await app.http.block_user(row.member.id)


async def _do_group_remove(app, source, row, choice):
    await app.http.remove_group_recipient(source.id, row.member.id)


_KICK = ActionSpec(
    "kick", "Kicking", "kicked", _do_kick,
    "this account lacks the permission, or the target outranks it.",
)
_BAN = ActionSpec(
    "ban", "Banning", "banned", _do_ban,
    "this account lacks the permission, or the target outranks it.",
    wants_purge=True,
)
_UNFRIEND = ActionSpec(
    "remove friend", "Removing", "unfriended", _do_unfriend,
    "Discord refused the request.",
    uses_reason=False,
    gated=False,
    note="Removes the friendship only. They can still message you and can send "
         "another request -- block instead if you want that stopped.",
)
_DECLINE = ActionSpec(
    "decline request", "Declining", "declined", _do_unfriend,
    "Discord refused the request.",
    uses_reason=False,
    gated=False,
    note="Declines the request. They are not told, and can send another.",
)
_BLOCK = ActionSpec(
    "block", "Blocking", "blocked", _do_block,
    "Discord refused the request.",
    uses_reason=False,
    gated=False,
    note="Blocks them and removes any friendship. They cannot message you or "
         "see your messages.",
)
_GROUP_REMOVE = ActionSpec(
    "remove from group", "Removing", "removed", _do_group_remove,
    "only the group's owner can remove people.",
    uses_reason=False,
    gated=False,
    note="Removes them from this group DM. Only the group owner may do this.",
)

#: which two member actions each source kind offers, keyed by binding
_ACTIONS: dict[str, dict[str, ActionSpec]] = {
    KIND_GUILD: {"kick": _KICK, "ban": _BAN},
    KIND_FRIENDS: {"kick": _UNFRIEND, "ban": _BLOCK},
    KIND_REQUESTS: {"kick": _DECLINE, "ban": _BLOCK},
    KIND_GROUP: {"kick": _GROUP_REMOVE, "ban": _BLOCK},
}


def _category_key(row: "Row") -> tuple:
    worst = row.report.worst_account
    name = category_name(worst.category if worst else None)
    # unknown categories sort last rather than first, whichever direction
    return (name is None, (name or "").lower(), row.member.display_name.lower())


#: results columns, in table order, paired with how to sort by each
RESULT_SORTS: list[tuple[str, "Callable[[Row], object]"]] = [
    ("Member", lambda r: r.member.display_name.lower()),
    ("Verdict", lambda r: (-int(r.report.verdict), r.member.display_name.lower())),
    ("Flag", lambda r: (
        flag_name(r.report.worst_account.flag_type if r.report.worst_account else None),
        r.member.display_name.lower(),
    )),
    ("Category", _category_key),
    ("Roblox", lambda r: (
        not r.report.accounts,
        (r.report.accounts[0].username.lower() if r.report.accounts else ""),
    )),
    ("Srv", lambda r: (-len(r.report.servers), r.member.display_name.lower())),
]

#: sources columns, likewise
SOURCE_SORTS: list[tuple[str, "Callable[[ScanSource], object]"]] = [
    ("Name", lambda s: s.name.lower()),
    ("Kind", lambda s: (s.label, s.name.lower())),
    ("Members", lambda s: (-(s.member_count or 0), s.name.lower())),
]

#: results default to worst-first, which is the whole point of the tool
DEFAULT_RESULT_SORT = 1

#: Row-key prefix for group headers in the sources pane. Deliberately not
#: "group:", which is what a group DM's own key starts with -- sharing that
#: prefix made every group DM look like a header and unselectable.
GROUP_KEY = "grouphdr:"

#: results rows rendered at once. Textual redraws the whole table on change,
#: so a five-figure member list has to be paged or the UI crawls.
PAGE_SIZE = 250


class DetailDivider(Static):
    """Drag to resize the detail pane; click to fold it.

    Same idea as the vertical divider between the panes, turned on its side.
    A drag and a click are told apart by whether the pointer actually moved,
    so one handle can do both without a modifier key.
    """

    MIN_HEIGHT = 3
    MAX_HEIGHT = 40

    def __init__(self) -> None:
        super().__init__(id="detail-divider", markup=False)
        self._dragging = False
        self._moved = False

    def on_mouse_down(self, event) -> None:
        self._dragging = True
        self._moved = False
        self.capture_mouse()
        self.add_class("dragging")
        event.stop()

    def on_mouse_move(self, event) -> None:
        if not self._dragging:
            return
        if abs(event.delta_y) or abs(event.delta_x):
            self._moved = True
        # the pane runs from the pointer to the bottom of the screen
        self.app.set_detail_height(self.app.size.height - int(event.screen_y) - 2)
        event.stop()

    def on_mouse_up(self, event) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self.release_mouse()
        self.remove_class("dragging")
        if not self._moved:
            self.app.action_toggle_detail()
        event.stop()


class SourceTable(DataTable):
    """The sources list, with group headers that fold on a single click.

    DataTable only emits RowSelected once it has focus, so clicking a group in
    an unfocused pane cost two clicks: one to focus, one to act. Handling the
    click here catches the first one either way.
    """

    class GroupClicked(Message):
        def __init__(self, title: str) -> None:
            super().__init__()
            self.title = title

    def on_click(self, event) -> None:
        try:
            row, _column = self.hover_coordinate
        except Exception:
            return
        if row < 0 or row >= self.row_count:
            return
        try:
            key = self.coordinate_to_cell_key(Coordinate(row, 0)).row_key.value
        except Exception:
            return
        if key and key.startswith(GROUP_KEY):
            self.move_cursor(row=row)
            self.post_message(self.GroupClicked(key[len(GROUP_KEY):]))
            event.stop()


class PaneDivider(Static):
    """Draggable splitter between the sources pane and the results pane.

    Textual has no built-in splitter, so this captures the mouse on press and
    resizes the left pane as the pointer moves. Keyboard users are not left
    out: the same adjustment is bound to `[` and `]` on the app.
    """

    MIN_WIDTH = 24
    MAX_WIDTH = 90

    def __init__(self) -> None:
        super().__init__("\u2502", id="divider")
        self._dragging = False

    def on_mouse_down(self, event) -> None:
        self._dragging = True
        self.capture_mouse()
        self.add_class("dragging")
        event.stop()

    def on_mouse_move(self, event) -> None:
        if not self._dragging:
            return
        # screen_x is absolute, so the new width is simply the pointer column
        self.app.set_pane_width(int(event.screen_x))
        event.stop()

    def on_mouse_up(self, event) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self.release_mouse()
        self.remove_class("dragging")
        event.stop()


class _ScanAborted(Exception):
    """Raised inside the member sink to stop reading when the scan has died."""


@dataclass
class Row:
    member: GuildMember
    report: MemberReport
    #: "kicked" / "banned" once acted on, for the table to show
    actioned: str | None = None
    #: False for members listed but never looked up
    checked: bool = True


class ScannerApp(App):
    TITLE = "rotector-selfbot"
    SUB_TITLE = "Discord member safety scanner"
    #: the palette lists every binding, so a clipped footer hides nothing
    COMMANDS = App.COMMANDS | {BindingCommands}

    CSS = """
    Screen { layers: base overlay; }

    #body { height: 1fr; }

    #servers-pane {
        width: 56;
    }
    #servers-pane.collapsed { width: 3; }
    #servers-pane.collapsed #guilds { display: none; }
    #servers-pane.collapsed .pane-title { width: 3; }
    /* the results pane takes whatever the sources pane gives up, so folding
       it away widens the table rather than leaving a gap */
    #results-pane { width: 1fr; }

    .pane-title { link-color: $text; }

    #detail.collapsed { display: none; }
    #detail-divider {
        height: 1;
        width: 100%;
        background: $panel-lighten-1;
        color: $panel-lighten-3;
        text-align: center;
    }
    #detail-divider:hover, #detail-divider.dragging {
        background: $primary;
        color: $primary;
    }

    #logpanel {
        height: 14;
        display: none;
        border-top: solid $panel-lighten-2;
        background: $surface-darken-1;
    }
    #logpanel.visible { display: block; }
    #divider {
        width: 1;
        height: 100%;
        background: $panel-lighten-2;
        color: $panel-lighten-3;
    }
    #divider:hover, #divider.dragging {
        background: $primary;
        color: $primary;
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
        ("n", "next_page", "Next page"),
        ("N", "prev_page", "Prev page"),
        ("m", "list_members", "List members only"),
        ("S", "scan_member", "Scan this member"),
        ("p", "purge", "Purge my messages"),
        ("k", "kick", "Kick / unfriend"),
        ("b", "ban", "Ban / block"),
        ("ctrl+r", "reload_guilds", "Reload sources"),
        ("ctrl+s", "settings", "Settings"),
        ("ctrl+shift+r", "reload_code", "Reload code"),
        ("ctrl+b", "toggle_sources", "Sources pane"),
        ("ctrl+d", "toggle_detail", "Detail pane"),
        ("ctrl+up", "grow_detail", ""),
        ("ctrl+down", "shrink_detail", ""),
        ("ctrl+l", "toggle_log", "Debug log"),
        ("L", "leave_group", "Leave group DM"),
        ("[", "narrow_pane", ""),
        ("]", "widen_pane", ""),
        ("o", "cycle_sort", "Sort column"),
        ("O", "reverse_sort", "Reverse sort"),
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

        self.sources: list[ScanSource] = []
        self.rows: dict[str, Row] = {}
        self.current_source: ScanSource | None = None
        self.filter_mode = FilterMode.FINDINGS
        # verdicts treated as noise; never dropped from the data, only hidden
        self.hidden_verdicts: set[Verdict] = set()
        if config.scan.hide_no_detections:
            self.hidden_verdicts.add(Verdict.NO_DETECTIONS)
        if config.scan.hide_unknown:
            self.hidden_verdicts.add(Verdict.UNKNOWN)
        self.search_term = ""
        self.source_search = ""
        self._search_target: str | None = None
        self._advisories: list = []
        self._sources_hidden = False
        self._detail_hidden = False
        self._detail_height = 14
        self._last_logged = ""
        #: how many lines the debug log holds; RichLog exposes no count
        self.log_lines = 0
        self._reloader = HotReloader()
        #: how to retry whatever last failed, if it can be retried
        self._retry: Callable[[], None] | None = None
        #: rows rendered per page; a table of 10k rows is unusable otherwise
        self.page_size = PAGE_SIZE
        self._page = 0
        self._matching = 0
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
        self._eta_progress: tuple[int, int] | None = None
        self._eta_ready = False
        #: start of the whole run, spanning every phase
        self._process_started: float | None = None
        #: set while a scan is being torn down, so late callbacks are ignored
        self._stopping = False
        self._scan_task: asyncio.Task | None = None
        self._pane_width = 56
        self.my_id: str | None = None
        # (column index, descending) per table
        self._result_sort = (DEFAULT_RESULT_SORT, False)
        self._source_sort: tuple[int, bool] | None = None
        #: group titles the user has folded away
        self._collapsed: set[str] = set()
        self._source_rows: list[str] = []

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="servers-pane"):
                yield Static("SOURCES  <", classes="pane-title", id="sources-title")
                yield SourceTable(id="guilds", cursor_type="row", zebra_stripes=True)
            yield PaneDivider()
            with Vertical(id="results-pane"):
                yield Static("RESULTS", classes="pane-title", id="results-title")
                yield Static("", id="summary")
                yield Input(placeholder="Filter by name or ID...", id="search")
                yield DataTable(id="results", cursor_type="row", zebra_stripes=True)
                yield DetailDivider()
                yield Static("DETAILS  v", classes="pane-title", id="detail-title")
                with VerticalScroll(id="detail"):
                    yield Static(self._welcome_text(), id="detail-body")
        yield RichLog(id="logpanel", markup=False, wrap=False, max_lines=2000)
        yield ProgressBar(id="progress", show_eta=False)
        yield StatusStrip()
        yield ScrollableFooter()

    def on_mount(self) -> None:
        guilds = self.query_one("#guilds", DataTable)
        for (name, _), width in zip(SOURCE_SORTS, (32, 10, 9)):
            guilds.add_column(name, width=width)

        results = self.query_one("#results", DataTable)
        for (name, _), width in zip(RESULT_SORTS, (28, 15, 17, 12, 24, 6)):
            results.add_column(name, width=width)

        self._apply_sort_headers()
        self.set_interval(0.15, self._refresh_status)
        self.connect()

    def _sorted_labels(self, names: list[str], state) -> list[str]:
        """Column labels with an arrow on whichever one is sorting."""
        if state is None:
            return list(names)
        index, descending = state
        arrow = " v" if descending else " ^"
        return [
            f"{name}{arrow}" if i == index else name
            for i, name in enumerate(names)
        ]

    def _apply_sort_headers(self) -> None:
        results = self.query_one("#results", DataTable)
        labels = self._sorted_labels(
            [name for name, _ in RESULT_SORTS], self._result_sort
        )
        for column, label in zip(results.columns.values(), labels):
            column.label = Text(label)
        sources = self.query_one("#guilds", DataTable)
        labels = self._sorted_labels(
            [name for name, _ in SOURCE_SORTS], self._source_sort
        )
        for column, label in zip(sources.columns.values(), labels):
            column.label = Text(label)
        results.refresh()
        sources.refresh()

    def _sort_results(self, index: int, toggle: bool = True) -> None:
        current, descending = self._result_sort
        # clicking the sorted column again flips direction, as tables do
        descending = not descending if (toggle and index == current) else False
        self._result_sort = (index % len(RESULT_SORTS), descending)
        self._apply_sort_headers()
        self._rebuild_table()
        name = RESULT_SORTS[self._result_sort[0]][0]
        self._set_status(
            f"Results sorted by {name}, "
            f"{'descending' if descending else 'ascending'}."
        )

    def _sort_sources(self, index: int, toggle: bool = True) -> None:
        current, descending = self._source_sort or (-1, False)
        descending = not descending if (toggle and index == current) else False
        self._source_sort = (index % len(SOURCE_SORTS), descending)
        self._apply_sort_headers()
        self.call_later(self._refresh_source_table)
        name = SOURCE_SORTS[self._source_sort[0]][0]
        self._set_status(
            f"Sources sorted by {name}, "
            f"{'descending' if descending else 'ascending'}."
        )

    def _focused_table_id(self) -> str:
        node = self.focused
        while node is not None:
            if isinstance(node, DataTable) and node.id in ("results", "guilds"):
                return node.id
            node = node.parent
        return "results"

    def action_cycle_sort(self) -> None:
        """Move to the next sortable column on whichever table has focus."""
        if self._focused_table_id() == "guilds":
            index = 0 if self._source_sort is None else self._source_sort[0] + 1
            self._sort_sources(index % len(SOURCE_SORTS), toggle=False)
        else:
            self._sort_results(
                (self._result_sort[0] + 1) % len(RESULT_SORTS), toggle=False
            )

    def action_reverse_sort(self) -> None:
        if self._focused_table_id() == "guilds":
            index = 0 if self._source_sort is None else self._source_sort[0]
            self._sort_sources(index, toggle=True)
        else:
            self._sort_results(self._result_sort[0], toggle=True)

    @on(DataTable.HeaderSelected, "#results")
    def _results_header_clicked(self, event: DataTable.HeaderSelected) -> None:
        self._sort_results(event.column_index)

    @on(DataTable.HeaderSelected, "#guilds")
    def _sources_header_clicked(self, event: DataTable.HeaderSelected) -> None:
        self._sort_sources(event.column_index)

    @on(SourceTable.GroupClicked)
    def _group_clicked(self, event: SourceTable.GroupClicked) -> None:
        self._toggle_group(event.title)

    # -- collapsible panes -------------------------------------------------

    def action_toggle_sources(self) -> None:
        """Fold the sources pane away, leaving results and details the width."""
        self._sources_hidden = not self._sources_hidden
        pane = self.query_one("#servers-pane")
        pane.set_class(self._sources_hidden, "collapsed")
        self.query_one("#sources-title", Static).update(
            ">" if self._sources_hidden else "SOURCES  <"
        )
        if self._sources_hidden:
            self.query_one("#results", DataTable).focus()
        else:
            pane.styles.width = self._pane_width
            self.query_one("#guilds", DataTable).focus()
        self.log_debug(
            f"sources pane {'hidden' if self._sources_hidden else 'shown'}"
        )

    def set_detail_height(self, height: int) -> None:
        """Resize the detail pane, clamped so neither pane vanishes."""
        detail = self.query_one("#detail")
        if self._detail_hidden:
            return
        height = max(
            DetailDivider.MIN_HEIGHT,
            min(
                DetailDivider.MAX_HEIGHT,
                min(height, max(DetailDivider.MIN_HEIGHT, self.size.height - 12)),
            ),
        )
        detail.styles.height = height
        self._detail_height = height

    def action_grow_detail(self) -> None:
        self.set_detail_height(self._detail_height + 3)

    def action_shrink_detail(self) -> None:
        self.set_detail_height(self._detail_height - 3)

    def action_toggle_detail(self) -> None:
        """Fold the detail pane away, the same way the sources pane folds."""
        self._detail_hidden = not self._detail_hidden
        detail = self.query_one("#detail")
        detail.set_class(self._detail_hidden, "collapsed")
        self.query_one("#detail-title", Static).update(
            "DETAILS  >" if self._detail_hidden else "DETAILS  v"
        )
        if not self._detail_hidden:
            detail.styles.height = self._detail_height
        self.log_debug(f"detail pane {'hidden' if self._detail_hidden else 'shown'}")

    @on(events.Click, "#sources-title")
    def _sources_title_clicked(self, event) -> None:
        event.stop()
        self.action_toggle_sources()

    @on(events.Click, "#detail-title")
    def _detail_title_clicked(self, event) -> None:
        event.stop()
        self.action_toggle_detail()

    # -- debug log ---------------------------------------------------------

    def action_toggle_log(self) -> None:
        panel = self.query_one("#logpanel", RichLog)
        showing = "visible" in panel.classes
        panel.set_class(not showing, "visible")
        if not showing:
            self.log_debug("debug log opened")

    def log_debug(self, message: str, level: str = "info") -> None:
        """Append a line to the debug log, whether or not it is on screen.

        The log keeps what the status bar overwrites: a scan emits dozens of
        messages, each replacing the last, and when something goes wrong the
        useful one has usually already gone.
        """
        if message == self._last_logged:
            return
        self._last_logged = message
        self.log_lines += 1
        stamp = time.strftime("%H:%M:%S")
        style = {
            "error": "bold red",
            "warn": "yellow",
            "net": "cyan",
            "scan": "green",
        }.get(level, "")
        line = Text(f"{stamp}  ", style="dim")
        line.append(f"{level:<5} ", style=style or "dim")
        line.append(message)
        try:
            self.query_one("#logpanel", RichLog).write(line)
        except Exception:
            pass

    def set_pane_width(self, width: int) -> None:
        """Resize the sources pane, clamped to something usable."""
        pane = self.query_one("#servers-pane")
        width = max(
            PaneDivider.MIN_WIDTH,
            min(PaneDivider.MAX_WIDTH, min(width, self.size.width - 20)),
        )
        pane.styles.width = width
        self._pane_width = width

    def action_widen_pane(self) -> None:
        self.set_pane_width(self._pane_width + 4)

    def action_narrow_pane(self) -> None:
        self.set_pane_width(self._pane_width - 4)

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

    def action_reload_code(self) -> None:
        self.reload_code()

    @work(exclusive=True, group="reload")
    async def reload_code(self, then: Callable[[], None] | None = None) -> None:
        """Pick up edited modules without restarting or losing results."""
        held = len(self.rows)
        report = self._reloader.reload()
        if report.failed:
            self._set_status(report.describe(), "bold red")
            return
        if not report.reloaded:
            self._set_status(report.describe())
            return
        kept = f" {held:,} scanned members kept." if held else ""
        self._set_status(f"{report.describe()}.{kept}")
        try:
            self.query_one(ScrollableFooter).refresh_keys()
        except Exception:
            pass
        if then is not None:
            then()

    def report_error(
        self,
        exc: BaseException,
        context: str = "",
        retry: Callable[[], None] | None = None,
    ) -> None:
        """Surface a failure with a way out, instead of a dead status line."""
        self._retry = retry
        held = len(self.rows)
        self.log_debug(f"{context}: {type(exc).__name__}: {exc}", "error")
        self.show_error(
            summary=f"{type(exc).__name__}: {exc}",
            detail="".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )[-4000:],
            context=context,
            can_retry=retry is not None,
            preserved=(
                f"{held:,} scanned members are still loaded."
                if held
                else "Nothing was in progress."
            ),
        )

    @work(exclusive=True, group="error")
    async def show_error(
        self,
        summary: str,
        detail: str,
        context: str,
        can_retry: bool,
        preserved: str,
    ) -> None:
        choice = await self.push_screen_wait(
            ErrorScreen(
                summary,
                detail,
                context=context,
                can_retry=can_retry,
                preserved=preserved,
            )
        )
        if choice == "reload":
            self.reload_code(then=self._retry if self._retry else None)
        elif choice == "retry" and self._retry is not None:
            self._retry()

    def action_settings(self) -> None:
        self.open_settings()

    @work(exclusive=True, group="settings")
    async def open_settings(self) -> None:
        changed = await self.push_screen_wait(SettingsScreen(self.config))
        if changed:
            self._set_status(
                "Settings saved. Some changes need a restart to take effect."
            )
            # anything that only affects display can be applied at once
            self.hidden_verdicts = set()
            if self.config.scan.hide_no_detections:
                self.hidden_verdicts.add(Verdict.NO_DETECTIONS)
            if self.config.scan.hide_unknown:
                self.hidden_verdicts.add(Verdict.UNKNOWN)
            self._rebuild_table()

    @work(exclusive=True, group="connect")
    async def connect(self) -> None:
        # First run, or a config that cannot work: say so up front rather than
        # failing with a stack trace three steps later.
        if not (self.config.token or "").strip():
            if not await self.push_screen_wait(SetupWizard(self.config)):
                self.exit()
                return

        blocking = blocking_problems(self.config)
        if blocking:
            choice = await self.push_screen_wait(
                DiagnosticsScreen(
                    run_checks(self.config),
                    headline="The configuration cannot work as it stands.",
                )
            )
            if choice == "settings":
                await self.push_screen_wait(SettingsScreen(self.config))
            if blocking_problems(self.config):
                self._set_status(
                    "Configuration problems unresolved - press ctrl+s to fix.",
                    "bold red",
                )
                return

        advisories = advisory_problems(self.config)

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

            self.my_id = str(me.get("id") or "")
            name = me.get("global_name") or me.get("username") or "?"
            keyed = "API key" if self.config.rotector.api_key else "no API key"
            routing = f" - {len(proxies)} proxies" if proxies else ""
            self.sub_title = f"{name} - {keyed}{routing}"
            if advisories:
                self._advisories = advisories
            await self._load_sources()
        except DiscordAuthError as exc:
            self._fatal(str(exc))
            self.offer_diagnostics(str(exc))
        except GatewayError as exc:
            self._fatal(f"Gateway: {exc}")
        except Exception as exc:  # noqa: BLE001
            self._fatal(f"{type(exc).__name__}: {exc}")

    @work(exclusive=True, group="diagnose")
    async def offer_diagnostics(self, headline: str) -> None:
        checks = run_checks(self.config)
        checks.append(
            _Check("Discord reachable", False, headline,
                   "Check the token, and that discord.com is reachable.")
        )
        choice = await self.push_screen_wait(
            DiagnosticsScreen(checks, headline=headline)
        )
        if choice == "settings":
            if await self.push_screen_wait(SettingsScreen(self.config)):
                self.connect()
        elif choice == "retry":
            self.connect()

    async def _load_sources(self) -> None:
        assert self.http is not None
        self._set_activity("Loading your servers, friends and group DMs...")
        guilds = await self.http.guilds()

        # Relationships and private channels are optional extras: a token
        # without access to them should not cost you the server list.
        relationships, private_channels = [], []
        try:
            relationships = await self.http.relationships()
        except Exception:  # noqa: BLE001 - an optional extra, never fatal
            pass
        try:
            private_channels = await self.http.private_channels()
        except Exception:  # noqa: BLE001
            pass

        self.sources = build_sources(guilds, relationships, private_channels)

        await self._refresh_source_table(focus_key="")
        self.query_one("#guilds", DataTable).focus()

        counts = {}
        for source in self.sources:
            counts[source.label] = counts.get(source.label, 0) + 1
        summary = ", ".join(f"{n} {label}" for label, n in counts.items())
        note = ""
        if self._advisories:
            names = ", ".join(c.name for c in self._advisories)
            note = f"  [{len(self._advisories)} config warning(s): {names} - ctrl+s]"
        self._set_status(
            f"{summary}. Select one and press 's' to scan.{note}",
            "yellow" if self._advisories else "",
        )

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

    def _selected_source(self) -> ScanSource | None:
        table = self.query_one("#guilds", DataTable)
        if not self.sources or table.cursor_row < 0:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None
        if key and key.startswith(GROUP_KEY):
            return None
        return next(
            (s for s in self.sources if f"{s.kind}:{s.id}" == key), None
        )

    def _selected_group(self) -> str | None:
        table = self.query_one("#guilds", DataTable)
        if table.cursor_row < 0:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None
        return key[len(GROUP_KEY):] if key and key.startswith(GROUP_KEY) else None

    @work(exclusive=True, group="scan-start")
    async def start_scan(self, source: ScanSource, lookup: bool = True) -> None:
        """Decide what happens to existing results, then scan."""
        mode = "replace"
        if self.rows and lookup:
            configured = self.config.scan.on_rescan
            if configured == "ask":
                choice = await self.push_screen_wait(
                    RescanDialog(source.name, len(self.rows), "replace")
                )
                if choice is None:
                    self._set_status("Scan cancelled.")
                    return
                mode = choice.mode
                if choice.remember:
                    self.config.scan.on_rescan = mode
                    try:
                        self.config.save_scan_settings()
                    except OSError:
                        pass
            else:
                mode = configured
        self.scan_source(source, lookup=lookup, mode=mode)

    def action_scan(self) -> None:
        group = self._selected_group()
        if group is not None:
            self._toggle_group(group)
            return
        source = self._selected_source()
        if source is None:
            return
        if self.gateway is None or self.rotector is None:
            self._set_status("Still connecting...", "yellow")
            return
        self.start_scan(source)

    def action_list_members(self) -> None:
        """Enumerate members without looking any of them up."""
        source = self._selected_source()
        if source is None or source.is_live:
            self._set_status(
                "Pick a server, friends list or group DM to list.", "yellow"
            )
            return
        if self.gateway is None:
            self._set_status("Still connecting...", "yellow")
            return
        self.start_scan(source, lookup=False)

    def action_scan_member(self) -> None:
        """Look up just the highlighted member."""
        row = self._selected_row()
        if row is None:
            self._set_status("No member selected.", "yellow")
            return
        if self.rotector is None:
            return
        self.scan_one(row)

    @work(exclusive=True, group="scan-one")
    async def scan_one(self, row: Row) -> None:
        member = row.member
        self._set_activity(f"Checking {member.display_name}...")
        try:
            reports = await self.rotector.scan_members([member.id])
        except AllRoutesFailed as exc:
            self._halt_all_routes(exc)
            return
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Lookup failed: {exc}", "bold red")
            self.report_error(
                exc,
                context=f"while checking {member.display_name}",
                retry=lambda: self.scan_one(row),
            )
            return

        report = reports.get(member.id)
        if report is None:
            self._set_status(f"No answer for {member.display_name}.", "yellow")
            return

        self.rows[member.id] = Row(
            member=member, report=report, actioned=row.actioned, checked=True
        )
        self._rebuild_table()
        verdict = report.verdict
        self._set_status(
            f"{member.display_name}: {verdict_label(verdict)} - "
            f"{verdict_meaning(verdict)}",
            verdict_style(verdict) if verdict is Verdict.THREAT else "",
        )

    def action_stop_scan(self) -> None:
        """Stop a running scan, including the work it spawned.

        The lookup pipeline runs in a plain task rather than a Textual worker,
        so cancelling the worker group alone leaves it running -- and a running
        pipeline keeps reporting progress, which restarts the spinner and the
        per-task clock the user just stopped.
        """
        cancelled = self.workers.cancel_group(self, "scan")
        task = self._scan_task
        if task is not None and not task.done():
            task.cancel()
            cancelled = True
        if not cancelled:
            return

        self._stopping = True
        self._end_run()
        self._set_status("Scan stopped.", "yellow")
        self.query_one("#progress", ProgressBar).remove_class("visible")

    def _end_run(self) -> None:
        """Clear everything that animates, so a stopped run looks stopped."""
        self._activity = None
        self._eta_progress = None
        self._eta_ready = False
        self._process_started = None
        self._scan_task = None
        self._paint_status()

    @work(exclusive=True, group="scan")
    async def scan_source(
        self, source: ScanSource, lookup: bool = True, mode: str = "replace"
    ) -> None:
        assert self.http and self.gateway and self.rotector
        self.current_source = source
        merging = mode.startswith("merge")
        carried = len(self.rows) if merging else 0
        if not merging:
            self.rows.clear()
        self._shown.clear()
        self._matching = 0
        self._page = 0
        self.query_one("#results", DataTable).clear()
        self.query_one("#results-title", Static).update(
            f"RESULTS - {source.name} ({source.label})"
        )
        self._update_summary()

        progress = self.query_one("#progress", ProgressBar)
        progress.add_class("visible")
        progress.update(total=100, progress=0)
        self._stopping = False
        self._process_started = time.monotonic()

        try:
            def member_progress(found: int, total: int | None, note: str) -> None:
                if total:
                    progress.update(total=total, progress=min(found, total))
                if found:
                    seen = f"{found:,}{f' / {total:,}' if total else ''}"
                    plural = "" if found == 1 else "s"
                    self._set_activity(f"{note} - {seen} member{plural} found")
                else:
                    self._set_activity(note)

            queue: asyncio.Queue = asyncio.Queue()
            by_id: dict[str, GuildMember] = {}
            cap = self.config.scan.max_members
            truncated = 0
            reading_done = False
            listed_dirty = [False]
            seen = [0]
            skipped = [0]
            self._eta.reset()
            self._eta_progress = None
            self._eta_ready = False

            def live_label(checked: int = 0) -> str:
                return (
                    f"Watching incoming messages - {seen[0]:,} sender(s) seen, "
                    f"{checked:,} checked - press x to stop"
                )

            def scan_progress(stage: str, done: int, of: int) -> None:
                progress.update(total=max(of, 1), progress=done)
                self._eta.update(done)
                self._eta_progress = None if source.is_live else (done, of)
                self._eta_ready = reading_done
                if source.is_live:
                    # a live feed has no end, so "watching" is the true state;
                    # lookup progress must not overwrite it
                    self._set_activity(live_label(done))
                    return
                note = "" if reading_done else "  (still reading members)"
                self._set_activity(f"{stage} - {done:,} / {of:,}{note}")

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
            self._scan_task = scan_task

            def on_members(new_members: list[GuildMember]) -> None:
                nonlocal truncated
                if lookup and scan_task.done():
                    # the scan died (e.g. every route failed); stop reading
                    raise _ScanAborted
                for member in new_members:
                    if self.config.scan.skip_bots and member.bot:
                        continue
                    if cap and len(by_id) >= cap:
                        truncated += 1
                        continue
                    if mode == "merge_skip" and member.id in self.rows:
                        # already checked; the point of this mode is to spend
                        # the rate limit only on people we have not seen
                        skipped[0] += 1
                        continue
                    by_id[member.id] = member
                    if lookup:
                        queue.put_nowait(member.id)
                    else:
                        # listing only: show the member, judge nothing
                        self.rows[member.id] = Row(
                            member=member,
                            report=MemberReport(discord_id=member.id),
                            checked=False,
                        )
                        self._append_row(member.id)
                        listed_dirty[0] = True

            try:
                if source.is_live:
                    self._set_activity(live_label())

                    def on_author(member: GuildMember) -> None:
                        seen[0] += 1
                        on_members([member])
                        self._set_activity(live_label(len(self.rows)))

                    await self.gateway.watch_messages(
                        on_author,
                        should_stop=lambda: self._stopping,
                        own_id=self.my_id,
                        include_bots=not self.config.scan.skip_bots,
                    )
                elif source.needs_gateway:
                    self._set_activity(f"Reading channels of {source.name}...")
                    channels = await self.http.channels(source.id)
                    if not channels:
                        self._set_status(
                            "No readable text channels in this server.", "yellow"
                        )
                        progress.remove_class("visible")
                        queue.put_nowait(None)
                        scan_task.cancel()
                        return
                    open_channels = sum(1 for c in channels if c.everyone_can_view)
                    guild = source.guild
                    can_chunk = bool(guild and guild.can_chunk)

                    # The public widget, if this guild publishes one. It needs
                    # no permissions, but gives names rather than ids, so it is
                    # only worth the round trip when we cannot get the whole
                    # list outright.
                    widget_names: list[str] = []
                    if not can_chunk:
                        self._set_activity("Checking for a public server widget...")
                        try:
                            widget = await self.http.widget(source.id)
                        except Exception:  # noqa: BLE001 - an extra, never fatal
                            widget = None
                        if widget:
                            widget_names = [
                                str(m.get("username") or "")
                                for m in (widget.get("members") or [])
                            ]
                            self._set_activity(
                                f"Widget is enabled: {len(widget_names)} online "
                                f"names to cross-check"
                            )
                    self._set_activity(
                        f"{len(channels)} text channels, {open_channels} visible to "
                        f"@everyone"
                        + ("  (full member list permitted)" if can_chunk else "")
                    )
                    await self.gateway.fetch_members(
                        source.id,
                        channels,
                        expected=source.member_count,
                        on_progress=member_progress,
                        on_members=on_members,
                        can_chunk=can_chunk,
                        widget_names=widget_names,
                    )
                else:
                    # friends, requests and group DMs arrive complete from one
                    # REST call -- no sidebar, no coverage question
                    self._set_activity(f"Reading {source.label}...")
                    on_members(list(source.members))
                    member_progress(
                        len(source.members),
                        source.member_count,
                        f"Read {source.label}",
                    )
            except _ScanAborted:
                pass
            finally:
                reading_done = True
                queue.put_nowait(None)

            if lookup:
                await scan_task
            else:
                scan_task.cancel()
                if listed_dirty[0]:
                    self._update_summary()

            if not by_id:
                if skipped[0]:
                    # read fine, just nothing new -- a very different situation
                    # from not being able to see the member list at all
                    self._set_status(
                        f"Nothing new: all {skipped[0]:,} members read were "
                        f"already checked. Use 're-check everyone' to look "
                        f"them up again."
                    )
                else:
                    self._set_status(
                        "No members could be read. The member list may be hidden "
                        "for this account in this server.",
                        "yellow",
                    )
                progress.remove_class("visible")
                return

            total = len(by_id)

            if not lookup:
                took = (
                    format_duration(time.monotonic() - self._process_started)
                    if self._process_started
                    else "?"
                )
                expected = source.member_count or 0
                short = ""
                if source.needs_gateway and expected and len(by_id) < expected * 0.99:
                    pct = 100.0 * len(by_id) / expected
                    short = (
                        f" ({pct:.0f}% of {expected:,} - Discord hides offline "
                        f"members from large member lists)"
                    )
                self._set_status(
                    f"Listed {len(by_id):,} members of {source.name} in {took}"
                    f"{short} - nothing looked up. Press S to check one, s for all.",
                    "yellow" if short else "",
                )
                self.query_one("#results", DataTable).focus()
                return

            threats = sum(1 for r in self.rows.values() if r.report.verdict is Verdict.THREAT)
            note = f" ({truncated:,} skipped by max_members)" if truncated else ""
            verdict_note = (
                f"{threats} flagged as THREAT" if threats else "no THREAT verdicts"
            )
            coverage = ""
            expected = source.member_count or 0
            held = len(self.rows)
            if source.needs_gateway and expected and held < expected * 0.99:
                pct = 100.0 * held / expected
                guild = source.guild
                if guild is not None and not guild.can_chunk:
                    why = (
                        "Discord only exposes the full member list to accounts "
                        "with kick, ban or manage-roles here; without those, "
                        "offline members in a large guild are unreachable"
                    )
                else:
                    why = "Discord did not return the rest"
                coverage = (
                    f"  [{pct:.0f}% coverage - {expected - held:,} not reached. {why}]"
                )
            scanned_now = len(by_id)
            merged = ""
            if merging:
                bits = [f"added to {carried:,} already held"]
                if skipped[0]:
                    bits.append(f"{skipped[0]:,} already checked, skipped")
                bits.append(f"{len(self.rows):,} in total now")
                merged = "  (" + "; ".join(bits) + ")"
            plural = "" if scanned_now == 1 else "s"
            took = (
                format_duration(time.monotonic() - self._process_started)
                if self._process_started
                else "?"
            )
            self._set_status(
                f"Scanned {scanned_now:,} member{plural}{note} in {took}{merged} - "
                f"{verdict_note}.{coverage}",
                "bold red" if threats else ("yellow" if coverage else ""),
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
            self.report_error(
                exc,
                context=f"while scanning {source.name}",
                retry=lambda: self.scan_source(source, lookup=lookup, mode=mode),
            )
        except asyncio.CancelledError:
            self._end_run()
            raise
        finally:
            progress.remove_class("visible")
            task = self._scan_task
            if task is not None and not task.done():
                task.cancel()
            self._eta_progress = None
            self._eta_ready = False
            self._process_started = None
            self._scan_task = None

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
        if not row.checked:
            # listed but never looked up -- there is no verdict to filter on,
            # so only the verdict-specific filters exclude them
            if mode in (FilterMode.THREATS, FilterMode.ATTENTION):
                return False
        elif mode is FilterMode.FINDINGS and verdict in self.hidden_verdicts:
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

        if not row.checked:
            return [
                Text(row.member.display_name, overflow="ellipsis"),
                Text("not checked", style="dim"),
                Text("-", style="dim"),
                Text("-", style="dim"),
                Text("-", style="dim"),
                Text("-", style="dim", justify="right"),
            ]

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
        """Add a streaming result, if it belongs on the page being viewed."""
        row = self.rows[discord_id]
        if not self._passes(row):
            return
        self._matching += 1
        if self.page_size:
            # only the last page grows; earlier pages are already full and
            # rewriting them mid-scan would fight the user's scrolling
            if self._page != self.page_count - 1:
                return
            if len(self._shown) >= self.page_size:
                return
        table = self.query_one("#results", DataTable)
        table.add_row(*self._cells(row), key=discord_id)
        self._shown.append(discord_id)

    def _matching_rows(self) -> list[Row]:
        index, descending = self._result_sort
        ordered = sorted(
            self.rows.values(), key=RESULT_SORTS[index][1], reverse=descending
        )
        return [row for row in ordered if self._passes(row)]

    @property
    def page_count(self) -> int:
        if not self.page_size:
            return 1
        return max(1, (self._matching + self.page_size - 1) // self.page_size)

    def _rebuild_table(self) -> None:
        table = self.query_one("#results", DataTable)
        table.clear()
        self._shown.clear()

        matching = self._matching_rows()
        self._matching = len(matching)
        self._page = max(0, min(self._page, self.page_count - 1))

        if self.page_size:
            start = self._page * self.page_size
            page = matching[start : start + self.page_size]
        else:
            page = matching

        for row in page:
            table.add_row(*self._cells(row), key=row.member.id)
            self._shown.append(row.member.id)
        self._update_summary()

    def action_next_page(self) -> None:
        if self._page + 1 >= self.page_count:
            self._set_status(f"Already on the last page ({self.page_count}).")
            return
        self._page += 1
        self._rebuild_table()

    def action_prev_page(self) -> None:
        if self._page == 0:
            self._set_status("Already on the first page.")
            return
        self._page -= 1
        self._rebuild_table()

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
        hidden = len(self.rows) - self._matching
        tail = f"   filter: {self.filter_mode.value}"
        if hidden > 0:
            tail += f"  ({hidden:,} hidden)"
        if self.page_count > 1:
            tail += (
                f"  page {self._page + 1}/{self.page_count}"
                f" ({listed:,} of {self._matching:,})"
            )
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
        """Search whichever table has focus -- sources or results."""
        search = self.query_one("#search", Input)
        target = self._focused_table_id()

        if "visible" in search.classes:
            search.remove_class("visible")
            search.value = ""
            self.search_term = ""
            self.source_search = ""
            self._page = 0
            self._rebuild_table()
            self.call_later(self._refresh_source_table)
            self.query_one(f"#{self._search_target or 'results'}", DataTable).focus()
            self._search_target = None
            return

        self._search_target = target
        search.placeholder = (
            "Filter sources by name or kind..."
            if target == "guilds"
            else "Filter members by name, ID or Roblox account..."
        )
        search.add_class("visible")
        search.focus()

    def action_close_search(self) -> None:
        if "visible" in self.query_one("#search", Input).classes:
            self.action_search()

    @on(Input.Changed, "#search")
    def _on_search(self, event: Input.Changed) -> None:
        term = event.value.strip()
        if self._search_target == "guilds":
            self.source_search = term
            self.call_later(self._refresh_source_table)
        else:
            self.search_term = term
            self._page = 0
            self._rebuild_table()

    @on(Input.Submitted, "#search")
    def _on_search_submit(self) -> None:
        self.query_one(
            f"#{self._search_target or 'results'}", DataTable
        ).focus()

    def action_reload_guilds(self) -> None:
        if self.http:
            self.reload_guilds_worker()

    @work(exclusive=True, group="guilds")
    async def reload_guilds_worker(self) -> None:
        try:
            await self._load_sources()
        except Exception as exc:  # noqa: BLE001
            self._set_status(str(exc), "bold red")

    # -- export ------------------------------------------------------------

    def _export_rows(self, scope: str) -> list[ExportRow]:
        """Rows for an export.

        ``filtered`` means everything the filter matches, across *every* page --
        not just the page on screen. Paging is a display concern; an export
        that silently dropped the other pages would be data loss dressed up as
        a feature. ``page`` is there for when the visible page really is what
        you want.
        """
        if scope == "all":
            index, descending = self._result_sort
            source = sorted(
                self.rows.values(), key=RESULT_SORTS[index][1], reverse=descending
            )
        elif scope == "page":
            source = [self.rows[key] for key in self._shown if key in self.rows]
        else:
            source = self._matching_rows()
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
        if not self.rows or self.current_source is None:
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
                png_style=settings.png_style,
                filter_name=self.filter_mode.value,
                filtered_count=self._matching,
                page_count=len(self._shown),
                page_number=self._page + 1,
                total_pages=self.page_count,
                total_count=len(self.rows),
                preserve=settings.preserve,
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

        source = self.current_source

        # Cards need each member's avatar and banner, which are not in the
        # member payload -- fetched here rather than inside the renderer so a
        # slow CDN shows up as progress instead of a frozen export.
        profiles: dict = {}
        wants_profiles = (
            choice.png_style in ("cards", "both") and "png" in choice.formats
        )
        if wants_profiles or "html" in choice.formats:
            self._set_activity(f"Fetching {len(rows):,} Discord profiles...")
            try:
                profiles = await fetch_profiles(
                    self.http,
                    [r.discord_id for r in rows],
                    with_images=True,
                    # the member list already carried these, so avatars work
                    # even where the profile route is refused
                    seed_avatars={
                        key: row.member.guild_avatar or row.member.avatar
                        for key, row in self.rows.items()
                        if row.member.guild_avatar or row.member.avatar
                    },
                    guild_id=(
                        source.id
                        if source is not None and source.kind == KIND_GUILD
                        else None
                    ),
                    on_progress=lambda stage, done, of: self._set_activity(
                        f"{stage} - {done:,} / {of:,}"
                    ),
                )
                refused = sum(
                    1 for p in profiles.values()
                    if any("unavailable" in e for e in p.errors)
                )
                if refused:
                    self.log_debug(
                        f"{refused} profile(s) refused by Discord; avatars from "
                        f"the member list used where available",
                        "warn",
                    )
            except Exception as exc:  # noqa: BLE001 - cards degrade gracefully
                self.log_debug(f"profile fetch failed: {exc}", "warn")

        try:
            manifest = render_export(
                rows,
                guild_name=f"{source.name} ({source.label})",
                guild_id=source.id,
                base_directory=Path(settings.directory),
                formats=choice.formats,
                columns=choice.columns,
                scope={
                    "all": "everything scanned",
                    "page": f"page {self._page + 1} of {self.page_count}, "
                            f"filter: {self.filter_mode.value}",
                }.get(choice.scope, f"filter: {self.filter_mode.value}"),
                segment_size=choice.segment_size,
                preserve=choice.preserve,
                png_style=choice.png_style,
                profiles=profiles,
            )
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Export failed: {exc}", "bold red")
            self.report_error(exc, context="while exporting", retry=self.run_export)
            return

        if choice.remember:
            settings.formats = choice.formats
            settings.scope = choice.scope
            settings.segment_size = choice.segment_size
            settings.columns = choice.columns
            settings.preserve = choice.preserve
            settings.png_style = choice.png_style
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
        if manifest.purged:
            swept = f" Cleared {len(manifest.purged)} expired export(s)."
        elif choice.preserve:
            swept = " Older exports preserved."
        else:
            swept = ""
        self._set_status(
            f"Exported {manifest.rows:,} members{parts} to {shown}/ "
            f"({', '.join(manifest.formats)}).{remembered}{swept}"
        )

    def action_leave_group(self) -> None:
        source = self._selected_source() or self.current_source
        if source is None or source.kind != KIND_GROUP:
            self._set_status(
                "Select a group DM in the left pane to leave it.", "yellow"
            )
            return
        self.leave_group(source)

    @work(exclusive=True, group="leave")
    async def leave_group(self, source: ScanSource) -> None:
        choice = await self.push_screen_wait(
            LeaveGroupDialog(
                name=source.name,
                member_count=source.member_count or 0,
                silent=self.config.moderation.silent_leave,
            )
        )
        if choice is None:
            self._set_status("Leaving cancelled.")
            return

        self._set_activity(f"Leaving {source.name}...")
        try:
            await self.http.leave_group_dm(source.id, silent=choice.silent)
        except DiscordForbidden:
            self._set_status("Discord refused: cannot leave this group.", "bold red")
            return
        except DiscordNotFound:
            self._set_status("You are not in that group any more.", "yellow")
            return
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Could not leave: {exc}", "bold red")
            return

        remembered = ""
        if choice.remember:
            self.config.moderation.silent_leave = choice.silent
            try:
                saved = self.config.save_moderation_settings()
                remembered = f" Saved silent_leave={str(choice.silent).lower()} to {saved.name}."
            except OSError as exc:
                remembered = f" (could not save: {exc})"

        how = "silently" if choice.silent else "with a farewell message"
        self._set_status(f"Left {source.name} {how}.{remembered}")

        # the group is gone; drop it from the list and clear its results
        self.sources = [s for s in self.sources if s is not source]
        if self.current_source is source:
            self.current_source = None
            self.rows.clear()
            self._shown.clear()
            self.query_one("#results", DataTable).clear()
            self._update_summary()
        await self._refresh_source_table()

    async def _refresh_source_table(self, focus_key: str | None = None) -> None:
        """Rebuild the sources pane, grouped and collapsible.

        DataTable has no tree mode, so groups are header rows that hide their
        children when collapsed. Keeping it a DataTable means sorting, keys and
        selection all keep working exactly as before.
        """
        table = self.query_one("#guilds", DataTable)
        # remember where the cursor was, so a rebuild does not throw the user
        # back to the top of the list
        if focus_key is None:
            try:
                index = table.cursor_row
                if 0 <= index < len(self._source_rows):
                    focus_key = self._source_rows[index]
            except Exception:
                focus_key = None
        table.clear()
        self._source_rows = []

        pool = self.sources
        if self.source_search:
            needle = self.source_search.lower()
            pool = [
                s for s in pool
                if needle in s.name.lower() or needle in s.label.lower()
            ]

        if self._source_sort is None:
            ordered = sorted(pool, key=lambda s: s.sort_key)
        else:
            index, descending = self._source_sort
            ordered = sorted(
                pool, key=SOURCE_SORTS[index][1], reverse=descending
            )

        if self.source_search and not ordered:
            table.add_row(
                Text(f"no source matches {self.source_search!r}", style="dim"),
                Text(""),
                Text(""),
                key=f"{GROUP_KEY}__none__",
            )
            self._source_rows.append(f"{GROUP_KEY}__none__")
            return

        for title, _kinds in GROUPS:
            members = [s for s in ordered if group_for(s.kind) == title]
            if not members:
                continue
            collapsed = title in self._collapsed
            total = sum(s.member_count or 0 for s in members)
            header = Text()
            header.append("> " if collapsed else "v ", style="bold cyan")
            header.append(title.upper(), style="bold")
            table.add_row(
                header,
                # left-aligned so it lines up with the "server" / "group DM"
                # labels on the rows beneath it
                Text(f"{len(members)} item(s)", style="dim"),
                Text(f"{total:,}" if total else "", style="dim", justify="right"),
                key=f"{GROUP_KEY}{title}",
            )
            self._source_rows.append(f"{GROUP_KEY}{title}")
            if collapsed:
                continue
            for source in members:
                count = f"{source.member_count:,}" if source.member_count else "?"
                style = "bold cyan" if source.kind == KIND_REQUESTS else ""
                name = Text("  ", style="dim")
                name.append(source.name, style=style)
                table.add_row(
                    name,
                    Text(source.label, style="dim"),
                    Text(count, justify="right"),
                    key=f"{source.kind}:{source.id}",
                )
                self._source_rows.append(f"{source.kind}:{source.id}")

        self._restore_source_cursor(focus_key)

    def _restore_source_cursor(self, key: str | None) -> None:
        """Put the cursor back on ``key``, or as near to it as still exists."""
        if not self._source_rows:
            return
        table = self.query_one("#guilds", DataTable)
        if key and key in self._source_rows:
            table.move_cursor(row=self._source_rows.index(key))
            return
        if key and not key.startswith(GROUP_KEY):
            # the row was folded away; settle on its group header instead
            source = next(
                (s for s in self.sources if f"{s.kind}:{s.id}" == key), None
            )
            if source is not None:
                header = f"{GROUP_KEY}{group_for(source.kind)}"
                if header in self._source_rows:
                    table.move_cursor(row=self._source_rows.index(header))
                    return
        first = next(
            (i for i, k in enumerate(self._source_rows)
             if not k.startswith(GROUP_KEY)),
            0,
        )
        table.move_cursor(row=first)

    def _toggle_group(self, title: str) -> None:
        """Fold or unfold a group, leaving the cursor on its header."""
        if title in self._collapsed:
            self._collapsed.discard(title)
        else:
            self._collapsed.add(title)
        header = f"{GROUP_KEY}{title}"
        self.call_later(self._refresh_source_table, header)

    # -- member actions ---    # -- member actions ----------------------------------------------------

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

    def action_purge(self) -> None:
        row = self._selected_row()
        if row is None:
            self._set_status("No member selected.", "yellow")
            return
        if self.current_source is None or self.http is None:
            return
        self.purge_messages(row)

    @work(exclusive=True, group="purge")
    async def purge_messages(self, row: Row) -> None:
        source = self.current_source
        settings = self.config.purge

        # Where is there actually a conversation with this person?
        if source.kind == KIND_GROUP:
            target = PurgeTarget(
                channel_id=source.id, label=f"group {source.name}", kind=PURGE_GROUP
            )
        else:
            self._set_activity(f"Looking for a DM with {row.member.display_name}...")
            try:
                channel_id = await self.http.find_dm_channel(row.member.id)
            except Exception as exc:  # noqa: BLE001
                self._set_status(f"Could not check for a DM: {exc}", "bold red")
                return
            if channel_id is None:
                self._set_status(
                    f"No open DM with {row.member.display_name} - nothing of "
                    f"yours to remove.",
                    "yellow",
                )
                return
            target = PurgeTarget(
                channel_id=channel_id,
                label=f"DM with {row.member.display_name}",
                kind=KIND_DM,
            )

        choice = await self.push_screen_wait(
            PurgePlanDialog(
                target_label=target.label,
                member_label=f"{row.member.display_name} ({row.member.tag})",
                is_group=target.is_group,
                max_messages=settings.max_messages,
                max_age_days=settings.max_age_days,
                delete_delay=settings.delete_delay,
            )
        )
        if choice is None:
            self._set_status("Purge cancelled.")
            return

        stop = False

        def should_stop() -> bool:
            return stop

        def progress(stage: str, done: int, total: int) -> None:
            self._set_activity(f"{stage} - {done:,} / {total:,}")

        try:
            plan = await plan_purge(
                self.http,
                target,
                own_id=self.my_id or "",
                max_messages=choice.max_messages,
                max_age_days=choice.max_age_days,
                on_progress=progress,
                should_stop=should_stop,
            )
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Could not read history: {exc}", "bold red")
            self.report_error(
                exc,
                context=f"while reading {target.label}",
                retry=lambda: self.purge_messages(row),
            )
            return

        if not plan.count:
            self._set_status(plan.describe(), "yellow")
            return

        estimated = plan.count * max(0.0, choice.delete_delay)
        confirmed = await self.push_screen_wait(
            PurgeConfirmDialog(plan, estimated_seconds=estimated)
        )
        if not confirmed:
            self._set_status(f"Nothing deleted. {plan.describe()}")
            return

        if choice.remember:
            settings.max_messages = choice.max_messages
            settings.max_age_days = choice.max_age_days
            settings.delete_delay = choice.delete_delay
            try:
                self.config.save_purge_settings()
            except OSError:
                pass

        result = await execute_purge(
            self.http,
            plan,
            delete_delay=choice.delete_delay,
            on_progress=progress,
            should_stop=should_stop,
        )
        note = f" ({'; '.join(result.errors)})" if result.errors else ""
        self._set_status(
            f"{target.label}: {result.describe()}.{note} "
            f"Their messages are untouched - Discord allows no other way.",
            "bold red" if result.failed else "",
        )

    def action_kick(self) -> None:
        self._moderate("kick")

    def action_ban(self) -> None:
        self._moderate("ban")

    def _moderate(self, action: str) -> None:
        row = self._selected_row()
        if row is None:
            self._set_status("No member selected.", "yellow")
            return
        if self.current_source is None or self.http is None:
            return
        self.run_moderation(action, row)

    @work(exclusive=True, group="moderate")
    async def run_moderation(self, action: str, row: Row) -> None:
        source = self.current_source
        if source is None:
            return
        resolved = _ACTIONS[source.kind][action]
        eligibility = check_eligibility(
            row.report, require_threat=self.config.moderation.require_threat
        )
        if not resolved.gated:
            eligibility = Eligibility(
                True,
                False,
                f"{verdict_label(row.report.verdict)}. This one is your call - "
                f"the finding is context, not a precondition.",
            )
        choice = await self.push_screen_wait(
            ModerationDialog(
                action=resolved.name,
                member_label=f"{row.member.display_name} ({row.member.tag})",
                member_id=row.member.id,
                report=row.report,
                eligibility=eligibility,
                template=self.config.moderation.default_reason,
                delete_message_seconds=self.config.moderation.delete_message_seconds,
                wants_purge=resolved.wants_purge,
                note=resolved.note,
            )
        )
        if choice is None:
            self._set_status(f"{resolved.name.capitalize()} cancelled.")
            return

        self._set_activity(f"{resolved.gerund} {row.member.display_name}...")
        try:
            await resolved.run(self, source, row, choice)
        except DiscordForbidden:
            self._set_status(
                f"Cannot {resolved.name} {row.member.display_name}: "
                f"{resolved.forbidden_hint}",
                "bold red",
            )
            return
        except DiscordNotFound:
            self._set_status(
                f"{row.member.display_name} is no longer here.", "yellow"
            )
            return
        except Exception as exc:  # noqa: BLE001
            self._set_status(
                f"{resolved.name.capitalize()} failed: {exc}", "bold red"
            )
            self.report_error(
                exc,
                context=f"while trying to {resolved.name} "
                        f"{row.member.display_name}",
                retry=lambda: self.run_moderation(action, row),
            )
            return

        self._set_status(
            f"{resolved.past.capitalize()} {row.member.display_name}."
            + (f" Reason: {choice.reason}" if resolved.uses_reason else "")
        )
        self._mark_actioned(row, resolved.past)

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
    def _estimate_source(self, event: DataTable.RowHighlighted) -> None:
        """Predict scan duration for the highlighted source."""
        key = event.row_key.value if event.row_key else None
        source = next(
            (s for s in self.sources if f"{s.kind}:{s.id}" == key), None
        )
        if source is None or self.rotector is None:
            return
        if self.workers and any(w.group == "scan" for w in self.workers):
            return  # a scan is running; leave its output alone

        members = source.member_count or 0
        text = Text()
        text.append(f"{source.name}\n", style="bold")
        text.append(
            f"{source.label} - "
            + (f"{members:,} members" if members else "member count unknown"),
            style="dim",
        )
        text.append("\n\n")

        if source.is_complete:
            text.append("Complete coverage\n", style="bold green")
            text.append(
                "This list comes back whole from a single request - every "
                "member is checked, with none of the member-list visibility "
                "caveats a server scan carries.\n\n",
                style="dim",
            )

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
            if source.needs_gateway:
                text.append(
                    "\nRotector lookups only; reading the member list from the "
                    "gateway happens first and is not included. Assumes a "
                    "typical share of members have a linked Roblox account -- "
                    "the live estimate corrects itself once the scan starts.\n",
                    style="dim",
                )

        actions = _ACTIONS.get(source.kind, {})
        if actions:
            text.append(
                "\nActions here: "
                + ", ".join(
                    f"{key} = {spec.name}" for key, spec in actions.items()
                ),
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
        self.log_debug(text, "error" if "red" in style else "info")
        self._paint_status()

    def _set_activity(self, text: str) -> None:
        if self._stopping:
            # A cancelled scan can still have coroutines in flight for a beat;
            # without this they revive the spinner after the user stopped it.
            return
        """Announce what is happening *now*, before the step that blocks.

        Every long-running step calls this on the way in, so the bar always
        names the current action rather than the last one that finished.
        """
        if text != self._activity:
            self._activity = text
            self._activity_started = time.monotonic()
            self.log_debug(text, "scan")
        self._paint_status()

    def _compose_status(self) -> Text:
        """One line: what is happening, then the timers, then the budget.

        The timers and budget are built as discrete trailing fields and the
        *activity* text is what gets truncated to fit. Embedding them in the
        activity string is what made the ETA vanish -- the bar is one line with
        ellipsis overflow, so anything at the end simply got cut off.
        """
        tail = Text(no_wrap=True, overflow="visible", end="")

        if self._activity is not None:
            eta = self._current_eta()
            if eta is not None:
                tail.append("  ETA ", style="dim")
                tail.append(format_duration(eta), style="bold cyan")

            task = time.monotonic() - self._activity_started
            if task >= _ELAPSED_AFTER:
                tail.append("  task ", style="dim")
                tail.append(format_duration(task))

        if self._process_started is not None:
            total = time.monotonic() - self._process_started
            tail.append("  total ", style="dim")
            tail.append(format_duration(total), style="bold")

        if self.rotector is not None:
            state = self.rotector.limiter.snapshot()
            tail.append("   |   ", style="dim")
            pool = self.rotector.pool
            if len(pool.routes) > 1:
                healthy = pool.available_count()
                total_routes = len(pool.routes)
                tail.append(
                    f"routes {healthy}/{total_routes}",
                    style="bold red" if healthy == 0
                    else ("yellow" if healthy < total_routes else ""),
                )
                tail.append("   ", style="dim")
            if state.blocked_for > 0:
                tail.append(
                    f"rate limit hold {state.blocked_for:.1f}s", style="yellow"
                )
            else:
                tail.append(f"budget {state.available}/{state.limit - state.reserve}")

        # Nothing is truncated any more: the strip scrolls, so long messages
        # stay readable instead of being cut off with no sign of it.
        if self._activity is not None:
            frame = _SPINNER[self._spinner_frame % len(_SPINNER)]
            head = Text(no_wrap=True, overflow="visible", end="")
            head.append(f" {frame} ", style="bold cyan")
            head.append(self._activity)
        else:
            head = Text(
                f" {self._status_text}",
                style=self._status_style,
                no_wrap=True,
                overflow="visible",
                end="",
            )

        head.append_text(tail)
        return head

    def _current_eta(self) -> float | None:
        """Seconds remaining for the phase in flight, if it can be known yet."""
        if self._eta_progress is None:
            return None
        done, total = self._eta_progress
        if not total or done >= total:
            return None
        if not self._eta_ready:
            # while the member list is still growing, "remaining" is a moving
            # target and any figure would be a guess dressed as a measurement
            return None
        return self._eta.eta(done, total)

    def _paint_status(self) -> None:
        try:
            self.query_one(StatusStrip).set_text(self._compose_status())
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
