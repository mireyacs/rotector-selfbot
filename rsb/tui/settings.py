"""Editing config.toml from the UI, plus first-run setup and diagnostics.

Every field is generated from :data:`rsb.migrate.SCHEMA` -- the same list the
migrator writes from -- so the editor, the file format and the upgrade path
cannot drift apart. Adding a setting to the schema makes it appear here with no
further work.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Static, TabbedContent, TabPane

from ..config import Config, write_section
from .dialogs import DismissOnOutsideClick
from ..migrate import SCHEMA, Section, Setting

_CSS = """
    #panel {
        width: 92;
        height: 90%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    #panel > .title { text-style: bold; padding-bottom: 1; }
    .hint { color: $text-muted; padding-bottom: 1; }
    .field-label { text-style: bold; padding-top: 1; }
    .field-help { color: $text-muted; }
    #buttons { height: auto; padding-top: 1; align-horizontal: right; }
    Button { margin-left: 1; }
"""


def _widget_id(section: str, setting: str) -> str:
    return f"cfg-{section}-{setting}".replace("_", "-")


def _as_text(value) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return "" if value is None else str(value)


def _coerce(raw: str, default):
    """Parse a field back to the type its default implies."""
    raw = raw.strip()
    if isinstance(default, bool):
        return raw.lower() in ("true", "1", "yes", "on")
    if isinstance(default, list):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(raw)
        except ValueError:
            return default
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError:
            return default
    return raw


def current_value(config: Config, section: str, setting: str, default):
    """Read a live value off the Config, falling back to the schema default."""
    if section == "discord" and setting == "token":
        return config.token or ""
    holder = getattr(config, section, None)
    if holder is None:
        return default
    return getattr(holder, setting, default)


def apply_value(config: Config, section: str, setting: str, value) -> None:
    if section == "discord" and setting == "token":
        config.token = value or None
        return
    holder = getattr(config, section, None)
    if holder is not None and hasattr(holder, setting):
        setattr(holder, setting, value)


class SettingsScreen(DismissOnOutsideClick, ModalScreen[bool]):
    """Edit every setting, then write them back section by section."""

    CSS = "SettingsScreen { align: center middle; }" + _CSS
    BINDINGS = [("escape", "cancel", "Close")]

    def _dismiss_from_outside(self) -> None:
        self.dismiss(False)

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        with Vertical(id="panel"):
            yield Static("Settings", classes="title")
            yield Static(
                Text(
                    f"Written to {self.config.config_path()} - only the sections "
                    f"you change are rewritten, comments elsewhere are kept.",
                    style="dim",
                ),
                classes="hint",
            )
            with TabbedContent():
                for section in SCHEMA:
                    with TabPane(section.name.title(), id=f"tab-{section.name}"):
                        with VerticalScroll():
                            yield from self._fields(section)
            with Horizontal(id="buttons"):
                yield Button("Cancel", variant="default", id="cancel")
                yield Button("Save", variant="primary", id="save")

    def _fields(self, section: Section):
        if section.comment:
            yield Static(Text(section.comment, style="italic dim"), classes="hint")
        for setting in section.settings:
            value = current_value(
                self.config, section.name, setting.name, setting.default
            )
            widget_id = _widget_id(section.name, setting.name)
            yield Static(setting.name.replace("_", " "), classes="field-label")
            if setting.comment:
                yield Static(Text(setting.comment, style="dim"), classes="field-help")
            if isinstance(setting.default, bool):
                yield Checkbox("enabled", bool(value), id=widget_id)
            else:
                secret = setting.name in ("token", "api_key")
                yield Input(
                    value=_as_text(value),
                    id=widget_id,
                    password=secret and bool(value),
                    placeholder=_as_text(setting.default),
                )

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        changed: list[str] = []
        for section in SCHEMA:
            body = {}
            for setting in section.settings:
                widget_id = _widget_id(section.name, setting.name)
                try:
                    widget = self.query_one(f"#{widget_id}")
                except Exception:
                    continue
                raw = (
                    widget.value
                    if isinstance(widget, Checkbox)
                    else _coerce(widget.value, setting.default)
                )
                before = current_value(
                    self.config, section.name, setting.name, setting.default
                )
                if raw != before:
                    changed.append(f"{section.name}.{setting.name}")
                apply_value(self.config, section.name, setting.name, raw)
                body[setting.name] = raw
            try:
                write_section(self.config.config_path(), section.name, body)
            except OSError as exc:
                self.notify(f"Could not write config: {exc}", severity="error")
                return

        self.config.source = self.config.config_path()
        self.notify(
            f"Saved {len(changed)} change(s)."
            if changed
            else "Saved (nothing changed).",
        )
        self.dismiss(bool(changed))


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    #: whether failing this makes starting up pointless.
    #: Heuristics (does the token *look* right?) are advisory: a token that
    #: looks odd may work perfectly, and Discord's answer is the authority.
    #: Only things that certainly cannot work should stop the app.
    blocking: bool = False


class DiagnosticsScreen(DismissOnOutsideClick, ModalScreen[str | None]):
    """What is wrong and what to do about it.

    Shown when startup fails, so the answer is on screen instead of in a log
    line that has already scrolled past.
    """

    CSS = "DiagnosticsScreen { align: center middle; }" + _CSS
    BINDINGS = [("escape", "cancel", "Close")]

    def __init__(self, checks: list[Check], headline: str = "") -> None:
        super().__init__()
        self.checks = checks
        self.headline = headline

    def compose(self) -> ComposeResult:
        with Vertical(id="panel"):
            yield Static("Something is not set up correctly", classes="title")
            if self.headline:
                yield Static(Text(self.headline, style="bold red"), classes="hint")
            with VerticalScroll():
                for check in self.checks:
                    line = Text()
                    line.append("  OK  " if check.ok else " FAIL ",
                                style="reverse green" if check.ok else "reverse red")
                    line.append(f"  {check.name}\n", style="bold")
                    line.append(f"        {check.detail}\n")
                    if not check.ok and check.fix:
                        line.append(f"        -> {check.fix}\n", style="yellow")
                    yield Static(line)
            with Horizontal(id="buttons"):
                yield Button("Close", variant="default", id="cancel")
                yield Button("Open settings", variant="primary", id="settings")
                yield Button("Retry", variant="success", id="retry")

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#settings")
    def _settings(self) -> None:
        self.dismiss("settings")

    @on(Button.Pressed, "#retry")
    def _retry(self) -> None:
        self.dismiss("retry")


class SetupWizard(DismissOnOutsideClick, ModalScreen[bool]):
    """First run: collect the minimum needed to get going."""

    CSS = "SetupWizard { align: center middle; }" + _CSS
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        with Vertical(id="panel"):
            yield Static("Welcome - let's get you set up", classes="title")

            intro = Text()
            intro.append(
                "This checks the people around you against Rotector, a database "
                "of Roblox accounts with detected violations.\n\n"
            )
            intro.append("Two things worth knowing first.\n", style="bold")
            intro.append(
                "  1. Automating a user account is against Discord's Terms of "
                "Service. This only ever reads, but the risk of losing the "
                "account is real and yours.\n"
                "  2. A verdict is not a background check. 'No detections' means "
                "nothing has been found yet, not that someone is safe.\n",
                style="yellow",
            )
            yield Static(intro, classes="hint")

            yield Static("Discord token", classes="field-label")
            yield Static(
                Text(
                    "Required. Stored in config.toml, which is gitignored. "
                    "Treat it like a password.",
                    style="dim",
                ),
                classes="field-help",
            )
            yield Input(password=True, id="wiz-token", placeholder="paste your token")

            yield Static("Rotector API key", classes="field-label")
            yield Static(
                Text(
                    "Optional. Without one you get the standard rate limit, "
                    "which is fine for normal use. panel.rotector.com issues "
                    "keys with higher limits.",
                    style="dim",
                ),
                classes="field-help",
            )
            yield Input(id="wiz-key", placeholder="leave blank if you have none")

            yield Checkbox(
                "Hide members with no findings (recommended)", True, id="wiz-hide"
            )

            with Horizontal(id="buttons"):
                yield Button("Quit", variant="default", id="cancel")
                yield Button("Save and continue", variant="primary", id="save")

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        token = self.query_one("#wiz-token", Input).value.strip()
        if not token:
            self.notify("A Discord token is required.", severity="error")
            return
        key = self.query_one("#wiz-key", Input).value.strip()
        hide = self.query_one("#wiz-hide", Checkbox).value

        self.config.token = token
        self.config.rotector.api_key = key or None
        self.config.scan.hide_no_detections = hide
        self.config.scan.hide_unknown = hide

        path = self.config.config_path()
        try:
            write_section(path, "discord", {"token": token})
            write_section(
                path,
                "rotector",
                {
                    "api_key": key,
                    "rate_limit": self.config.rotector.rate_limit,
                    "window": self.config.rotector.window,
                    "reserve": self.config.rotector.reserve,
                    "cache_ttl": self.config.rotector.cache_ttl,
                    "concurrency": self.config.rotector.concurrency,
                },
            )
            write_section(
                path,
                "scan",
                {
                    "skip_bots": self.config.scan.skip_bots,
                    "max_members": self.config.scan.max_members,
                    "hide_no_detections": hide,
                    "hide_unknown": hide,
                },
            )
        except OSError as exc:
            self.notify(f"Could not write {path}: {exc}", severity="error")
            return

        self.config.source = path
        self.dismiss(True)


def blocking_problems(config: Config) -> list[Check]:
    """Only the failures that make starting up pointless."""
    return [c for c in run_checks(config) if not c.ok and c.blocking]


def advisory_problems(config: Config) -> list[Check]:
    """Failures worth mentioning that need not stop anything."""
    return [c for c in run_checks(config) if not c.ok and not c.blocking]


def run_checks(config: Config) -> list[Check]:
    """Static configuration checks, before anything touches the network."""
    checks: list[Check] = []

    token = (config.token or "").strip()
    checks.append(
        Check(
            "Discord token",
            bool(token),
            f"{len(token)} characters" if token else "not set",
            "Add it in Settings, or set DISCORD_TOKEN.",
            blocking=True,
        )
    )
    if token:
        checks.append(
            Check(
                "Token shape",
                token.count(".") >= 2,
                "looks like a user token"
                if token.count(".") >= 2
                else "does not look like a Discord token",
                "User tokens have two dots. A bot token will not work here.",
            )
        )

    rot = config.rotector
    checks.append(
        Check(
            "Rate limit",
            0 < rot.reserve < rot.rate_limit and rot.window > 0,
            f"{rot.rate_limit} per {rot.window}s, reserve {rot.reserve}",
            "reserve must be below rate_limit, and window above zero.",
        )
    )

    proxies = config.proxy_urls()
    if config.proxy.enabled:
        checks.append(
            Check(
                "Proxies",
                bool(proxies),
                f"{len(proxies)} configured"
                if proxies
                else "enabled, but the list is empty",
                "Add proxies, or set proxy.enabled = false.",
            )
        )

    path = config.config_path()
    writable = True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            writable = path.stat().st_mode & 0o200 != 0
    except OSError:
        writable = False
    checks.append(
        Check(
            "Config file",
            writable,
            str(path) if writable else f"{path} is not writable",
            "Settings changes cannot be saved until this is fixed.",
        )
    )
    return checks


class ErrorScreen(DismissOnOutsideClick, ModalScreen[str | None]):
    """What went wrong, and what can be done about it without restarting.

    A traceback in a log the user cannot see is not error handling. This shows
    it, and offers the three things actually worth doing: try again, reload the
    code after editing it, or carry on with what is already on screen.
    """

    CSS = "ErrorScreen { align: center middle; }" + _CSS
    BINDINGS = [("escape", "cancel", "Dismiss")]

    def __init__(
        self,
        summary: str,
        detail: str,
        *,
        context: str = "",
        can_retry: bool = True,
        preserved: str = "",
    ) -> None:
        super().__init__()
        self.summary = summary
        self.detail = detail
        self.context = context
        self.can_retry = can_retry
        self.preserved = preserved

    def compose(self) -> ComposeResult:
        with Vertical(id="panel"):
            yield Static("Something went wrong", classes="title")

            head = Text()
            if self.context:
                head.append(f"{self.context}\n", style="dim")
            head.append(self.summary, style="bold red")
            yield Static(head)

            if self.preserved:
                note = Text()
                note.append("\nNothing was lost. ", style="bold green")
                note.append(self.preserved)
                yield Static(note)

            yield Static("Detail", classes="section")
            with VerticalScroll():
                yield Static(Text(self.detail, style="dim"))

            yield Static(
                Text(
                    "\nEdit the code and press Reload to pick the change up "
                    "without restarting. Scan results stay as they are.",
                    style="dim",
                ),
                classes="hint",
            )

            with Horizontal(id="buttons"):
                yield Button("Dismiss", variant="default", id="cancel")
                yield Button("Reload code", variant="warning", id="reload")
                if self.can_retry:
                    yield Button("Try again", variant="primary", id="retry")

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#reload")
    def _reload(self) -> None:
        self.dismiss("reload")

    @on(Button.Pressed, "#retry")
    def _retry(self) -> None:
        self.dismiss("retry")
