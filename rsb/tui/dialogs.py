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
    ) -> None:
        super().__init__()
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
    ) -> None:
        super().__init__()
        self.action = action
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

            yield Static("Reason", classes="section")
            with RadioSet(id="reason-mode"):
                yield RadioButton("Automatic", value=True, id="reason-auto")
                yield RadioButton("Custom", id="reason-custom")
            yield Input(placeholder="Your own reason...", id="reason-text")
            yield Static("", id="reason-preview")

            if self.action == "ban":
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
        if self.action == "ban":
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
