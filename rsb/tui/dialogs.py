"""Modal dialogs: export options, and the kick/ban flow."""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Button,
    Checkbox,
    Input,
    Label,
    RadioButton,
    RadioSet,
    SelectionList,
    Static,
)

from ..export import ALL_COLUMNS, DEFAULT_COLUMNS
from ..moderation import Eligibility, build_reason
from ..verdict import verdict_label, verdict_style

class DismissOnOutsideClick:
    """Clicking the dimmed area around a dialog closes it.

    Modals are meant to be easy to back out of. Escape already works, but
    clicking away is what most people try first, and having nothing happen
    reads as the app being stuck.
    """

    def on_click(self, event) -> None:
        try:
            panel = self.query_one("#panel")
        except Exception:
            return
        if panel.region.contains(event.screen_x, event.screen_y):
            return
        event.stop()
        self._dismiss_from_outside()

    def _dismiss_from_outside(self) -> None:
        # subclasses override when their result type is not Optional
        self.dismiss(None)


_DIALOG_CSS = """
    #panel {
        width: 78;
        max-height: 90%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    #panel > .title { text-style: bold; padding-bottom: 1; }
    .section { padding-top: 1; color: $text-muted; text-style: bold; }
    /* the fields scroll; the buttons are docked so they cannot be pushed
       off the bottom by a long form */
    #fields, #body { height: 1fr; overflow-y: auto; overflow-x: hidden; }
    #buttons {
        height: auto;
        padding-top: 1;
        align-horizontal: right;
        dock: bottom;
        background: $surface;
    }
    Button { margin-left: 1; }
    SelectionList { height: auto; max-height: 10; border: round $panel-lighten-2; }
    RadioSet { height: auto; border: round $panel-lighten-2; }
"""


@dataclass
class ExportChoice:
    formats: list[str]
    scope: str
    segment_size: int
    columns: list[str]
    preserve: bool = False
    remember: bool = False
    png_style: str = "table"


class ExportDialog(DismissOnOutsideClick, ModalScreen[ExportChoice | None]):
    """Pick formats, scope, segmentation and columns for one export."""

    CSS = "ExportDialog { align: center middle; }" + _DIALOG_CSS
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        formats: list[str],
        scope: str,
        segment_size: int,
        columns: list[str],
        filter_name: str,
        filtered_count: int,
        total_count: int,
        preserve: bool = False,
        png_style: str = "table",
        page_count: int = 0,
        page_number: int = 1,
        total_pages: int = 1,
    ) -> None:
        super().__init__()
        self._preserve = preserve
        self._png_style = png_style
        self._formats = [f.lower() for f in formats] or ["csv"]
        self._scope = scope if scope in ("filtered", "page", "all") else "filtered"
        self._segment_size = segment_size
        self._columns = columns or list(DEFAULT_COLUMNS)
        self.filter_name = filter_name
        self.filtered_count = filtered_count
        self.total_count = total_count
        self.page_count = page_count
        self.page_number = page_number
        self.total_pages = total_pages

    def compose(self) -> ComposeResult:
        with Vertical(id="panel"):
            yield Static("Export results", classes="title")

            with VerticalScroll(id="fields"):
                yield from self._fields()

            with Horizontal(id="buttons"):
                yield Button("Cancel", variant="default", id="cancel")
                yield Button("Export", variant="primary", id="confirm")

    def _fields(self) -> ComposeResult:
            yield Static("Formats", classes="section")
            yield SelectionList[str](
                ("CSV  - segmented spreadsheet", "csv", "csv" in self._formats),
                ("TXT  - readable report", "txt", "txt" in self._formats),
                ("JSON - full structured data", "json", "json" in self._formats),
                (
                    "PNG  - the table as an image, for pasting into a report",
                    "png",
                    "png" in self._formats,
                ),
                (
                    "HTML - one searchable page, table and cards together",
                    "html",
                    "html" in self._formats,
                ),
                id="formats",
            )

            yield Static("What to export", classes="section")
            with RadioSet(id="scope"):
                yield RadioButton(
                    f"Current filter: {self.filter_name} "
                    f"({self.filtered_count:,} members, all pages)",
                    value=self._scope == "filtered",
                    id="scope-filtered",
                )
                yield RadioButton(
                    f"This page only ({self.page_count:,} members, "
                    f"page {self.page_number} of {self.total_pages})",
                    value=self._scope == "page",
                    id="scope-page",
                )
                yield RadioButton(
                    f"Everything scanned ({self.total_count:,} members)",
                    value=self._scope == "all",
                    id="scope-all",
                )

            yield Static("PNG style", classes="section")
            with RadioSet(id="png-style"):
                yield RadioButton(
                    "Table - the results grid as one image",
                    value=self._png_style != "cards",
                    id="png-table",
                )
                yield RadioButton(
                    "Cards - a Discord-style profile per member, with avatar "
                    "and banner",
                    value=self._png_style == "cards",
                    id="png-cards",
                )
                yield RadioButton(
                    "Both - the table and a card for every member",
                    value=self._png_style == "both",
                    id="png-both",
                )
            yield Static(
                Text(
                    "Cards fetch each member's avatar and banner from Discord, "
                    "so they take longer and need network.",
                    style="dim",
                )
            )

            yield Static("Rows per CSV segment (0 = one file)", classes="section")
            yield Input(
                value=str(self._segment_size),
                placeholder="1000",
                id="segment",
                type="integer",
            )

            yield Static("Columns", classes="section")
            yield SelectionList[str](
                *(
                    (name.replace("_", " "), name, name in self._columns)
                    for name in ALL_COLUMNS
                ),
                id="columns",
            )

            yield Static("Retention", classes="section")
            yield Checkbox(
                "Preserve older exports (skip the 24h cleanup)",
                self._preserve,
                id="preserve",
            )
            yield Static(
                Text(
                    "Off, export folders older than 24 hours are removed. "
                    "Rotector's terms forbid keeping their data longer than "
                    "that; preserving it makes that your responsibility.",
                    style="dim",
                )
            )

            yield Checkbox(
                "Remember these settings in config.toml", False, id="remember"
            )

    def _chosen_png_style(self) -> str:
        if self.query_one("#png-both", RadioButton).value:
            return "both"
        if self.query_one("#png-cards", RadioButton).value:
            return "cards"
        return "table"

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#confirm")
    def _confirm(self) -> None:
        formats = list(self.query_one("#formats", SelectionList).selected)
        columns = [c for c in ALL_COLUMNS if c in set(
            self.query_one("#columns", SelectionList).selected
        )]
        if not formats:
            self.notify("Pick at least one format.", severity="warning")
            return
        if not columns:
            self.notify("Pick at least one column.", severity="warning")
            return

        try:
            segment = max(0, int(self.query_one("#segment", Input).value or 0))
        except ValueError:
            segment = 0

        if self.query_one("#scope-all", RadioButton).value:
            scope = "all"
        elif self.query_one("#scope-page", RadioButton).value:
            scope = "page"
        else:
            scope = "filtered"
        self.dismiss(
            ExportChoice(
                formats=formats,
                scope=scope,
                segment_size=segment,
                columns=columns,
                preserve=self.query_one("#preserve", Checkbox).value,
                png_style=self._chosen_png_style(),
                remember=self.query_one("#remember", Checkbox).value,
            )
        )


@dataclass
class ModerationChoice:
    action: str                      # "kick" | "ban"
    reason: str
    delete_message_seconds: int = 0
    acknowledged_override: bool = False
    #: try to DM them before acting
    notify: bool = False
    #: remember the notify choice as the new default
    remember_notify: bool = False


class ModerationDialog(DismissOnOutsideClick, ModalScreen[ModerationChoice | None]):
    """Confirm a kick or ban, with an automatic or hand-written reason.

    The dialog always shows the exact reason that will be written to the audit
    log, and refuses to proceed on a non-actionable finding until the operator
    explicitly acknowledges that Rotector does not support the action.
    """

    CSS = "ModerationDialog { align: center middle; }" + _DIALOG_CSS
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        action: str,
        member_label: str,
        member_id: str,
        report,
        eligibility: Eligibility,
        template: str,
        delete_message_seconds: int = 0,
        wants_purge: bool = False,
        note: str = "",
        can_notify: bool = False,
        notify: bool = False,
        appeal=None,
    ) -> None:
        super().__init__()
        self.appeal = appeal
        self.action = action
        self.can_notify = can_notify
        # NOT self.notify -- that is Textual's own method on a Screen, and
        # shadowing it with a bool breaks every warning this dialog raises
        self.notify_default = notify
        self.wants_purge = wants_purge
        self.note = note
        self.member_label = member_label
        self.member_id = member_id
        self.report = report
        self.eligibility = eligibility
        self.template = template
        self.delete_message_seconds = delete_message_seconds

    def compose(self) -> ComposeResult:
        verb = self.action.capitalize()
        with Vertical(id="panel"):
            yield Static(f"{verb} member", classes="title")

            header = Text()
            header.append(self.member_label, style="bold")
            header.append(f"   id {self.member_id}\n", style="dim")
            header.append(
                verdict_label(self.report.verdict),
                style=verdict_style(self.report.verdict),
            )
            yield Static(header)

            warning = Text()
            if self.eligibility.needs_override:
                warning.append("\nNot supported by the finding\n", style="bold red")
                warning.append(self.eligibility.explanation, style="yellow")
            else:
                warning.append(f"\n{self.eligibility.explanation}", style="green")
            yield Static(warning)

            if self.note:
                yield Static(Text(f"\n{self.note}", style="dim"))

            yield Static("Reason", classes="section")
            with RadioSet(id="reason-mode"):
                yield RadioButton("Automatic", value=True, id="reason-auto")
                yield RadioButton("Custom", id="reason-custom")
            yield Input(placeholder="Your own reason...", id="reason-text")
            yield Static("", id="reason-preview")

            if self.wants_purge:
                yield Static("Delete recent messages (seconds)", classes="section")
                yield Input(
                    value=str(self.delete_message_seconds),
                    id="delete-seconds",
                    type="integer",
                )

            if self.can_notify:
                yield Static("Before acting", classes="section")
                yield Checkbox(
                    "Try to DM them first, so they know why and can appeal",
                    self.notify_default,
                    id="notify",
                )
                yield Static(
                    Text(
                        "Sent before the action, because a banned account "
                        "shares no server with you and cannot be reached "
                        "afterwards. A closed DM does not stop the action.",
                        style="dim",
                    )
                )
                yield Checkbox("Remember this", False, id="remember-notify")

            if self.eligibility.needs_override:
                yield Static(
                    "Confirm" if self.eligibility.needs_double_confirm else "",
                    classes="section",
                )
                yield Checkbox(
                    "I understand Rotector does not support this action",
                    False,
                    id="override",
                )
                if self.eligibility.needs_double_confirm:
                    yield Static(
                        Text(
                            "This is a CAUTION finding. Rotector found "
                            "something and deliberately did not conclude "
                            "from it, so this may well be wrong about a real "
                            "person. Type the word below to confirm you "
                            "have decided that yourself.",
                            style="yellow",
                        )
                    )
                    yield Input(
                        placeholder=f"type {self.action.upper()} to confirm",
                        id="double-confirm",
                    )

            with Horizontal(id="buttons"):
                yield Button("Cancel", variant="default", id="cancel")
                yield Button(verb, variant="error", id="confirm")

    def on_mount(self) -> None:
        self._refresh_preview()

    def _current_reason(self) -> str:
        custom = None
        if self.query_one("#reason-custom", RadioButton).value:
            custom = self.query_one("#reason-text", Input).value
        return build_reason(self.report, self.template, custom, appeal=self.appeal)

    def _refresh_preview(self) -> None:
        preview = Text()
        preview.append("Audit log will read:\n", style="dim")
        preview.append(self._current_reason(), style="italic")
        self.query_one("#reason-preview", Static).update(preview)

    @on(Input.Changed, "#reason-text")
    @on(RadioSet.Changed, "#reason-mode")
    def _on_change(self) -> None:
        self._refresh_preview()

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#confirm")
    def _confirm(self) -> None:
        acknowledged = True
        if self.eligibility.needs_override:
            acknowledged = self.query_one("#override", Checkbox).value
            if not acknowledged:
                self.notify(
                    "Tick the acknowledgement, or cancel.", severity="warning"
                )
                return
            if not self.eligibility.allowed:
                self.notify(
                    "Blocked by moderation.require_threat in config.toml.",
                    severity="error",
                )
                return
            if self.eligibility.needs_double_confirm:
                typed = self.query_one("#double-confirm", Input).value.strip()
                if typed.upper() != self.action.upper():
                    self.notify(
                        f"Type {self.action.upper()} to confirm. A CAUTION "
                        f"finding is not evidence enough to act on by "
                        f"reflex.",
                        severity="error",
                    )
                    return

        seconds = 0
        if self.wants_purge:
            try:
                seconds = int(self.query_one("#delete-seconds", Input).value or 0)
            except ValueError:
                seconds = 0

        wants_notice = False
        remember = False
        if self.can_notify:
            wants_notice = self.query_one("#notify", Checkbox).value
            remember = self.query_one("#remember-notify", Checkbox).value

        self.dismiss(
            ModerationChoice(
                action=self.action,
                reason=self._current_reason(),
                delete_message_seconds=seconds,
                acknowledged_override=acknowledged,
                notify=wants_notice,
                remember_notify=remember,
            )
        )


@dataclass
class LeaveChoice:
    silent: bool
    remember: bool = False


class LeaveGroupDialog(DismissOnOutsideClick, ModalScreen[LeaveChoice | None]):
    """Confirm leaving a group DM, with the silent option."""

    CSS = "LeaveGroupDialog { align: center middle; }" + _DIALOG_CSS
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, name: str, member_count: int, silent: bool) -> None:
        super().__init__()
        self.group_name = name
        self.member_count = member_count
        self._silent = silent

    def compose(self) -> ComposeResult:
        with Vertical(id="panel"):
            yield Static("Leave group DM", classes="title")

            header = Text()
            header.append(self.group_name, style="bold")
            header.append(f"\n{self.member_count} other member(s)\n", style="dim")
            yield Static(header)

            yield Static(
                Text(
                    "You will stop receiving messages from this group. Rejoining "
                    "needs someone still in it to add you back.",
                    style="yellow",
                )
            )

            yield Static("Options", classes="section")
            yield Checkbox(
                'Leave silently (no "left the group" message)',
                self._silent,
                id="silent",
            )
            yield Checkbox(
                "Remember this silent setting in config.toml", False, id="remember"
            )

            with Horizontal(id="buttons"):
                yield Button("Cancel", variant="default", id="cancel")
                yield Button("Leave", variant="error", id="confirm")

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#confirm")
    def _confirm(self) -> None:
        self.dismiss(
            LeaveChoice(
                silent=self.query_one("#silent", Checkbox).value,
                remember=self.query_one("#remember", Checkbox).value,
            )
        )


@dataclass
class PurgeChoice:
    max_messages: int
    max_age_days: int
    delete_delay: float
    remember: bool = False


class PurgePlanDialog(DismissOnOutsideClick, ModalScreen[PurgeChoice | None]):
    """Choose how far back to look. Deletes nothing -- this only builds a plan."""

    CSS = "PurgePlanDialog { align: center middle; }" + _DIALOG_CSS
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        target_label: str,
        member_label: str,
        is_group: bool,
        max_messages: int,
        max_age_days: int,
        delete_delay: float,
    ) -> None:
        super().__init__()
        self.target_label = target_label
        self.member_label = member_label
        self.is_group = is_group
        self._max_messages = max_messages
        self._max_age_days = max_age_days
        self._delete_delay = delete_delay

    def compose(self) -> ComposeResult:
        with Vertical(id="panel"):
            yield Static("Purge your messages", classes="title")

            header = Text()
            header.append(self.target_label, style="bold")
            header.append(f"\nwith {self.member_label}\n", style="dim")
            yield Static(header)

            scope = Text()
            scope.append("\nOnly your own messages are deleted. ", style="bold yellow")
            scope.append(
                "Discord provides no way to remove someone else's messages from "
                "a conversation, so their side stays exactly where it is.",
                style="yellow",
            )
            if self.is_group:
                scope.append(
                    "\n\nThis is a group DM, so this removes every message you "
                    "sent to the group - not only the ones aimed at this person.",
                    style="bold yellow",
                )
            yield Static(scope)

            yield Static("How far back", classes="section")
            yield Static(Text("Days of history (0 = all)", style="dim"))
            yield Input(value=str(self._max_age_days), id="days", type="integer")
            yield Static(Text("Most messages to remove (0 = no cap)", style="dim"))
            yield Input(value=str(self._max_messages), id="cap", type="integer")

            yield Static("Pacing", classes="section")
            yield Static(
                Text(
                    "Seconds between deletions. Discord's delete limit is strict; "
                    "going faster mostly buys 429s.",
                    style="dim",
                )
            )
            yield Input(value=str(self._delete_delay), id="delay")

            yield Checkbox(
                "Remember these settings in config.toml", False, id="remember"
            )

            with Horizontal(id="buttons"):
                yield Button("Cancel", variant="default", id="cancel")
                yield Button("Preview", variant="primary", id="confirm")

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#confirm")
    def _confirm(self) -> None:
        def number(widget_id: str, default):
            raw = self.query_one(f"#{widget_id}", Input).value.strip()
            try:
                return type(default)(raw) if raw else default
            except ValueError:
                return default

        self.dismiss(
            PurgeChoice(
                max_messages=max(0, number("cap", 0)),
                max_age_days=max(0, number("days", 0)),
                delete_delay=max(0.0, number("delay", 1.0)),
                remember=self.query_one("#remember", Checkbox).value,
            )
        )


class PurgeConfirmDialog(DismissOnOutsideClick, ModalScreen[bool]):
    """Clicking away means "no", which is the safe reading for a deletion."""
    """Show exactly what will be deleted, and require a typed confirmation."""

    CSS = "PurgeConfirmDialog { align: center middle; }" + _DIALOG_CSS
    BINDINGS = [("escape", "cancel", "Cancel")]

    def _dismiss_from_outside(self) -> None:
        self.dismiss(False)

    def __init__(self, plan, estimated_seconds: float) -> None:
        super().__init__()
        self.plan = plan
        self.estimated_seconds = estimated_seconds

    def compose(self) -> ComposeResult:
        from ..eta import format_duration

        with Vertical(id="panel"):
            yield Static("Confirm deletion", classes="title")

            summary = Text()
            summary.append(f"{self.plan.count:,}", style="bold red")
            summary.append(" of your messages will be permanently deleted from ")
            summary.append(f"{self.plan.target.label}.\n", style="bold")
            summary.append(
                f"Scanned {self.plan.scanned:,} messages across "
                f"{self.plan.pages} page(s). "
                f"Estimated time: {format_duration(self.estimated_seconds)}.\n",
                style="dim",
            )
            summary.append("\nThis cannot be undone.", style="bold red")
            yield Static(summary)

            if self.plan.messages:
                yield Static("Oldest first", classes="section")
                preview = Text()
                ordered = sorted(self.plan.messages, key=lambda m: m.timestamp)
                for message in ordered[:6]:
                    preview.append(f"  {message.when}  ", style="dim")
                    preview.append(f"{message.preview}\n")
                if len(ordered) > 6:
                    preview.append(
                        f"  ... and {len(ordered) - 6:,} more\n", style="dim"
                    )
                yield Static(preview)

            yield Static("Type DELETE to confirm", classes="section")
            yield Input(placeholder="DELETE", id="confirm-text")

            with Horizontal(id="buttons"):
                yield Button("Cancel", variant="default", id="cancel")
                yield Button("Delete", variant="error", id="confirm")

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm")
    def _confirm(self) -> None:
        typed = self.query_one("#confirm-text", Input).value.strip()
        if typed != "DELETE":
            self.notify("Type DELETE to confirm.", severity="warning")
            return
        self.dismiss(True)


@dataclass
class ScanChoice:
    """How a new scan relates to results already on screen."""

    mode: str            # "replace" | "merge_skip" | "merge_recheck"
    remember: bool = False


class RescanDialog(DismissOnOutsideClick, ModalScreen[ScanChoice | None]):
    """Asked when a scan starts and there are already results.

    Merging is worth offering rather than always wiping, because a member list
    is not the same twice: an unprivileged scan sees whoever was online, so
    running it again later reaches people the first pass could not. Wiping
    throws that accumulation away.
    """

    CSS = "RescanDialog { align: center middle; }" + _DIALOG_CSS
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, source_name: str, existing: int, default: str) -> None:
        super().__init__()
        self.source_name = source_name
        self.existing = existing
        self._default = default if default in _MODES else "replace"

    def compose(self) -> ComposeResult:
        with Vertical(id="panel"):
            yield Static("Scan again", classes="title")

            header = Text()
            header.append(f"{self.existing:,} members", style="bold")
            header.append(" are already on screen.\n", style="")
            header.append(
                "A member list is not the same twice -- without elevated "
                "permissions a scan only sees who was online -- so scanning "
                "again later reaches people the last pass could not.",
                style="dim",
            )
            yield Static(header)

            yield Static("What to do with them", classes="section")
            with RadioSet(id="mode"):
                yield RadioButton(
                    "Start fresh - discard the current results",
                    value=self._default == "replace",
                    id="mode-replace",
                )
                yield RadioButton(
                    "Add to them - only look up members not already checked",
                    value=self._default == "merge_skip",
                    id="mode-merge-skip",
                )
                yield RadioButton(
                    "Add to them - re-check everyone (flags change over time)",
                    value=self._default == "merge_recheck",
                    id="mode-merge-recheck",
                )

            yield Checkbox("Remember this choice in config.toml", False, id="remember")

            with Horizontal(id="buttons"):
                yield Button("Cancel", variant="default", id="cancel")
                yield Button("Scan", variant="primary", id="confirm")

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#confirm")
    def _confirm(self) -> None:
        if self.query_one("#mode-replace", RadioButton).value:
            mode = "replace"
        elif self.query_one("#mode-merge-skip", RadioButton).value:
            mode = "merge_skip"
        else:
            mode = "merge_recheck"
        self.dismiss(
            ScanChoice(mode=mode, remember=self.query_one("#remember", Checkbox).value)
        )


_MODES = ("replace", "merge_skip", "merge_recheck")


@dataclass
class BulkChoice:
    """What a confirmed bulk action should do."""

    scope: str
    custom: str | None = None
    delete_message_seconds: int = 0
    acknowledged_override: bool = False
    notify: bool = False


class BulkActionDialog(DismissOnOutsideClick, ModalScreen[BulkChoice | None]):
    """Confirm an action against many members at once.

    Bulk moderation is the least recoverable thing this program does, so the
    dialog is deliberately harder to get through than the single-member one:
    it names everyone who would be affected, it separates out those the finding
    does not support instead of quietly including them, and it will not act
    until the exact number of targets has been typed by hand. Clicking through
    without reading should not be possible.
    """

    CSS = "BulkActionDialog { align: center middle; }" + _DIALOG_CSS
    BINDINGS = [("escape", "cancel", "Cancel")]

    #: how many names to list before summarising the rest
    PREVIEW = 12

    def __init__(
        self,
        action: str,
        resolve,
        scopes,
        *,
        has_selection: bool,
        wants_purge: bool = False,
        uses_reason: bool = True,
        delete_message_seconds: int = 0,
        note: str = "",
        can_notify: bool = False,
        notify: bool = False,
    ) -> None:
        super().__init__()
        self.can_notify = can_notify
        self.notify_default = notify
        self.action = action
        self.resolve = resolve
        self.scopes = scopes
        self.has_selection = has_selection
        self.wants_purge = wants_purge
        self.uses_reason = uses_reason
        self.delete_message_seconds = delete_message_seconds
        self.note = note
        self.scope = scopes[0][0] if scopes else "selected"
        self.plan = None

    def compose(self) -> ComposeResult:
        verb = self.action.capitalize()
        with Vertical(id="panel"):
            yield Static(f"{verb} in bulk", classes="title")
            with VerticalScroll(id="body"):
                yield Static(
                    Text(
                        "This acts on many people at once and cannot be undone.",
                        style="bold yellow",
                    )
                )
                if self.note:
                    yield Static(Text(f"{self.note}\n", style="dim"))

                yield Static("Who", classes="section")
                with RadioSet(id="scope"):
                    for index, (key, label) in enumerate(self.scopes):
                        disabled = key == "selected" and not self.has_selection
                        yield RadioButton(
                            label + (" - none selected" if disabled else ""),
                            value=index == 0 and not disabled,
                            id=f"scope-{key}",
                            disabled=disabled,
                        )

                yield Static("", id="plan-summary")
                yield Static("", id="plan-preview")

                if self.uses_reason:
                    yield Static("Reason", classes="section")
                    with RadioSet(id="reason-mode"):
                        yield RadioButton("Automatic", value=True, id="reason-auto")
                        yield RadioButton("Custom", id="reason-custom")
                    yield Input(placeholder="Your own reason...", id="reason-text")

                if self.wants_purge:
                    yield Static(
                        "Delete recent messages (seconds)", classes="section"
                    )
                    yield Input(
                        value=str(self.delete_message_seconds),
                        id="delete-seconds",
                        type="integer",
                    )

                if self.can_notify:
                    yield Static("Before acting", classes="section")
                    yield Checkbox(
                        "DM each of them first, so they know why and can "
                        "appeal",
                        self.notify_default,
                        id="notify",
                    )
                    yield Static(
                        Text(
                            "One DM per member, paced by "
                            "moderation.bulk_delay. Messaging many people who "
                            "have not messaged you is what Discord's spam "
                            "heuristics look for, so this is slower and "
                            "riskier the larger the run.",
                            style="yellow",
                        )
                    )

                yield Static("", id="caution-warning")
                yield Checkbox(
                    "I have reviewed the CAUTION findings myself",
                    False,
                    id="caution-ack",
                )

                yield Static("Confirm", classes="section")
                yield Static("", id="confirm-hint")
                yield Input(placeholder="type the number here", id="confirm-count")

            with Horizontal(id="buttons"):
                yield Button("Cancel", variant="default", id="cancel")
                yield Button(verb, variant="error", id="confirm")

    def on_mount(self) -> None:
        # a disabled first option means the initial scope is not the one shown
        for key, _ in self.scopes:
            if key == "selected" and not self.has_selection:
                continue
            self.scope = key
            break
        self._refresh()

    # -- plan ---------------------------------------------------------------

    def _refresh(self) -> None:
        self.plan = self.resolve(self.scope)
        allowed = len(self.plan.allowed)

        summary = Text()
        summary.append(self.plan.describe() + "\n", style="bold")
        if self.plan.blocked:
            summary.append(
                "Blocked members are left alone, not acted on.\n", style="dim"
            )
        self.query_one("#plan-summary", Static).update(summary)

        preview = Text()
        for target in self.plan.allowed[: self.PREVIEW]:
            preview.append("  " + verdict_label(target.verdict) + "  ",
                           style=verdict_style(target.verdict))
            preview.append(target.label + "\n")
        if allowed > self.PREVIEW:
            preview.append(
                f"  ... and {allowed - self.PREVIEW:,} more\n", style="dim"
            )
        if self.plan.blocked:
            preview.append(
                f"\n  not acted on: "
                f"{', '.join(t.label for t in self.plan.blocked[:5])}"
                + (f" +{len(self.plan.blocked) - 5:,} more"
                   if len(self.plan.blocked) > 5 else "")
                + "\n",
                style="dim",
            )
        self.query_one("#plan-preview", Static).update(preview)

        cautious = [t for t in self.plan.allowed if t.eligibility.needs_double_confirm]
        warning = self.query_one("#caution-warning", Static)
        ack = self.query_one("#caution-ack", Checkbox)
        if cautious:
            text = Text()
            text.append(
                f"\n{len(cautious):,} of these are CAUTION findings\n",
                style="bold yellow",
            )
            text.append(
                "Rotector found something for them and deliberately did not "
                "conclude from it. Acting on those is your judgement, and it "
                "may be wrong about a real person.\n",
                style="yellow",
            )
            text.append(
                "  " + ", ".join(t.label for t in cautious[:6])
                + (f" +{len(cautious) - 6:,} more" if len(cautious) > 6 else ""),
                style="dim",
            )
            warning.update(text)
            warning.display = True
            ack.display = True
        else:
            warning.update("")
            warning.display = False
            ack.display = False
            ack.value = False

        hint = Text()
        if allowed:
            hint.append("Type ")
            hint.append(str(allowed), style="bold")
            hint.append(f" to confirm {self.action} on {allowed:,} ")
            hint.append("member" if allowed == 1 else "members")
        else:
            hint.append("Nothing to act on with this choice.", style="yellow")
        self.query_one("#confirm-hint", Static).update(hint)

    @on(RadioSet.Changed, "#scope")
    def _scope_changed(self, event: RadioSet.Changed) -> None:
        pressed = event.pressed.id or ""
        self.scope = pressed.removeprefix("scope-")
        self.query_one("#confirm-count", Input).value = ""
        self._refresh()

    # -- confirm ------------------------------------------------------------

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#confirm")
    def _confirm(self) -> None:
        allowed = len(self.plan.allowed) if self.plan else 0
        if not allowed:
            self.notify("Nothing matches this choice.", severity="warning")
            return

        cautious = [
            t for t in self.plan.allowed if t.eligibility.needs_double_confirm
        ]
        if cautious and not self.query_one("#caution-ack", Checkbox).value:
            self.notify(
                f"{len(cautious)} of these are CAUTION findings. Tick the "
                f"acknowledgement, or narrow the scope to THREAT only.",
                severity="error",
            )
            return

        typed = self.query_one("#confirm-count", Input).value.strip()
        if typed != str(allowed):
            self.notify(
                f"Type {allowed} to confirm. This is the last check before "
                f"{allowed:,} irreversible actions.",
                severity="error",
            )
            return

        custom = None
        if self.uses_reason and self.query_one("#reason-custom", RadioButton).value:
            custom = self.query_one("#reason-text", Input).value

        seconds = 0
        if self.wants_purge:
            try:
                seconds = int(self.query_one("#delete-seconds", Input).value or 0)
            except ValueError:
                seconds = 0

        self.dismiss(
            BulkChoice(
                scope=self.scope,
                custom=custom,
                delete_message_seconds=seconds,
                acknowledged_override=True,
                notify=(
                    self.can_notify
                    and self.query_one("#notify", Checkbox).value
                ),
            )
        )


class BulkPickDialog(DismissOnOutsideClick, ModalScreen[str | None]):
    """Which action a bulk run should perform, in this source's terms."""

    CSS = "BulkPickDialog { align: center middle; }" + _DIALOG_CSS
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, choices: list[tuple[str, str]]) -> None:
        super().__init__()
        self.choices = choices

    def compose(self) -> ComposeResult:
        with Vertical(id="panel"):
            yield Static("Bulk action", classes="title")
            yield Static(
                Text("Which action should be applied to many members?",
                     style="dim")
            )
            with RadioSet(id="action"):
                for index, (key, label) in enumerate(self.choices):
                    yield RadioButton(
                        label.capitalize(), value=index == 0, id=f"act-{key}"
                    )
            with Horizontal(id="buttons"):
                yield Button("Cancel", variant="default", id="cancel")
                yield Button("Continue", variant="primary", id="confirm")

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#confirm")
    def _confirm(self) -> None:
        for key, _ in self.choices:
            if self.query_one(f"#act-{key}", RadioButton).value:
                self.dismiss(key)
                return
        self.dismiss(None)


class UpdateDialog(DismissOnOutsideClick, ModalScreen[bool]):
    """Offer an update, and show what is in it before it is applied.

    Pulling means the next reload runs code that is not on this machine yet, so
    the commit subjects are listed rather than summarised as a count. Clicking
    away means "not now", which is the safe reading for running new code.

    The dialog is also the place the *un*available cases are explained -- no
    git, not a clone, a dirty tree -- because "check for updates" quietly doing
    nothing is the one outcome that leaves somebody wondering.
    """

    CSS = "UpdateDialog { align: center middle; }" + _DIALOG_CSS
    BINDINGS = [("escape", "cancel", "Close")]

    def _dismiss_from_outside(self) -> None:
        self.dismiss(False)

    def __init__(self, status) -> None:
        super().__init__()
        self.status = status

    def compose(self) -> ComposeResult:
        from ..update import MAX_LISTED, requirements_changed

        status = self.status
        with Vertical(id="panel"):
            yield Static("Update", classes="title")

            summary = Text()
            if not status.usable:
                summary.append("Updates are unavailable here.\n", style="bold yellow")
                summary.append(status.reason)
            elif not status.behind:
                summary.append("Already up to date.\n", style="bold green")
                summary.append(
                    f"{status.branch} matches {status.upstream}.", style="dim"
                )
            else:
                plural = "" if status.behind == 1 else "s"
                summary.append(f"{status.behind}", style="bold")
                summary.append(f" new commit{plural} on ")
                summary.append(f"{status.upstream}", style="bold")
                summary.append(".\n")
                summary.append(
                    "Updating fast-forwards this working copy. Nothing is "
                    "applied until you press Update.",
                    style="dim",
                )
            yield Static(summary)

            if status.commits:
                yield Static("Incoming", classes="section")
                listing = Text()
                for sha, subject in status.commits[:MAX_LISTED]:
                    listing.append(f"  {sha}  ", style="dim")
                    listing.append(f"{subject}\n")
                if len(status.commits) > MAX_LISTED:
                    listing.append(
                        f"  ... and {len(status.commits) - MAX_LISTED} more\n",
                        style="dim",
                    )
                yield Static(listing)

            if status.usable and status.dirty:
                warn = Text()
                warn.append("This working copy has local changes.\n",
                            style="bold yellow")
                warn.append(
                    "Updating is refused while tracked files are modified, "
                    "because a fast-forward cannot keep both your edits and "
                    "the new commits. Commit or stash them, then check again.",
                    style="dim",
                )
                yield Static(warn)

            if status.can_apply and requirements_changed(status):
                note = Text()
                note.append(
                    "Some of these look like dependency changes -- re-run "
                    "pip install -r requirements.txt afterwards.",
                    style="yellow",
                )
                yield Static(note)

            with Horizontal(id="buttons"):
                if status.can_apply:
                    yield Button("Not now", variant="default", id="cancel")
                    yield Button("Update", variant="primary", id="confirm")
                else:
                    yield Button("Close", variant="default", id="cancel")

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm")
    def _confirm(self) -> None:
        self.dismiss(True)


class JobsDialog(DismissOnOutsideClick, ModalScreen[str | None]):
    """The scan queue, managed from a popup rather than a pane.

    Same shape as the settings and export dialogs: it opens over the results,
    does one thing, and gets out of the way. A permanent pane would spend
    screen on a list that is empty most of the time, and the results table is
    what the app is *for*.

    Returns the action taken, so the caller can report it -- the dialog itself
    only ever changes queue state, never touches a worker. Anything that has to
    stop a running scan is the app's job, because the queue does not own the
    tasks.
    """

    CSS = "JobsDialog { align: center middle; }" + _DIALOG_CSS
    BINDINGS = [
        ("escape", "cancel", "Close"),
        ("p", "pause", "Pause / resume"),
        ("t", "prioritise", "Prioritise"),
        ("o", "only", "Give it the budget"),
        ("x", "cancel_job", "Stop"),
        ("d", "forget", "Remove"),
    ]

    def _dismiss_from_outside(self) -> None:
        self.dismiss(None)

    def __init__(self, queue, budget=None) -> None:
        super().__init__()
        self.queue = queue
        self.budget = budget
        self._acted: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="panel"):
            yield Static("Scan queue", classes="title")
            yield Static(Text(self.queue.summary(), style="dim"), id="queue-summary")
            with VerticalScroll(id="body"):
                yield DataTable(id="jobs", cursor_type="row", zebra_stripes=True)
            yield Static(self._legend(), id="queue-legend")
            with Horizontal(id="buttons"):
                yield Button("Close", variant="default", id="cancel")

    def _legend(self) -> Text:
        text = Text()
        for key, what in (
            ("p", "pause / resume"), ("t", "prioritise"),
            ("o", "give it the whole budget"), ("x", "stop"), ("d", "remove"),
        ):
            text.append(f" {key} ", style="reverse bold")
            text.append(f" {what}   ", style="dim")
        return text

    def on_mount(self) -> None:
        table = self.query_one("#jobs", DataTable)
        for name, width in (
            ("Job", 26), ("Backend", 10), ("State", 9),
            ("Found", 9), ("Elapsed", 9), ("What it is doing", 34),
        ):
            table.add_column(name, width=width)
        self._rebuild()
        table.focus()

    # -- painting ----------------------------------------------------------

    def _rebuild(self, keep_id: int | None = None) -> None:
        """Redraw, keeping the cursor on the same *job*.

        Order changes with state -- pausing a job moves it down the list -- so
        holding the row index would leave the cursor on whatever slid into that
        position, and the next keypress would act on a job nobody chose.
        """
        from ..eta import format_duration
        from ..jobs import STATE_LABEL, JobState

        table = self.query_one("#jobs", DataTable)
        if keep_id is None:
            current = self._selected_job()
            keep_id = current.id if current is not None else None
        table.clear()

        styles = {
            JobState.RUNNING: "bold cyan", JobState.PAUSED: "yellow",
            JobState.PENDING: "", JobState.DONE: "green",
            JobState.FAILED: "bold red", JobState.CANCELLED: "dim",
        }
        for job in self.queue.order():
            found = f"{len(job.rows):,}"
            if job.expected:
                found += f" / {job.expected:,}"
            table.add_row(
                Text(job.source_name, overflow="ellipsis"),
                Text(job.backend),
                Text(STATE_LABEL[job.state], style=styles.get(job.state, "")),
                Text(found, justify="right"),
                Text(format_duration(job.elapsed) if job.elapsed else "-",
                     justify="right"),
                Text(job.error or job.note or "", overflow="ellipsis",
                     style="red" if job.error else "dim"),
                key=str(job.id),
            )

        summary = self.queue.summary()
        if self.budget is not None:
            sharing = self.budget.sharing(self.queue.jobs)
            splits = [f"{n} on {name}" for name, n in sorted(sharing.items()) if n > 1]
            if splits:
                # worth saying out loud: two scans on one backend is not a
                # fault, but it is why both of them look half as fast
                summary += f"  -  sharing one budget: {', '.join(splits)}"
        self.query_one("#queue-summary", Static).update(Text(summary, style="dim"))

        if keep_id is not None:
            for index, job in enumerate(self.queue.order()):
                if job.id == keep_id:
                    table.move_cursor(row=index)
                    return
        if table.row_count:
            table.move_cursor(row=0)

    def _selected_job(self):
        table = self.query_one("#jobs", DataTable)
        if not table.row_count:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:  # noqa: BLE001 - nothing under the cursor
            return None
        return self.queue.get(int(key.value)) if key.value else None

    def _after(self, job, message: str, action: str) -> None:
        self._acted = action
        self._rebuild(keep_id=job.id)
        self.notify(message)

    # -- actions -----------------------------------------------------------

    def action_pause(self) -> None:
        from ..jobs import JobState

        job = self._selected_job()
        if job is None:
            return
        if job.state is JobState.RUNNING and self.queue.pause(job.id):
            self._after(job, f"Paused {job.source_name}. It keeps its results.", "pause")
        elif job.state is JobState.PAUSED and self.queue.resume(job.id):
            self._after(job, f"{job.source_name} is queued to carry on.", "resume")
        else:
            self.notify("Only a running or paused scan can be toggled.",
                        severity="warning")

    def action_prioritise(self) -> None:
        job = self._selected_job()
        if job is None:
            return
        if self.queue.prioritise(job.id):
            self._after(job, f"{job.source_name} goes next. Nothing running was "
                        f"stopped.", "prioritise")

    def action_only(self) -> None:
        """Give one job the whole budget by pausing the others."""
        job = self._selected_job()
        if job is None:
            return
        paused = self.queue.pause_others(job.id)
        if not paused:
            self.notify("Nothing else was running.", severity="warning")
            return
        names = ", ".join(j.source_name for j in paused)
        self._after(job, f"Paused {names}. {job.source_name} has the budget.", "only")

    def action_cancel_job(self) -> None:
        job = self._selected_job()
        if job is None:
            return
        if self.queue.cancel(job.id):
            self._after(
                job,
                f"Stopped {job.source_name}. Its {len(job.rows):,} result(s) "
                f"are kept.", "cancel",
            )

    def action_forget(self) -> None:
        job = self._selected_job()
        if job is None:
            return
        name = job.source_name
        if self.queue.remove(job.id) is not None:
            self._acted = "remove"
            self._rebuild()          # the job is gone; the cursor cannot follow it
            self.notify(f"Removed {name} from the queue.")
        else:
            self.notify("Stop it first: a running scan cannot be removed.",
                        severity="warning")

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(self._acted)
