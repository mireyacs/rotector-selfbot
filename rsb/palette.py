"""The colours an export is drawn in, shared by the PNG and the HTML.

Both renderers used to carry their own hardcoded dark palette -- one as RGB
tuples for Pillow, the other as hex literals inside a CSS template -- which
made "match the app's theme" a change in two places that could disagree. They
take one of these instead.

The defaults *are* the old palette, to the value, so an export made without
asking for anything looks exactly as it did. ``from_theme`` builds one from a
Textual theme's generated variables, which is what ``[export] follow_theme``
uses to make an export look like the app it came out of.

No Pillow, no Textual, no imports beyond the standard library: `rsb.htmlrender`
must keep working on a machine with no imaging stack, and this sits under both.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .verdict import Verdict


def _rgb(value: str) -> tuple[int, int, int]:
    """`#rrggbb` to a Pillow-shaped tuple."""
    text = value.lstrip("#")
    if len(text) == 3:  # #abc
        text = "".join(c * 2 for c in text)
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def _hex(value: str, fallback: str) -> str:
    """A usable `#rrggbb`, or the fallback.

    Theme variables are not all colours -- Textual answers ``auto 87%`` for
    body text and ``transparent`` for several others -- so anything that is not
    a plain hex triple is declined rather than rendered as a broken CSS value
    or crashed on by ``int(..., 16)``.
    """
    text = (value or "").strip()
    if not text.startswith("#"):
        return fallback
    body = text[1:]
    if len(body) not in (3, 6):
        return fallback
    try:
        int(body, 16)
    except ValueError:
        return fallback
    return f"#{body}"


#: verdict -> accent, the same five the terminal and the page use
DEFAULT_VERDICT_COLOURS: dict[Verdict, str] = {
    Verdict.THREAT: "#f43f5e",
    Verdict.CAUTION: "#facc15",
    Verdict.INFO: "#38bdf8",
    Verdict.NO_DETECTIONS: "#4ade80",
    Verdict.UNKNOWN: "#8a929c",
}


@dataclass(frozen=True)
class ExportPalette:
    """One set of colours, in hex, for whichever renderer wants them."""

    #: the page or image background
    background: str = "#121418"
    #: cards, table headers, anything raised off the background
    panel: str = "#1c2026"
    #: alternating table rows
    stripe: str = "#171a1f"
    #: rules and borders
    grid: str = "#343a42"
    #: the quieter rule the HTML uses between rows and card sections
    rule: str = "#23272e"
    #: link text, which needs to carry on the background rather than on the
    #: accent block the way `on_accent` does
    link: str = "#7aa2f7"
    #: whether this is a dark palette, for `color-scheme` and contrast picks
    dark: bool = True
    #: body text
    text: str = "#dee2e6"
    #: labels, captions, anything subordinate
    muted: str = "#8a929c"
    #: the export's title
    heading: str = "#ffa62b"
    #: links, and the active state of a filter button
    accent: str = "#2b62d9"
    #: text drawn *on* the accent
    on_accent: str = "#ffffff"
    verdicts: dict = field(default_factory=lambda: dict(DEFAULT_VERDICT_COLOURS))

    #: the name of the theme this came from, for the export's own footer
    source: str = ""

    # -- Pillow wants tuples ----------------------------------------------

    @property
    def background_rgb(self) -> tuple[int, int, int]:
        return _rgb(self.background)

    @property
    def panel_rgb(self) -> tuple[int, int, int]:
        return _rgb(self.panel)

    @property
    def stripe_rgb(self) -> tuple[int, int, int]:
        return _rgb(self.stripe)

    @property
    def grid_rgb(self) -> tuple[int, int, int]:
        return _rgb(self.grid)

    @property
    def text_rgb(self) -> tuple[int, int, int]:
        return _rgb(self.text)

    @property
    def muted_rgb(self) -> tuple[int, int, int]:
        return _rgb(self.muted)

    @property
    def heading_rgb(self) -> tuple[int, int, int]:
        return _rgb(self.heading)

    def verdict_rgb(self, verdict) -> tuple[int, int, int]:
        return _rgb(self.verdicts.get(verdict, self.text))

    def verdict_css(self, verdict) -> str:
        return self.verdicts.get(verdict, self.text)


DEFAULT = ExportPalette()


def from_theme(variables: dict, name: str = "", dark: bool = True) -> ExportPalette:
    """Build a palette from a Textual theme's generated variables.

    Verdict colours are deliberately *not* taken from the theme. They are the
    export's evidence -- a THREAT row is red because it is a threat -- and a
    monochrome theme would drain exactly the distinction a reader needs. The
    chrome follows the app; the findings keep their meaning.

    Anything the theme cannot answer with a real colour falls back to the
    default for that slot, so a partial or ansi theme yields a readable export
    rather than a half-styled one.
    """
    get = lambda key, fallback: _hex(str(variables.get(key, "")), fallback)

    background = get("background", DEFAULT.background)
    panel = get("panel", DEFAULT.panel)
    return replace(
        DEFAULT,
        background=background,
        panel=panel,
        # surface sits between the two in most themes and is what the app uses
        # for alternating rows; falling back to the panel keeps rows visible
        stripe=get("surface", panel),
        # a rule has to move *away* from the background to be seen, and which
        # direction that is depends on the theme: lightening the panel of a
        # light theme lands on white and draws nothing
        grid=get(
            "panel-lighten-2" if dark else "panel-darken-2",
            DEFAULT.grid if dark else "#c8ccd2",
        ),
        # "text" is usually `auto NN%` rather than a colour, so foreground is
        # the one to ask for
        rule=get(
            "panel-lighten-1" if dark else "panel-darken-1",
            DEFAULT.rule if dark else "#dfe3e8",
        ),
        text=get("foreground", DEFAULT.text),
        muted=get("text-muted", DEFAULT.muted),
        heading=get("primary", DEFAULT.heading),
        accent=get("accent", DEFAULT.accent),
        on_accent=background,
        # a link sits on the background, so it takes the lighter of the two
        # brand colours on a dark theme and the darker on a light one
        link=get("primary-lighten-2" if dark else "primary-darken-1",
                 get("primary", DEFAULT.link)),
        dark=dark,
        source=name,
    )
