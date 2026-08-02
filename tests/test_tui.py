"""Headless TUI smoke test: stub Discord, use the REAL Rotector API."""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import rsb.tui.app as appmod
from rsb.config import Config
from rsb.discord.http import Channel, Guild
from rsb.discord.gateway import GuildMember
from rsb.verdict import Verdict

# Discord ids that Rotector actually has data for, plus filler.
SEEDED = ["1", "2", "3", "4", "5"]
MEMBERS = {
    i: GuildMember(id=i, username=f"user{i}", global_name=f"User {i}", nick=None)
    for i in SEEDED + [str(900_000_000_000_000_000 + n) for n in range(40)]
}
MEMBERS["1"].nick = "suspicious_guy"


class FakeHTTP:
    def __init__(self, token, **kw): pass
    async def me(self): return {"username": "tester", "global_name": "Tester", "id": "999"}
    async def guilds(self):
        return [
            Guild(id="111", name="Test Server A", owner=True, permissions=8,
                  member_count=len(MEMBERS), presence_count=10),
            Guild(id="222", name="Another Server", owner=False, permissions=0,
                  member_count=3, presence_count=1),
        ]
    async def relationships(self): return []
    async def widget(self, gid): return None
    async def private_channels(self): return []
    async def channels(self, gid):
        return [Channel(id="c1", name="general", type=0, position=0, everyone_can_view=True)]
    async def aclose(self): pass


class FakeGateway:
    def __init__(self, token, bot=False): self.user = None
    async def connect(self, timeout=45.0):
        self.user = {"username": "tester"}; return self.user
    async def fetch_members(self, gid, channels, expected=None, on_progress=None,
                            on_members=None, **kwargs):
        if on_progress: on_progress(len(MEMBERS), expected, "#general")
        # members are streamed to the caller as they are discovered
        if on_members: on_members(list(MEMBERS.values()))
        return dict(MEMBERS)
    async def close(self): pass


appmod.DiscordHTTP = FakeHTTP
appmod.DiscordGateway = FakeGateway


async def main():
    cfg = Config()
    cfg.token = "fake"
    app = appmod.ScannerApp(cfg)

    async with app.run_test(size=(130, 42)) as pilot:
        await pilot.pause(1.0)
        guilds = app.query_one("#guilds", appmod.DataTable)
        # the pane groups sources under collapsible headers now
        from rsb.tui.app import GROUP_KEY
        real = [k for k in app._source_rows if not k.startswith(GROUP_KEY)]
        assert sum(1 for k in real if k.startswith("guild:")) == 2, real
        print(f"[ok] sources pane: {len(real)} sources under "
              f"{len(app._source_rows) - len(real)} group headers")
        print(f"[ok] guild list populated: {guilds.row_count} rows")

        # highlighting a server shows a pre-scan time estimate
        guilds.move_cursor(row=app._source_rows.index(
            next(k for k in app._source_rows if k.startswith("guild:"))
        ))
        await pilot.pause(0.4)
        estimate = app.query_one("#detail-body", appmod.Static)
        # Static keeps its content in a name-mangled attribute in Textual 8
        content = getattr(estimate, "_Static__content", "")
        shown = content.plain if hasattr(content, "plain") else str(content)
        assert "Estimated scan time" in shown, shown[:200]
        print(f"[ok] pre-scan estimate shown: "
              f"{[l for l in shown.splitlines() if 'Estimated' in l][0].strip()!r}")

        # scan the first server
        await pilot.press("s")
        for _ in range(80):
            await pilot.pause(0.25)
            if app.query_one("#results", appmod.DataTable).row_count >= len(MEMBERS):
                break
        results = app.query_one("#results", appmod.DataTable)
        # default filter lists findings only -- the clear majority are hidden
        assert len(app.rows) == len(MEMBERS), "data lost, not just hidden"
        assert 0 < results.row_count < len(MEMBERS), results.row_count
        print(f"[ok] {results.row_count} findings listed of {len(MEMBERS)} scanned "
              f"(default filter: {app.filter_mode.value})")
        for row_id in app._shown:
            v = app.rows[row_id].report.verdict
            assert v not in app.hidden_verdicts, f"{row_id} is {v.name}, should be hidden"
        print(f"[ok] nothing with a hidden verdict is listed "
              f"({sorted(v.name for v in app.hidden_verdicts)})")

        summary = app.query_one("#summary", appmod.Static)
        content = getattr(summary, "_Static__content", "")
        summary_text = content.plain if hasattr(content, "plain") else str(content)
        assert "hidden" in summary_text, summary_text
        print(f"[ok] summary discloses what is hidden: {summary_text.strip()[-30:]!r}")

        # the status bar reports a single-route budget when no proxies are set
        status = app._compose_status().plain
        assert "budget" in status, status
        assert "routes" not in status, "routes indicator shown without proxies"
        print(f"[ok] status bar without proxies: {status[-40:]!r}")

        verdicts = {}
        for r in app.rows.values():
            verdicts[r.report.verdict.name] = verdicts.get(r.report.verdict.name, 0) + 1
        print(f"[ok] verdict spread: {verdicts}")
        assert verdicts.get("THREAT", 0) > 0, "expected at least one THREAT from seeded ids"

        # detail pane for the threat row
        threat_id = next(i for i, r in app.rows.items() if r.report.verdict is Verdict.THREAT)
        detail = app._render_detail(app.rows[threat_id])
        assert "Linked Roblox accounts" in detail and "rotector.com" in detail
        print(f"[ok] detail pane renders ({len(detail)} chars, attribution present)")

        # cycling to "Everything" reveals the hidden rows again
        modes = {}
        for _ in range(len(appmod.FilterMode)):
            await pilot.press("f")
            await pilot.pause(0.25)
            modes[app.filter_mode.value] = app.query_one(
                "#results", appmod.DataTable
            ).row_count
        print(f"[ok] filter cycle: {modes}")
        assert modes["Everything"] == len(MEMBERS), "Everything must show all rows"
        assert modes["Threats only"] > 0
        assert app.filter_mode is appmod.FilterMode.FINDINGS, "cycle did not wrap"
        print(f"[ok] 'Everything' restores all {len(MEMBERS)} rows; cycle wraps to "
              f"{app.filter_mode.value}")

        # search targets whichever table has focus
        app.query_one("#results", appmod.DataTable).focus()
        await pilot.pause(0.2)
        await pilot.press("slash"); await pilot.pause(0.2)
        for ch in "suspicious": await pilot.press(ch)
        await pilot.pause(0.4)
        n = app.query_one("#results", appmod.DataTable).row_count
        print(f"[ok] search 'suspicious' -> {n} row(s)")
        assert n == 1
        await pilot.press("escape")
        app.query_one("#search", appmod.Input).value = ""
        app.search_term = ""
        app._rebuild_table()
        await pilot.pause(0.3)

        # the same key filters the sources pane when that has focus
        app.query_one("#guilds", appmod.DataTable).focus()
        await pilot.pause(0.2)
        await pilot.press("slash"); await pilot.pause(0.2)
        assert app._search_target == "guilds", app._search_target
        for ch in "another":
            await pilot.press(ch)
        await pilot.pause(0.5)
        from rsb.tui.app import GROUP_KEY
        visible = [k for k in app._source_rows if not k.startswith(GROUP_KEY)]
        assert len(visible) == 1, visible
        print(f"[ok] source search narrowed the pane to {len(visible)} source")
        # escape closes it; "/" would just type a slash into the focused input
        await pilot.press("escape"); await pilot.pause(0.4)
        assert app.source_search == "", app.source_search
        restored = [k for k in app._source_rows if not k.startswith(GROUP_KEY)]
        assert len(restored) > 1, restored
        print(f"[ok] clearing it restores all {len(restored)} sources")

        # --- export: dialog, filter scope, segmentation, remembered defaults
        import tempfile
        from rsb.tui.dialogs import ExportDialog
        from textual.widgets import Button, Checkbox, Input as TInput, SelectionList

        workdir = Path(tempfile.mkdtemp())
        app.config.export.directory = str(workdir / "exports")
        app.config.source = workdir / "config.toml"

        app.filter_mode = appmod.FilterMode.THREATS
        app._rebuild_table()
        await pilot.pause(0.3)
        shown_now = len(app._shown)
        assert 0 < shown_now < len(MEMBERS)

        app.action_export()
        for _ in range(40):
            await pilot.pause(0.1)
            if isinstance(app.screen, ExportDialog):
                break
        assert isinstance(app.screen, ExportDialog), "export dialog never opened"
        print(f"[ok] export dialog opened (filter shows {shown_now} of {len(MEMBERS)})")

        dialog = app.screen
        dialog.query_one("#formats", SelectionList).select_all()
        dialog.query_one("#segment", TInput).value = "1"
        dialog.query_one("#remember", Checkbox).value = True
        dialog.query_one("#confirm", Button).press()
        for _ in range(60):
            await pilot.pause(0.1)
            if not isinstance(app.screen, ExportDialog):
                break

        folders = list((workdir / "exports").glob("*"))
        assert folders, "no export folder written"
        files = sorted(f.name for f in folders[0].iterdir())
        print(f"[ok] exported into {folders[0].name}/ -> {len(files)} files")

        csv_parts = [f for f in files if f.endswith(".csv")]
        assert len(csv_parts) == shown_now, (
            f"{len(csv_parts)} csv segments for {shown_now} rows at segment_size=1"
        )
        assert any(f.endswith(".txt") for f in files), files
        assert any(f.endswith(".json") for f in files), files
        # images and pages now live in their own subfolders
        pngs = sorted((folders[0] / "png").glob("*.png"))
        htmls = sorted((folders[0] / "html").glob("*.html"))
        assert pngs, sorted(p.name for p in folders[0].rglob("*"))
        assert htmls, sorted(p.name for p in folders[0].rglob("*"))
        assert not [f for f in files if f.endswith((".png", ".html"))], files
        from PIL import Image
        image = Image.open(pngs[0])
        assert image.format == "PNG" and image.width > 200
        print(f"[ok] segmented into {len(csv_parts)} CSV parts, plus TXT, JSON, "
              f"a {image.width}x{image.height} PNG in png/ and a page in html/")

        import json as _json
        payload = _json.loads(
            next(f for f in folders[0].iterdir() if f.suffix == ".json").read_text()
        )
        assert len(payload["members"]) == shown_now, (
            f"export wrote {len(payload['members'])} rows, but the filter shows "
            f"{shown_now} -- export ignored the filter"
        )
        assert "Threats only" in payload["scope"], payload["scope"]
        print(f"[ok] export honoured the filter: {len(payload['members'])} rows, "
              f"scope {payload['scope']!r}")

        import tomllib as _tomllib
        remembered = _tomllib.loads((workdir / "config.toml").read_text())
        assert remembered["export"]["segment_size"] == 1
        assert set(remembered["export"]["formats"]) == {
            "csv", "txt", "json", "png", "html"
        }
        print(f"[ok] defaults remembered in config.toml: "
              f"{remembered['export']['formats']}, segment {remembered['export']['segment_size']}")

        # --- copy the selected member
        table = app.query_one("#results", appmod.DataTable)
        table.move_cursor(row=0)
        await pilot.pause(0.2)
        row = app._selected_row()
        assert row is not None
        summary = app._member_summary(row)
        assert row.member.id in summary and "rotector.com" in summary
        app.action_copy()
        await pilot.pause(0.2)
        print(f"[ok] copy produced a {len(summary)}-char summary with attribution")

        app.filter_mode = appmod.FilterMode.FINDINGS
        app._rebuild_table()
        await pilot.pause(0.2)

        # render the screen so we can eyeball the layout
        app.filter_mode = appmod.FilterMode.ATTENTION
        app._rebuild_table()
        await pilot.pause(0.4)
        print("\n" + "=" * 130)
        from rich.console import Console
        from rich.segment import Segments
        console = Console(width=130, height=42, force_terminal=True, color_system="truecolor")
        strips = app.screen._compositor.render_strips()
        segs = []
        for strip in strips:
            segs.extend(strip._segments); segs.append(__import__("rich.segment", fromlist=["Segment"]).Segment("\n"))
        console.print(Segments(segs))

    print("\nALL TUI CHECKS PASSED")


asyncio.run(main())
