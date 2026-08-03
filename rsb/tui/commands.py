"""Command palette and a keybind bar that survives a narrow window.

Every action is reachable two ways: the footer, and `ctrl+p`. The footer is the
first thing a small terminal loses -- it silently truncates -- so the palette
carries the same commands, sourced from the very same binding list rather than
a second one that would drift out of step.
"""

from __future__ import annotations

from rich.style import Style
from rich.text import Text
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Horizontal, HorizontalScroll
from textual.widgets import Static


def _themed(app, variable: str, fallback: str) -> str:
    """A Rich style string pulled from whichever theme is active.

    The keybar is drawn as Rich text rather than as styled widgets, so it
    cannot pick colours up from CSS and has to read them itself. Not every
    theme variable is a colour Rich can parse -- the ansi themes answer with
    ``ansi_magenta`` and ``transparent``, and several are ``auto 60%`` -- so
    anything Rich refuses falls back rather than raising in a render.
    """
    value = app.theme_variables.get(variable, "")
    try:
        Style.parse(value)
    except Exception:
        return fallback
    return value


class BindingCommands(Provider):
    """Exposes the app's own key bindings as palette commands.

    Generated from ``App.BINDINGS`` so the palette and the footer can never
    disagree about what exists or what it is called.
    """

    def _commands(self) -> list[tuple[str, str, str]]:
        app = self.app
        out: list[tuple[str, str, str]] = []
        for binding in app.BINDINGS:
            keys, action, description = _unpack(binding)
            if not description:
                continue  # hidden bindings stay hidden
            out.append((description, action, keys))
        return out

    async def discover(self) -> Hits:
        for description, action, keys in self._commands():
            yield DiscoveryHit(
                display=description,
                command=self._runner(action),
                text=description,
                help=f"key: {keys}",
            )

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for description, action, keys in self._commands():
            haystack = f"{description} {keys}"
            score = matcher.match(haystack)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(description),
                    self._runner(action),
                    help=f"key: {keys}",
                )

    def _runner(self, action: str):
        app = self.app

        def run() -> None:
            app.call_later(app.run_action, action)

        return run


def _unpack(binding) -> tuple[str, str, str]:
    """Bindings may be tuples or Binding objects, depending on how declared."""
    if isinstance(binding, tuple):
        keys, action = binding[0], binding[1]
        description = binding[2] if len(binding) > 2 else ""
    else:
        keys = getattr(binding, "key", "")
        action = getattr(binding, "action", "")
        description = getattr(binding, "description", "")
    return str(keys), str(action), str(description or "")


class StripContent(Static):
    """A one-line Text sized to its own content, so it can be scrolled over."""

    def __init__(self, widget_id: str) -> None:
        super().__init__(id=widget_id, markup=False)

    def set_text(self, text: Text) -> None:
        self.update(text)
        self.styles.width = max(1, text.cell_len)


class ScrollArrow(Static):
    """A clickable arrow at one end of a strip."""

    def __init__(self, direction: int, widget_id: str) -> None:
        super().__init__(
            "\u25c0" if direction < 0 else "\u25b6", id=widget_id, markup=False
        )
        self.direction = direction

    def on_click(self, event) -> None:
        strip = self.parent
        if isinstance(strip, ScrollingStrip):
            strip.nudge(self.direction)
        event.stop()


class ScrollingStrip(Horizontal):
    """One line of text that scrolls sideways between two arrows.

    Used for anything that must stay on a single row but may not fit: the key
    bar and the status line. Clipping is the failure mode being avoided --
    truncated text gives no sign that anything is missing, whereas a lit arrow
    does.
    """

    DEFAULT_CSS = """
    ScrollingStrip { height: 1; width: 100%; }
    ScrollingStrip > .strip-arrow {
        width: 3;
        height: 1;
        content-align: center middle;
        background: $panel-lighten-1;
        color: $text;
    }
    ScrollingStrip > .strip-arrow.exhausted { color: $panel-lighten-2; }
    ScrollingStrip > .strip-arrow.hidden { display: none; }
    ScrollingStrip > .strip-arrow:hover { background: $primary; }
    ScrollingStrip > .strip-view {
        width: 1fr;
        height: 1;
        /* scrolling must be on for the arrows to move anything, but the
           scrollbar is sized away -- on a one-row strip it eats the row */
        overflow-x: auto;
        overflow-y: hidden;
        scrollbar-size-horizontal: 0;
    }
    """

    #: cells moved per arrow press
    STEP = 24
    #: hide the arrows entirely while everything fits
    AUTO_HIDE = True

    def __init__(self, prefix: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.prefix = prefix
        # Where we intend to be. Textual applies scroll_to on a later frame
        # even with animate=False, so reading the widget back during a burst
        # of clicks would make each one start from the same place.
        self._want_x = 0

    def compose(self):
        yield ScrollArrow(-1, f"{self.prefix}-left").add_class("strip-arrow")
        with HorizontalScroll(id=f"{self.prefix}-view", classes="strip-view"):
            yield StripContent(f"{self.prefix}-content")
        yield ScrollArrow(1, f"{self.prefix}-right").add_class("strip-arrow")

    @property
    def view(self) -> HorizontalScroll:
        return self.query_one(f"#{self.prefix}-view", HorizontalScroll)

    @property
    def content(self) -> StripContent:
        return self.query_one(f"#{self.prefix}-content", StripContent)

    @property
    def scrolled_to(self) -> int:
        return self._want_x

    def set_text(self, text: Text) -> None:
        self.content.set_text(text)
        self._sync_arrows()

    def nudge(self, direction: int) -> None:
        view = self.view
        limit = int(view.max_scroll_x)
        self._want_x = max(0, min(limit, self._want_x + direction * self.STEP))
        view.scroll_to(x=self._want_x, animate=False)
        self._sync_arrows()

    def _sync_arrows(self) -> None:
        try:
            view = self.view
            limit = int(view.max_scroll_x)
            self._want_x = max(0, min(limit, self._want_x))
            left = self.query_one(f"#{self.prefix}-left")
            right = self.query_one(f"#{self.prefix}-right")
            if self.AUTO_HIDE and limit <= 0:
                left.add_class("hidden")
                right.add_class("hidden")
                return
            left.remove_class("hidden")
            right.remove_class("hidden")
            left.set_class(self._want_x <= 0, "exhausted")
            right.set_class(self._want_x >= limit, "exhausted")
        except Exception:
            pass

    def on_resize(self, event) -> None:
        # how much overflow there is changes with the window
        self._sync_arrows()

    def on_show(self, event) -> None:
        self.set_timer(0.05, self._sync_arrows)

    def on_mouse_scroll_down(self, event) -> None:
        # a wheel over a one-line strip means sideways, not down
        self.nudge(1)
        event.stop()

    def on_mouse_scroll_up(self, event) -> None:
        self.nudge(-1)
        event.stop()


class ScrollableFooter(ScrollingStrip):
    """The keybind bar. Always shows its arrows, as a hint that it scrolls."""

    DEFAULT_CSS = """
    ScrollableFooter { background: $panel; }
    ScrollableFooter > .strip-view { background: $panel; }
    """
    AUTO_HIDE = False

    def __init__(self) -> None:
        super().__init__(prefix="keys")

    def on_mount(self) -> None:
        self.refresh_keys()
        # the bar is Rich text, so a theme swap has to redraw it by hand; CSS
        # would have repainted itself
        self.app.theme_changed_signal.subscribe(self, lambda _: self.refresh_keys())
        # the arrows depend on measured overflow, which is not known until the
        # layout has settled
        self.set_timer(0.2, self._sync_arrows)

    def refresh_keys(self) -> None:
        app = self.app
        separator = _themed(app, "panel-lighten-3", "dim")
        key = _themed(app, "footer-key-foreground", "")
        description = _themed(app, "foreground", "")

        text = Text(no_wrap=True, overflow="visible", end="")
        for index, (keys, _action, description_text) in enumerate(_described(app)):
            if index:
                text.append(" | ", style=separator)
            text.append(f" {_pretty(keys)} ", style=f"bold {key}".strip())
            text.append(description_text, style=description)
        self.set_text(text)


class StatusStrip(ScrollingStrip):
    """The status line. Arrows appear only when the text does not fit."""

    DEFAULT_CSS = """
    StatusStrip { background: $panel; color: $text-muted; }
    StatusStrip > .strip-view { background: $panel; }
    """

    def __init__(self) -> None:
        super().__init__(prefix="status")


def _described(app) -> list[tuple[str, str, str]]:
    out = []
    for binding in app.BINDINGS:
        keys, action, description = _unpack(binding)
        if description:
            out.append((keys, action, description))
    return out


def _pretty(keys: str) -> str:
    """`s,enter` -> `s`, `ctrl+p` -> `^p`, matching the footer's own style."""
    first = keys.split(",")[0]
    return first.replace("ctrl+", "^").replace("slash", "/")


def keybind_summary(app, width: int) -> Text:
    """One-line hint used when the footer cannot show everything."""
    total = sum(1 for b in app.BINDINGS if _unpack(b)[2])
    return Text(
        f"{total} commands - ctrl+p for all", style="dim", no_wrap=True
    )
