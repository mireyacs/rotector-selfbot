"""Modal dialogs: export options, and the kick/ban flow."""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
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
    #buttons { height: auto; padding-top: 1; align-horizontal: right; }
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


class ExportDialog(ModalScreen[ExportChoice | None]):
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
    ) -> None:
        super().__init__()
        self._preserve = preserve
        self._formats = [f.lower() for f in formats] or ["csv"]
        self._scope = scope if scope in ("filtered", "all") else "filtered"
        self._segment_size = segment_size
        self._columns = columns or list(DEFAULT_COLUMNS)
        self.filter_name = filter_name
        self.filtered_count = filtered_count
        self.total_count = total_count

    def compose(self) -> ComposeResult:
        with Vertical(id="panel"):
            yield Static("Export results", classes="title")

            yield Static("Formats", classes="section")
            yield SelectionList[str](
                ("CSV  - segmented spreadsheet", "csv", "csv" in self._formats),
                ("TXT  - readable report", "txt", "txt" in self._formats),
                ("JSON - full structured data", "json", "json" in self._formats),
                id="formats",
            )

            yield Static("What to export", classes="section")
            with RadioSet(id="scope"):
                yield RadioButton(
                    f"Current filter: {self.filter_name} "
                    f"({self.filtered_count:,} members)",
                    value=self._scope == "filtered",
                    id="scope-filtered",
                )
                yield RadioButton(
                    f"Everything scanned ({self.total_count:,} members)",
                    value=self._scope == "all",
                    id="scope-all",
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

            with Horizontal(id="buttons"):
                yield Button("Cancel", variant="default", id="cancel")
                yield Button("Export", variant="primary", id="confirm")

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

        scope = (
            "all"
            if self.query_one("#scope-all", RadioButton).value
            else "filtered"
        )
        self.dismiss(
            ExportChoice(
                formats=formats,
                scope=scope,
                segment_size=segment,
                columns=columns,
                preserve=self.query_one("#preserve", Checkbox).value,
                remember=self.query_one("#remember", Checkbox).value,
            )
        )


@dataclass
class ModerationChoice:
    action: str                      # "kick" | "ban"
    reason: str
    delete_message_seconds: int = 0
    acknowledged_override: bool = False


class ModerationDialog(ModalScreen[ModerationChoice | None]):
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
    ) -> None:
        super().__init__()
        self.action = action
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

            if self.eligibility.needs_override:
                yield Checkbox(
                    "I understand Rotector does not support this action",
                    False,
                    id="override",
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
        return build_reason(self.report, self.template, custom)

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

        seconds = 0
        if self.wants_purge:
            try:
                seconds = int(self.query_one("#delete-seconds", Input).value or 0)
            except ValueError:
                seconds = 0

        self.dismiss(
            ModerationChoice(
                action=self.action,
                reason=self._current_reason(),
                delete_message_seconds=seconds,
                acknowledged_override=acknowledged,
            )
        )


@dataclass
class LeaveChoice:
    silent: bool
    remember: bool = False


class LeaveGroupDialog(ModalScreen[LeaveChoice | None]):
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


class PurgePlanDialog(ModalScreen[PurgeChoice | None]):
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


class PurgeConfirmDialog(ModalScreen[bool]):
    """Show exactly what will be deleted, and require a typed confirmation."""

    CSS = "PurgeConfirmDialog { align: center middle; }" + _DIALOG_CSS
    BINDINGS = [("escape", "cancel", "Cancel")]

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
