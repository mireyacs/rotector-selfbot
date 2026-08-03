"""Record the screenshots as short loops of the program actually being used.

    python tools/motion.py

The stills say what each pane looks like. These say what it *does*: a scan
filling a table row by row, a cursor walking down it while the detail pane
follows, proxies reporting one at a time, settings moving between tabs. Every
frame is a real render of the real app under the real theme -- the same
machinery as ``tools/screenshots.py``, driven through a few more steps -- so
this is a recording rather than an animation of an idea.

Each pane becomes one animated WebP beside its SVG. The page keeps the SVG as
what it shows by default: it is vector, so it stays crisp on any display, and
it is what somebody sees with motion off, with reduced-motion set, or with no
JavaScript at all. The WebP is swapped in only while the figure is on screen
and motion is running.

Frames are rasterised through ImageMagick's rsvg delegate, so this needs
ImageMagick with SVG support as well as Pillow.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tools.screenshots as shots  # noqa: E402
import tools.sitesvg as sitesvg  # noqa: E402
import rsb.tui.app as appmod  # noqa: E402
from rsb.discord.gateway import GuildMember  # noqa: E402
from rsb.proxy import ProbeResult  # noqa: E402
from rsb.rotector import MemberReport  # noqa: E402
from rsb.tui.app import Row  # noqa: E402
from rsb.tui.proxies import ProxyTesterApp  # noqa: E402
from rsb.tui.settings import SettingsScreen  # noqa: E402
from rsb.tui.theme import NAME as THEME_NAME  # noqa: E402

OUT = ROOT / "docs" / "screenshots"

#: Rendered width. Every figure sits in the 7fr half of a split inside an
#: 1180px wrap, so none of them is displayed above ~690px; 1200 is a shade
#: under 1.8x, which covers a dense screen without the loops becoming the
#: heaviest thing on the page.
WIDTH = 1200

#: ~420ms a frame is a readable pace: fast enough to feel live, slow enough
#: that a row appearing can actually be read before the next one lands.
FRAME_MS = 420
#: the last frame holds, so the loop has somewhere to rest rather than snapping
END_HOLD_MS = 1600

QUALITY = 62


def _magick() -> str:
    for name in ("magick", "convert"):
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit("ImageMagick is needed to rasterise the frames.")


async def record(app, label: str, steps, size=shots.SIZE, settle: float = 2.5):
    """Run ``app`` through ``steps``, returning one SVG string per frame."""
    frames: list[str] = []
    async with app.run_test(size=size) as pilot:
        app.theme = THEME_NAME
        for _ in range(int(settle / 0.1)):
            await pilot.pause(0.1)
            if getattr(app, "_source_rows", None) or getattr(app, "entries", None):
                break
        await pilot.pause(0.4)
        for index, step in enumerate(steps):
            await step(app, pilot)
            await pilot.pause(0.25)
            frames.append(sitesvg.render(app, label, f"rsb-f{index}"))
    return frames


def encode(frames: list[str], path: Path) -> None:
    """Rasterise the frames and write one animated WebP."""
    from PIL import Image

    magick = _magick()
    images = []
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        for index, svg in enumerate(frames):
            source = tmp / f"{index:03d}.svg"
            target = tmp / f"{index:03d}.png"
            source.write_text(svg, encoding="utf-8")
            subprocess.run(
                [magick, "-background", "black", str(source),
                 "-resize", f"{WIDTH}x", str(target)],
                check=True, capture_output=True,
            )
            images.append(Image.open(target).convert("RGB"))

        durations = [FRAME_MS] * len(images)
        durations[-1] = END_HOLD_MS
        images[0].save(
            path, "WEBP", save_all=True, append_images=images[1:],
            duration=durations, loop=0, quality=QUALITY, method=6,
        )
    for image in images:
        image.close()


# -- the sequences ---------------------------------------------------------
# Each one is what somebody would actually do with that pane, in order.


def results_steps():
    """A scan filling the table, then reading down it."""
    rows = shots.make_rows()
    ordered = list(rows.items())

    async def title(app, pilot):
        app.current_source = app.sources[-1] if app.sources else None
        app.query_one("#results-title", appmod.Static).update(
            "RESULTS - Roblox Trading Hub (server)"
        )
        app._set_activity("Reading the member list from the gateway")
        app._rebuild_table()

    steps = [title]

    # the findings arrive first, as they do in a real scan: flagged members are
    # published as each batch comes back
    for count in (1, 2, 3, 5, 8):
        async def fill(app, pilot, count=count):
            app.rows.update(dict(ordered[:count]))
            app._set_activity(f"Checking members - {count * 1200:,} of 10,833")
            app._rebuild_table()
        steps.append(fill)

    async def finish(app, pilot):
        app.rows.update(rows)
        app._rebuild_table()
        app._set_status(
            "Scanned 10,833 members in 4m 12s - 5 flagged as THREAT.  "
            "Data: Rotector (https://rotector.com)",
            "bold red",
        )
        table = app.query_one("#results", appmod.DataTable)
        table.focus()
        table.move_cursor(row=0)
    steps.append(finish)

    # then a person reads down the findings, and the detail pane follows
    for row in (1, 2, 3, 4, 3, 1):
        async def walk(app, pilot, row=row):
            table = app.query_one("#results", appmod.DataTable)
            table.move_cursor(row=row)
        steps.append(walk)
    return steps


def sources_steps():
    """Moving down the source list, the way you pick something to scan."""
    steps = []

    async def start(app, pilot):
        app.query_one("#guilds", appmod.DataTable).focus()
        app._set_status("4 servers, 2 group DMs, friends and requests. Press s to scan.")
    steps.append(start)

    for row in (1, 2, 3, 4, 5, 6, 7, 8, 6, 3, 1):
        async def move(app, pilot, row=row):
            table = app.query_one("#guilds", appmod.DataTable)
            if row < table.row_count:
                table.move_cursor(row=row)
        steps.append(move)
    return steps


def settings_steps():
    """Moving between the settings tabs.

    Not recorded at the moment: ``docs/index.html`` shows the settings still
    only in the README, and shipping a loop nothing displays is dead weight.
    Kept because adding a settings figure to the page is a line in ``main``.
    """
    steps = []

    async def open_it(app, pilot):
        app.push_screen(SettingsScreen(app.config))
        await pilot.pause(0.8)
    steps.append(open_it)

    for tab in ("rotector", "okappiki", "update", "ui", "scan", "export",
                "moderation", "discord"):
        async def switch(app, pilot, tab=tab):
            from textual.widgets import TabbedContent
            try:
                app.screen.query_one(TabbedContent).active = f"tab-{tab}"
            except Exception:
                pass
        steps.append(switch)
    return steps


def proxies_steps():
    """Probes landing one at a time, which is how a test actually reads."""
    entries = ["direct", "23.95.150.11:6114", "198.23.239.134:6540",
               "45.38.107.97:6014", "107.172.163.27:6543"]
    outcomes = {
        "direct": (True, 210.0, 200, 50, 49, "own budget: 50/window"),
        "23.95.150.11:6114": (True, 340.0, 200, 50, 49, "own budget: 50/window"),
        "198.23.239.134:6540": (True, 512.0, 200, 50, 44,
                                "5 other request(s) already on this exit's window"),
        "45.38.107.97:6014": (False, None, None, None, None,
                              "ConnectError: All connection attempts failed"),
        "107.172.163.27:6543": (True, 288.0, 403, None, None,
                                "Rotector refused this exit IP"),
    }
    steps = []

    async def start(app, pilot):
        app.entries = list(entries)
        app.results.clear()
        app._rebuild()
        app._set_status("Testing 5 proxies against the Rotector API...")
        app.query_one("#table", appmod.DataTable).focus()
    steps.append(start)

    for index, entry in enumerate(entries):
        async def probe(app, pilot, entry=entry, index=index):
            ok_, latency, status, limit, remaining, note = outcomes[entry]
            result = ProbeResult(raw=entry, url=None, label=entry, ok=ok_)
            result.latency_ms = latency
            result.status = status
            result.rate_limit = limit
            result.rate_remaining = remaining
            if not ok_:
                result.error = note
            else:
                result.notes.append(note)
            app.results[entry] = result
            app._rebuild()
            app._refresh_head()
            app._set_status(f"Tested {index + 1} of {len(entries)}...")
            table = app.query_one("#table", appmod.DataTable)
            table.move_cursor(row=index)
        steps.append(probe)

    async def done(app, pilot):
        app._set_status(
            "Tested 5 - 2 with their own budget, 1 shared, 1 dead. "
            "Combined 100 req/window."
        )
        app.query_one("#table", appmod.DataTable).move_cursor(row=0)
    steps.append(done)
    return steps


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    appmod.DiscordHTTP = shots.StubHTTP
    appmod.DiscordGateway = shots.StubGateway

    def config():
        cfg = shots.Config()
        cfg.token = "fake.test.token"
        cfg.proxy.file = "/nonexistent"
        cfg.ui.theme = THEME_NAME
        cfg.update.check_on_start = False
        return cfg

    jobs = [
        ("results", "RESULTS",
         lambda: appmod.ScannerApp(config(), persist_theme=False), results_steps()),
        ("sources", "SOURCES",
         lambda: appmod.ScannerApp(config(), persist_theme=False), sources_steps()),
        ("proxies", "PROXY TESTER",
         lambda: ProxyTesterApp(config(), persist_theme=False), proxies_steps()),
    ]

    for name, label, build, steps in jobs:
        frames = await record(build(), label, steps)
        path = OUT / f"{name}.webp"
        encode(frames, path)
        size = path.stat().st_size // 1024
        print(f"  wrote {path.relative_to(ROOT)}  {len(frames)} frames, {size} KB")

    total = sum((OUT / f"{n}.webp").stat().st_size for n, _, _, _ in jobs) // 1024
    print(f"\n{len(jobs)} loops, {total} KB in total")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
