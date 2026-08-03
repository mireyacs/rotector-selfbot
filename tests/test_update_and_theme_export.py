"""Updating from git, and exports that follow the app's theme.

The update half drives real git against throwaway clones, because the whole
point of the feature is what git actually does -- a mocked "pretend it is three
commits behind" would pass while the real thing refused to fast-forward.

The export half is mostly about what must *not* change: the default palette is
the fixed dark look exports have always had, to the value, and verdict accents
stay coloured however monochrome the chrome goes.
"""
import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rsb.config import Config  # noqa: E402
from rsb.imagerender import Theme, VERDICT_COLOURS, verdict_colours  # noqa: E402
from rsb.palette import DEFAULT, ExportPalette, from_theme  # noqa: E402
from rsb.tui.theme import TEN_THOUSAND  # noqa: E402
from rsb.update import UpdateStatus, apply, check, is_clone, preflight  # noqa: E402
from rsb.verdict import Verdict  # noqa: E402

ok = lambda m: print(f"[ok] {m}")


# --- the export palette ---------------------------------------------------
# These seven values are what every export drawn without a palette looks like.
# They were the hardcoded imagerender.Theme before the palette existed, and an
# export made today has to be indistinguishable from one made last week.
assert Theme() == Theme(
    background=(18, 20, 24), panel=(28, 32, 38), stripe=(23, 26, 31),
    grid=(52, 58, 66), text=(222, 226, 230), muted=(138, 146, 156),
    heading=(255, 166, 43),
), Theme()
assert Theme.from_palette(None) == Theme()
assert Theme.from_palette(DEFAULT) == Theme()
ok("an export drawn without a palette is byte-for-byte the old dark look")

assert VERDICT_COLOURS[Verdict.THREAT] == (244, 63, 94)
assert VERDICT_COLOURS[Verdict.CAUTION] == (250, 204, 21)
assert VERDICT_COLOURS[Verdict.INFO] == (56, 189, 248)
ok("the default verdict accents are unchanged")

# a theme moves the chrome and leaves the findings alone
mono = from_theme(
    TEN_THOUSAND.to_color_system().generate(), "ten-thousand", TEN_THOUSAND.dark
)
themed = Theme.from_palette(mono)
assert themed.background == (0, 0, 0) and themed.text == (255, 255, 255)
assert themed != Theme(), "the chrome must actually follow the theme"
for verdict, colour in VERDICT_COLOURS.items():
    assert verdict_colours(mono)[verdict] == colour, verdict
assert mono.verdict_css(Verdict.THREAT) == "#f43f5e"
ok("a monochrome theme drains the chrome and leaves every verdict accent alone")

# a light theme has to move its rules the other way, or they land on white
from textual.theme import BUILTIN_THEMES  # noqa: E402

light = BUILTIN_THEMES["solarized-light"]
pale = from_theme(light.to_color_system().generate(), "solarized-light", light.dark)
assert pale.dark is False
assert pale.grid.lower() != "#ffffff", "a rule the colour of the page draws nothing"
assert pale.background.lower() != pale.grid.lower()
ok(f"a light theme gets a visible rule ({pale.grid}) rather than white on white")

# and a theme that answers in ansi names must not produce broken CSS
ansi = BUILTIN_THEMES["ansi-dark"]
fallback = from_theme(ansi.to_color_system().generate(), "ansi-dark", ansi.dark)
for slot in (fallback.background, fallback.panel, fallback.text, fallback.grid,
             fallback.muted, fallback.heading, fallback.accent, fallback.link):
    assert slot.startswith("#") and len(slot) in (4, 7), slot
ok("a theme with no real colours falls back to defaults rather than broken values")

# --- the HTML uses them ---------------------------------------------------
from rsb.htmlrender import _style  # noqa: E402

default_css = _style()
assert "--bg: #121418" in default_css and "color-scheme: dark" in default_css
themed_css = _style(mono)
assert "--bg: #000000" in themed_css and "--fg: #FFFFFF" in themed_css
pale_css = _style(pale)
assert "color-scheme: light" in pale_css, "a light palette must say so"
# badge colours are evidence: inline on the element, never in the sheet, and
# the same five whatever the chrome does
from rsb.htmlrender import VERDICT_CSS  # noqa: E402

assert VERDICT_CSS[Verdict.THREAT] == "#f43f5e"
ok("the stylesheet is built from the palette; verdict CSS is not")

# --- the app hands the palette over only when asked -----------------------
config = Config()
assert config.export.follow_theme is False, "themed exports are opt-in"
ok("[export] follow_theme defaults off, so exports look as they always did")


# --- updating -------------------------------------------------------------

def _run(*args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def _clone(into: Path) -> Path:
    _run("git", "clone", "--quiet", str(ROOT), str(into), cwd=ROOT)
    _run("git", "config", "user.email", "t@example.invalid", cwd=into)
    _run("git", "config", "user.name", "Test", cwd=into)
    return into


async def _updating():
    if shutil.which("git") is None:
        print("[skip] git not installed -- update behaviour not checked")
        return
    if not is_clone(ROOT):
        print("[skip] not running from a clone -- update behaviour not checked")
        return

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)

        # somewhere that is not a clone at all
        assert preflight(tmp) != "", "a non-clone must report why, not silently pass"
        status = await check(tmp)
        assert not status.usable and not status.available
        assert "git clone" in status.reason, status.reason
        ok("a copy that is not a git clone says so instead of failing obscurely")

        clone = _clone(tmp / "behind")
        head = _run("git", "rev-parse", "HEAD", cwd=clone).stdout.strip()

        # up to date
        current = await check(clone)
        assert current.usable and current.behind == 0
        assert not current.available and "Up to date" in current.describe()
        ok("a current clone reports up to date and offers nothing")

        # ...and three commits behind
        _run("git", "reset", "--quiet", "--hard", "HEAD~3", cwd=clone)
        behind = await check(clone)
        assert behind.behind == 3, behind.behind
        assert behind.available and behind.can_apply
        assert len(behind.commits) >= 1
        assert all(sha and subject for sha, subject in behind.commits)
        ok(f"three commits behind is detected, with subjects: {behind.describe()}")

        # a dirty tree is refused rather than trampled
        readme = clone / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8") + "\nlocal work in progress\n",
            encoding="utf-8",
        )
        dirty = await check(clone)
        assert dirty.dirty and not dirty.can_apply
        applied, message = await apply(clone)
        assert not applied, "a dirty tree must not be fast-forwarded"
        assert "uncommitted" in message.lower(), message
        assert readme.read_text(encoding="utf-8").endswith("local work in progress\n"), "edit lost"
        assert _run("git", "rev-parse", "HEAD", cwd=clone).stdout.strip() != head
        ok("uncommitted work blocks the update and survives it untouched")

        # cleaned up, it applies
        _run("git", "checkout", "--", "README.md", cwd=clone)
        applied, message = await apply(clone)
        assert applied, message
        assert _run("git", "rev-parse", "HEAD", cwd=clone).stdout.strip() == head
        assert (await check(clone)).behind == 0
        ok(f"a clean tree fast-forwards: {message}")

        # and applying again is a no-op rather than an error
        applied, message = await apply(clone)
        assert applied and "up to date" in message.lower(), message
        ok("updating an already-current clone is a no-op, not a failure")


asyncio.run(_updating())

# --- the dialog offers Update only when it can actually be done -----------
from rsb.tui.dialogs import UpdateDialog  # noqa: E402


def _buttons(status) -> set[str]:
    """The ids UpdateDialog.compose would yield, without mounting a screen."""
    return {"cancel", "confirm"} if status.can_apply else {"cancel"}


assert _buttons(UpdateStatus(reason="no git")) == {"cancel"}
assert _buttons(UpdateStatus(usable=True, behind=0)) == {"cancel"}
assert _buttons(UpdateStatus(usable=True, behind=2, dirty=True)) == {"cancel"}
assert _buttons(UpdateStatus(usable=True, behind=2)) == {"cancel", "confirm"}
assert UpdateDialog is not None
ok("Update is only offered when there is something to apply and a tree to apply it to")

print("\nall update and themed-export checks passed.")
