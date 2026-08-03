"""The full-screen player vibe mode replaces the interface with.

A scan against Okappiki takes half an hour. This is what the terminal shows for
it: the track, where you are in it, how the scan behind it is doing, and the
project page's own field -- bars lit by the audio, one continuous sine trace
across them, and a mark for every member the scan has flagged.

The field is the page's device rather than a chart. Which columns are bars
never changes; how bright they are is the amplitude envelope measured by
``tools/peaks.py`` and shipped beside the music. The findings are the same
marks the hero canvas draws: a hairline the height of the field with a square
somewhere along it, one per hit, laid out across the scan's progress.

Leaving does not stop the music. ``v`` from the scanner comes straight back to
whatever is still playing, which is the whole point of putting it behind a
screen instead of a mode.
"""

from __future__ import annotations

import math
import random

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Static

from ..backend import backend_label
from ..eta import format_duration


def _ink(app) -> tuple[str, str]:
    """(foreground, background) as real colours, never 'transparent'.

    Every segment this screen draws names its own background. Without one the
    cell falls through to the terminal's default, which in a profile with a
    transparent background means the desktop shows through the gaps in the
    barcode -- the field stops being a field and becomes a stencil.
    """
    variables = getattr(app, "theme_variables", {}) or {}
    background = str(variables.get("background") or "")
    foreground = str(variables.get("foreground") or "")
    if not background.startswith("#"):
        background = "#000000"
    if not foreground.startswith("#"):
        foreground = "#ffffff"
    return foreground, background


class Field(Widget):
    """Bars, the sine trace, and a mark for every finding.

    Drawn as strips because a bar is a *column*: every row draws the same
    column at the same brightness, which is what makes it read as a bar-field
    rather than as a chart of anything.
    """

    DEFAULT_CSS = "Field { height: 1fr; }"

    #: seconds of audio the field spans, left to right
    SPAN = 8.0
    #: the page's own divider, in terminal cells
    PERIOD = 12
    OFFSETS = frozenset({0, 2, 5, 6, 8, 11})

    def __init__(self) -> None:
        super().__init__()
        self.levels: list[float] = []
        #: (x fraction, y fraction) per finding, in the order they were found
        self.marks: list[tuple[float, float]] = []

    def render_line(self, y: int) -> Strip:
        width, height = self.size.width, max(1, self.size.height)
        if not width:
            return Strip.blank(0)

        foreground, background = _ink(self.app)
        base = Style(bgcolor=background)
        levels = self.levels

        # The sine, precomputed per column so the gap between two of them can
        # be filled. Testing `int(wave) == y` alone draws one cell a column,
        # which at a terminal's vertical resolution comes out as a row of
        # disconnected dashes wherever the curve is steep -- the page draws one
        # continuous trace and so should this.
        def wave_at(column: int) -> float:
            return height / 2 + math.sin(
                (column / max(1, width)) * math.pi * 1.6 + 0.4
            ) * (height * 0.34)

        cells: list[tuple[str, str]] = []
        for column in range(width):
            char, colour = " ", None

            if column % self.PERIOD in self.OFFSETS:
                level = levels[column] if column < len(levels) else 0.0
                shade = int(26 + min(1.0, level) * 170)
                char, colour = "█", f"rgb({shade},{shade},{shade})"

            here = wave_at(column)
            before = wave_at(column - 1) if column else here
            low, high = sorted((int(before), int(here)))
            if low <= y <= high:
                # a vertical run where the curve is steep, a flat one where it
                # is not, which is what makes it read as a single line
                char = "│" if (high - low) > 1 and int(here) != y else "─"
                colour = "rgb(200,200,200)"

            cells.append((char, colour))

        # findings last, so a mark is never hidden by a bar
        used: set[int] = set()
        for index, (mx, my) in enumerate(self.marks):
            column = min(width - 1, int(mx * width))
            # findings from one published batch share a progress fraction;
            # without this they stack into a single line pretending to be one
            while column in used and column < width - 1:
                column += 1
            used.add(column)
            mark_row = int(my * (height - 1))
            if y == mark_row:
                cells[column] = ("■", foreground)
            else:
                cells[column] = ("│", "rgb(150,150,150)")

        segments: list[Segment] = []
        run, run_style = [], None
        for char, colour in cells:
            style = Style(color=colour, bgcolor=background) if colour else base
            if style == run_style:
                run.append(char)
            else:
                if run:
                    segments.append(Segment("".join(run), run_style))
                run, run_style = [char], style
        if run:
            segments.append(Segment("".join(run), run_style))
        return Strip(segments, width)

    def update(self, levels: list[float], marks: list[tuple[float, float]]) -> None:
        self.levels = levels
        self.marks = marks
        self.refresh()


class SeekBar(Static):
    """The scrub bar. Click anywhere on it to go there."""

    DEFAULT_CSS = "SeekBar { height: 1; }"

    class Seek(events.Message):
        def __init__(self, fraction: float) -> None:
            super().__init__()
            self.fraction = fraction

    def on_click(self, event: events.Click) -> None:
        width = max(1, self.size.width)
        self.post_message(self.Seek(max(0.0, min(1.0, event.x / width))))
        event.stop()


class Control(Static):
    """One clickable word in the transport."""

    DEFAULT_CSS = """
    Control { width: auto; padding: 0 1; }
    Control:hover { background: $boost; text-style: bold; }
    """

    class Pressed(events.Message):
        def __init__(self, action: str) -> None:
            super().__init__()
            self.action = action

    def __init__(self, label: str, action: str) -> None:
        super().__init__(label)
        self.action_name = action

    def on_click(self, event: events.Click) -> None:
        self.post_message(self.Pressed(self.action_name))
        event.stop()


class VibeScreen(Screen):
    """Everything the player needs, and what the scan is doing behind it."""

    CSS = """
    VibeScreen { background: $background; }
    #vibe-wrap { height: 1fr; padding: 1 3; background: $background; }
    #vibe-title { text-style: bold; padding-top: 1; }
    #vibe-meta { color: $text-muted; }
    #vibe-scan { color: $text-muted; padding-top: 1; }
    #vibe-times { padding-top: 1; }
    #vibe-row { height: auto; padding-top: 1; }
    #vibe-keys { color: $text-muted; padding-top: 1; }
    """

    BINDINGS = [
        Binding("escape,v,q", "leave", "Back to the scanner"),
        Binding("right,l", "forward", "Forward 10s"),
        Binding("left,h", "back", "Back 10s"),
        Binding("n,>", "next", "Next track"),
        Binding("p,<", "previous", "Previous"),
        Binding("s", "stop", "Stop the music"),
    ]

    def __init__(self, vibe) -> None:
        super().__init__()
        self.vibe = vibe
        self._marks: list[tuple[float, float]] = []
        self._hits = 0
        self._rng = random.Random(0xF1A6)

    def compose(self) -> ComposeResult:
        with Vertical(id="vibe-wrap"):
            yield Field()
            yield Static("", id="vibe-title")
            yield Static("", id="vibe-meta")
            yield Static("", id="vibe-scan")
            yield SeekBar("", id="vibe-seek")
            yield Static("", id="vibe-times")
            with Horizontal(id="vibe-row"):
                yield Control("<< PREV", "previous")
                yield Control("NEXT >>", "next")
                yield Control("- 10s", "back")
                yield Control("+ 10s", "forward")
                yield Control("STOP", "stop")
                yield Control("BACK", "leave")
            yield Static(self._keys(), id="vibe-keys")

    def _keys(self) -> Text:
        text = Text()
        for key, what in (
            ("<-/->", "seek"), ("p / n", "track"), ("s", "stop the music"),
            ("esc", "back to the scanner, still playing"),
        ):
            text.append(f" {key} ", style="reverse bold")
            text.append(f" {what}    ", style="dim")
        return text

    def on_mount(self) -> None:
        # 20 fps is what the envelope was measured at, so the field moves
        # exactly as fast as the audio it came from
        self.set_interval(1 / 20, self._tick)
        self._tick()

    # -- painting ----------------------------------------------------------

    def _scan_marks(self) -> list[tuple[float, float]]:
        """One mark per finding, laid out across the scan's progress.

        Appended rather than recomputed, so a mark keeps the position it was
        given -- a field that reshuffles every tick is noise, and these are
        meant to read as the same fourteen marks the hero canvas draws.
        """
        job = getattr(self.app, "queue", None)
        job = job.active if job is not None else None
        if job is None:
            return self._marks

        hits = sum(
            1 for row in job.rows.values()
            if getattr(getattr(row, "report", None), "verdict", None) is not None
            and int(getattr(row.report.verdict, "value", 0)) >= 3
        )
        while self._hits < hits:
            self._hits += 1
            done = len(job.rows)
            span = job.expected or max(done, 1)
            self._marks.append(
                (min(0.98, done / span if span else 0.0), self._rng.random())
            )
        return self._marks

    def _tick(self) -> None:
        vibe = self.vibe
        field = self.query_one(Field)
        width = max(1, field.size.width)
        field.update(vibe.bars(width, Field.SPAN), self._scan_marks())

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

        self.query_one("#vibe-scan", Static).update(self._scan_line())
        self.query_one("#vibe-seek", SeekBar).update(self._seek_bar())
        self.query_one("#vibe-times", Static).update(self._times())

    def _scan_line(self) -> Text:
        """What the scan behind this screen is doing."""
        app = self.app
        text = Text()
        queue = getattr(app, "queue", None)
        job = queue.active if queue is not None else None
        if job is None or job.source is None:
            text.append("No scan running.", style="dim")
            return text

        text.append(f"{job.source_name} · {backend_label(job.backend)}   ",
                    style="bold")
        found = len(job.rows)
        if job.expected:
            text.append(f"{found:,} of {job.expected:,}   ")
            text.append(f"{found / job.expected * 100:.0f}%   ", style="dim")
        else:
            text.append(f"{found:,} checked   ")
        if self._hits:
            text.append(f"{self._hits} flagged", style="bold red")
        if getattr(app, "_activity", None):
            text.append(f"   {app._activity}", style="dim")
        return text

    def _seek_bar(self) -> Text:
        vibe = self.vibe
        position, duration = vibe.position, vibe.duration
        width = max(10, self.size.width - 8)
        filled = int(width * (position / duration)) if duration else 0
        text = Text()
        for index in range(width):
            if index < filled:
                text.append("█")
            elif index == filled:
                text.append("█", style="bold")
            else:
                text.append("─", style="dim")
        return text

    def _times(self) -> Text:
        vibe = self.vibe
        text = Text()
        text.append(format_duration(vibe.position), style="bold")
        text.append("  /  ", style="dim")
        text.append(format_duration(vibe.duration) if vibe.duration else "--:--",
                    style="dim")
        return text

    # -- interaction -------------------------------------------------------

    @on(SeekBar.Seek)
    def _seek_clicked(self, event: SeekBar.Seek) -> None:
        duration = self.vibe.duration
        if duration:
            self._run(self.vibe.seek(event.fraction * duration))

    @on(Control.Pressed)
    def _control_pressed(self, event: Control.Pressed) -> None:
        getattr(self, f"action_{event.action}")()

    def _run(self, coroutine) -> None:
        self.app.run_worker(coroutine, group="vibe-seek", exclusive=True)

    def action_leave(self) -> None:
        """Back to the scanner. The music keeps playing."""
        self.dismiss(None)

    def action_stop(self) -> None:
        self._run(self.vibe.stop())
        self.dismiss(None)

    def action_forward(self) -> None:
        self._run(self.vibe.nudge(10))

    def action_back(self) -> None:
        self._run(self.vibe.nudge(-10))

    def action_next(self) -> None:
        self._run(self.vibe.skip(1))

    def action_previous(self) -> None:
        self._run(self.vibe.previous())
