"""The project page's two-value palette, as a theme the terminal can wear.

``docs/index.html`` is pure white ink on pure black ground with no grey fills:
tone there comes from the density of hairline bars, never from a tinted panel.
A terminal cannot draw a hairline, but it can refuse the tinted panel, and that
is most of what makes the field read as one wall rather than stacked planes.

Registering this makes "ten-thousand" an entry in the theme selector (the
command palette's *Theme* command) alongside the Textual built-ins. It is not
the default -- the app opens in whatever Textual gives it, and a user who wants
the page's look picks it.

The semantic colours keep their hue on purpose. Verdict styling in the app is
literal Rich markup ("bold red" for THREAT, "yellow" for a caution), and that
colour is evidence rather than decoration: it is the one thing on the page that
is allowed to carry a hue. Draining it to match the chrome would make a
prettier screenshot of a less honest program.

``tools/screenshots.py`` renders the page's screenshots under this theme, and
``tools/thumbnail.py`` draws the og:image from the same constants, so the page,
its pictures and its thumbnail cannot drift apart.
"""
from __future__ import annotations

from textual.theme import Theme

from ..config import write_section

#: pure black ground and pure white ink -- the page's `--ground` and `--ink`,
#: set literally rather than as a near-black and an off-white
GROUND = "#000000"
INK = "#ffffff"

#: the page's `--rule`: every structural border there, every divider here
RULE = "#2a2a2a"
#: the page's `--texture-2` and quiet stroke, for the two tones a divider or a
#: scrollbar needs above the rule
TEXTURE = "#3a3a3a"
QUIET = "#9a9a9a"

#: the darkest step that is still not the ground, so a pane header separates
#: from the field without becoming a grey fill
NEAR_GROUND = "#101010"
HOVER = "#161616"

#: Evidence. These are not a new choice: they are what the app already renders.
#: Verdicts are styled with bare Rich colour names ("bold red" for THREAT), and
#: Textual resolves those through Rich's Monokai palette, so writing them down
#: here documents the program rather than restyling it.
THREAT = "#f4005f"
CAUTION = "#fd971f"
CLEAR = "#98e024"
NOTE = "#58d1eb"

#: One path does not agree with the rest. A Rich ``Text`` handed to a
#: ``Static(markup=False)`` -- which is what the status strip is -- resolves a
#: bare colour name through Rich's *standard* palette instead, so "bold red"
#: lands on a pure #ff0000 there while the verdict column beside it is #f4005f.
#: Naming the colour removes the fork; see ``evidence_style``.
_ANSI_EVIDENCE = {
    "red": THREAT,
    "yellow": CAUTION,
    "green": CLEAR,
    "cyan": NOTE,
}


def evidence_style(style: str) -> str:
    """Rewrite bare colour names in a Rich style string to explicit hexes.

    ``"bold red"`` becomes ``"bold #f4005f"``. Attributes (bold, dim) and
    anything already explicit are passed through untouched.
    """
    return " ".join(_ANSI_EVIDENCE.get(word, word) for word in style.split())

#: the name that appears in the theme selector
NAME = "ten-thousand"

TEN_THOUSAND = Theme(
    name=NAME,
    primary=INK,
    secondary=QUIET,
    accent=INK,
    foreground=INK,
    background=GROUND,
    # the field is one surface; only a header sits fractionally off it
    surface=GROUND,
    panel=NEAR_GROUND,
    error=THREAT,
    warning=CAUTION,
    success=CLEAR,
    dark=True,
    variables={
        # The page's Pure Ink Rule: a character a reader is meant to read is
        # ink at full opacity, never ink dimmed toward the ground. Textual's
        # default drops body text to 87%, and `Theme.text_alpha` does not move
        # it -- as of 8.2.8 that field is stored and never read -- so the value
        # has to be pinned here.
        "text": "auto 100%",
        # dividers step through the page's own rule and texture tones instead
        # of through shades Textual derives from the panel colour
        "panel-lighten-1": "#1c1c1c",
        "panel-lighten-2": RULE,
        "panel-lighten-3": TEXTURE,
        # a border is a rule: ink when the widget has focus, hairline when not
        "border": INK,
        "border-blurred": RULE,
        # the one tone that is text but not content -- column and pane labels
        "text-muted": QUIET,
        "text-disabled": TEXTURE,
        "block-hover-background": HOVER,
        # selection on the page is a full inversion, not a tint
        "input-selection-background": INK,
        "input-selection-foreground": GROUND,
        "screen-selection-background": INK,
        "screen-selection-foreground": GROUND,
        # the footer earns no fill of its own
        "footer-background": GROUND,
        "footer-key-foreground": INK,
        "footer-description-foreground": QUIET,
        "scrollbar": RULE,
        "scrollbar-hover": TEXTURE,
        "scrollbar-active": QUIET,
        "scrollbar-background": GROUND,
        "scrollbar-background-hover": GROUND,
        "scrollbar-background-active": GROUND,
    },
)


def register(app) -> None:
    """Add the theme to ``app``'s selector, without switching to it."""
    app.register_theme(TEN_THOUSAND)


class ThemeMemory:
    """Restores the theme the user last picked, and remembers new ones.

    Textual's theme picker applies each theme as you arrow through the list,
    so a write on every change would rewrite ``config.toml`` a dozen times
    while somebody is only *looking*. The write is therefore deferred until
    the choice has held still for a couple of seconds, which is also what
    stops a browse that ends in Escape from being saved as a decision.

    An unwritable config is not worth interrupting a scan for: the theme still
    applies for this session, it just will not be there next time.
    """

    #: seconds a choice must hold before it is written
    DELAY = 2.0

    def __init__(self, app, config, persist: bool = True) -> None:
        self.app = app
        self.config = config
        #: False for headless tooling. ``Config()`` built in a script still
        #: resolves ``config_path()`` to a real file -- ``./config.toml`` if one
        #: is there -- so a screenshot run would otherwise rewrite the
        #: operator's own config, token and all.
        self.persist = persist
        self._timer = None

    def install(self) -> None:
        register(self.app)

        saved = (self.config.ui.theme or "").strip()
        if saved and saved in self.app.available_themes:
            self.app.theme = saved
        elif saved:
            # a theme that no longer exists, or a typo. Say so rather than
            # silently opening in something else.
            self.app.notify(
                f"Unknown theme {saved!r} in config; using the default.",
                severity="warning",
            )

        # subscribed last, so restoring above is not itself recorded as a pick
        if self.persist:
            self.app.theme_changed_signal.subscribe(self.app, self._changed)

    def _changed(self, _theme=None) -> None:
        if self._timer is not None:
            self._timer.stop()
        self._timer = self.app.set_timer(self.DELAY, self._write)

    def _write(self) -> None:
        self._timer = None
        chosen = self.app.theme
        if chosen == self.config.ui.theme:
            return
        self.config.ui.theme = chosen
        try:
            write_section(self.config.config_path(), "ui", {"theme": chosen})
        except OSError as exc:
            self.app.notify(
                f"Theme applied, but could not be saved: {exc}", severity="warning"
            )
