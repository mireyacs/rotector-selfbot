"""The full-screen player vibe mode replaces the interface with.

A scan against Okappiki takes half an hour. This is what the terminal shows for
it: the track, where you are in it, and a barcode field driven by the actual
audio -- the envelope measured by ``tools/peaks.py`` and shipped beside the
music, not a timer pretending to be a visualiser.

The field is the project page's own device. Hairline bars at varying
brightness, scrolling right to left so the newest moment is under the cursor,
with the loud parts of the track standing out of it the way findings stand out
of the hero canvas. Same grammar, different data.

Escape or ``v`` puts the scanner back. Nothing here interrupts a scan: the
screen is a view, and the run carries on behind it.
"""

from __future__ import annotations

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Static

from ..eta import format_duration


class Barcode(Widget):
    """The visualiser: one column a bar, brightness from the envelope.

    Rendered as strips rather than as text because a bar is a *column* -- every
    row of the widget draws the same column at the same brightness, which is
    what makes it read as a bar-field rather than as a chart.
    """

    DEFAULT_CSS = "Barcode { height: 1fr; }"

    #: seconds of audio the field spans left to right
    SPAN = 8.0

    #: The page's own divider, in terminal cells: a 12-column period with bars
    #: at 0, 2, 5-6, 8 and 11. Which columns are bars is fixed; how bright they
    #: are is the audio. That is what makes it a barcode being lit rather than
    #: a chart drawn in blocks -- the same distinction the hero canvas makes.
    PERIOD = 12
    OFFSETS = frozenset({0, 2, 5, 6, 8, 11})

    def __init__(self) -> None:
        super().__init__()
        self.levels: list[float] = []

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        if not width:
            return Strip.blank(0)
        levels = self.levels

        segments = []
        run: list[str] = []
        run_style: Style | None = None
        for column in range(width):
            if column % self.PERIOD not in self.OFFSETS:
                shade = 0
            else:
                level = levels[column] if column < len(levels) else 0.0
                # a floor, so a quiet passage still reads as a wall rather than
                # blinking out of existence
                shade = int(26 + min(1.0, level) * 229)
            style = Style(color=f"rgb({shade},{shade},{shade})") if shade else None
            char = "\u2588" if shade else " "
            if style == run_style:
                run.append(char)
            else:
                if run:
                    segments.append(Segment("".join(run), run_style))
                run, run_style = [char], style
        if run:
            segments.append(Segment("".join(run), run_style))
        return Strip(segments, width)

    def update(self, levels: list[float]) -> None:
        self.levels = levels
        self.refresh()


class VibeScreen(Screen):
    """Everything the player needs and nothing else."""

    CSS = """
    VibeScreen { background: $background; }
    #vibe-wrap { height: 1fr; padding: 1 3; }
    #vibe-title { text-style: bold; padding-top: 1; }
    #vibe-meta { color: $text-muted; padding-bottom: 1; }
    #vibe-seek { padding-top: 1; }
    #vibe-keys { color: $text-muted; padding-top: 1; }
    """

    BINDINGS = [
        Binding("escape,v", "leave", "Back to the scanner"),
        Binding("right,l", "forward", "Forward 10s"),
        Binding("left,h", "back", "Back 10s"),
        Binding("n,>", "next", "Next track"),
        Binding("p,<", "previous", "Previous"),
        Binding("q", "leave", "Back", show=False),
    ]

    #: how many columns of history the bar field holds
    BARS_FROM_WIDTH = True

    def __init__(self, vibe) -> None:
        super().__init__()
        self.vibe = vibe

    def compose(self) -> ComposeResult:
        with Vertical(id="vibe-wrap"):
            yield Barcode()
            yield Static("", id="vibe-title")
            yield Static("", id="vibe-meta")
            yield Static("", id="vibe-seek")
            yield Static(self._keys(), id="vibe-keys")

    def _keys(self) -> Text:
        text = Text()
        for key, what in (
            ("<-/->", "seek 10s"), ("p / n", "previous / next"),
            ("esc", "back to the scanner"),
        ):
            text.append(f" {key} ", style="reverse bold")
            text.append(f" {what}    ", style="dim")
        return text

    def on_mount(self) -> None:
        # 20 fps is what the envelope was measured at; matching it means the
        # field moves exactly as fast as the audio it came from
        self.set_interval(1 / 20, self._tick)
        self._tick()

    def _tick(self) -> None:
        vibe = self.vibe
        field = self.query_one(Barcode)
        width = max(1, field.size.width)
        field.update(vibe.bars(width, Barcode.SPAN))

        track = vibe.current
        title = self.query_one("#vibe-title", Static)
        meta = self.query_one("#vibe-meta", Static)
        if track is None:
            title.update(Text("Nothing playing", style="dim"))
            meta.update("")
        else:
            title.update(Text(track.title))
            bits = [b for b in (track.artist, track.license) if b]
            meta.update(Text("  ·  ".join(bits), style="dim"))

        self.query_one("#vibe-seek", Static).update(self._seek_bar())

    def _seek_bar(self) -> Text:
        vibe = self.vibe
        position, duration = vibe.position, vibe.duration
        text = Text()
        text.append(format_duration(position).rjust(6), style="bold")
        width = max(10, self.size.width - 26)
        filled = int(width * (position / duration)) if duration else 0
        text.append("  ")
        # the same barcode material as the field above, at one row
        for index in range(width):
            if index < filled:
                text.append("█")
            elif index == filled:
                text.append("█", style="bold")
            else:
                text.append("─", style="dim")
        text.append("  ")
        text.append(format_duration(duration) if duration else "--:--", style="dim")
        return text

    # -- actions -----------------------------------------------------------

    def action_leave(self) -> None:
        self.dismiss(None)

    def action_forward(self) -> None:
        self.app.run_worker(self.vibe.nudge(10), group="vibe-seek", exclusive=True)

    def action_back(self) -> None:
        self.app.run_worker(self.vibe.nudge(-10), group="vibe-seek", exclusive=True)

    def action_next(self) -> None:
        self.app.run_worker(self.vibe.skip(1), group="vibe-seek", exclusive=True)

    def action_previous(self) -> None:
        self.app.run_worker(self.vibe.previous(), group="vibe-seek", exclusive=True)
