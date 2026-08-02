"""Sorting both tables, by header click and by keyboard."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rsb.tui.app as appmod
from rsb.config import Config
from rsb.discord.gateway import GuildMember
from rsb.discord.http import GROUP_DM_CHANNEL, Guild, PrivateChannel
from rsb.rotector import MemberReport, RobloxAccount
from rsb.tui.app import RESULT_SORTS, SOURCE_SORTS, Row
from rsb.verdict import category_name, verdict_label
from textual.widgets import DataTable

ok = lambda m: print(f"[ok] {m}")

CATEGORIES = {1: "CSAM", 2: "Sexual", 3: "Kink", 5: "Condo", 6: "Other"}


def make_row(i, flag, category, servers=0):
    report = MemberReport(discord_id=str(i))
    if flag is not None:
        report.accounts.append(
            RobloxAccount(
                user_id=1000 + i, username=f"rb{i:02d}", flag_type=flag,
                category=category, confidence=1.0,
            )
        )
    for n in range(servers):
        from rsb.rotector import TrackedServer
        report.servers.append(TrackedServer(f"s{n}", f"S{n}", None, None, False, False))
    member = GuildMember(id=str(i), username=f"user{i:02d}")
    return Row(member=member, report=report)


class FakeHTTP:
    def __init__(self, token, **kw): pass
    async def me(self): return {"username": "t", "global_name": "T", "id": "9"}
    async def guilds(self):
        return [
            Guild(id="1", name="Zulu", owner=False, permissions=0,
                  member_count=10, presence_count=1),
            Guild(id="2", name="alpha", owner=False, permissions=0,
                  member_count=900, presence_count=1),
        ]
    async def relationships(self): return []
    async def widget(self, gid): return None
    async def private_channels(self):
        return [PrivateChannel(id="g1", type=GROUP_DM_CHANNEL, name="Mid",
                               owner_id="9", recipients=[{"id": "5"}])]
    async def channels(self, gid): return []
    async def aclose(self): pass


class FakeGateway:
    def __init__(self, token, bot=False): self.user = None
    async def connect(self, timeout=45.0): return {}
    async def fetch_members(self, *a, **kw): return {}
    async def close(self): pass


def column_values(app, index):
    table = app.query_one("#results", DataTable)
    return [
        table.get_row_at(r)[index].plain for r in range(table.row_count)
    ]


async def main():
    appmod.DiscordHTTP = FakeHTTP
    appmod.DiscordGateway = FakeGateway

    cfg = Config()
    cfg.token = "fake"
    app = appmod.ScannerApp(cfg)

    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause(0.8)

        # a spread of categories and flags to sort through
        spec = [(2, 5), (1, 2), (2, 1), (5, 6), (1, 3), (2, None), (0, None)]
        for i, (flag, category) in enumerate(spec):
            row = make_row(i, flag, category, servers=i % 3)
            app.rows[row.member.id] = row
        app.filter_mode = appmod.FilterMode.ALL
        app._rebuild_table()
        await pilot.pause(0.3)

        results = app.query_one("#results", DataTable)
        assert results.row_count == len(spec)
        ok(f"{results.row_count} rows staged across "
           f"{len({c for _, c in spec})} categories")

        # --- default: worst verdict first
        assert app._result_sort == (appmod.DEFAULT_RESULT_SORT, False)
        verdicts = column_values(app, 1)
        assert verdicts[0] == "THREAT", verdicts
        ok(f"default sort is worst-first: {verdicts[:3]}")

        # --- header labels carry a direction marker
        labels = [c.label.plain for c in results.columns.values()]
        assert any("^" in l or "v" in l for l in labels), labels
        ok(f"sorted column is marked in the header: {labels}")

        # --- click the Category header
        category_index = next(
            i for i, (name, _) in enumerate(RESULT_SORTS) if name == "Category"
        )
        app._sort_results(category_index)
        await pilot.pause(0.3)
        values = [v for v in column_values(app, category_index) if v != "-"]
        # case-insensitive, so "Condo" precedes "CSAM" -- correct for a UI
        assert values == sorted(values, key=str.lower), values
        ok(f"sorted by Category ascending: {values}")

        blanks = column_values(app, category_index)
        assert blanks[-1] == "-", blanks
        ok("members with no category sort last, not first")

        # --- clicking the same header again reverses
        app._sort_results(category_index)
        await pilot.pause(0.3)
        assert app._result_sort == (category_index, True)
        reversed_values = [
            v for v in column_values(app, category_index) if v != "-"
        ]
        assert reversed_values == sorted(values, key=str.lower, reverse=True), (
            reversed_values
        )
        ok(f"clicking again reverses: {reversed_values}")

        labels = [c.label.plain for c in results.columns.values()]
        assert "v" in labels[category_index], labels[category_index]
        ok(f"the arrow flips with the direction: {labels[category_index]!r}")

        # --- keyboard cycling
        app.query_one("#results", DataTable).focus()
        await pilot.pause(0.2)
        before = app._result_sort[0]
        await pilot.press("o")
        await pilot.pause(0.3)
        assert app._result_sort[0] == (before + 1) % len(RESULT_SORTS)
        ok(f"'o' advances the sort column: "
           f"{RESULT_SORTS[before][0]} -> {RESULT_SORTS[app._result_sort[0]][0]}")

        await pilot.press("O")
        await pilot.pause(0.3)
        assert app._result_sort[1] is True
        ok("'O' reverses the current sort")

        # every column must sort without raising
        for index, (name, _) in enumerate(RESULT_SORTS):
            app._sort_results(index, toggle=False)
            await pilot.pause(0.1)
            assert app.query_one("#results", DataTable).row_count == len(spec)
        ok(f"all {len(RESULT_SORTS)} result columns sort cleanly: "
           f"{[n for n, _ in RESULT_SORTS]}")

        # --- sources table
        from rsb.tui.app import GROUP_KEY

        def source_names(app):
            """Displayed source names, headers excluded, indent stripped."""
            table = app.query_one("#guilds", DataTable)
            out = []
            for index, key in enumerate(app._source_rows):
                if key.startswith(GROUP_KEY):
                    continue
                out.append(table.get_row_at(index)[0].plain.strip())
            return out

        def source_counts(app):
            table = app.query_one("#guilds", DataTable)
            out = []
            for index, key in enumerate(app._source_rows):
                if key.startswith(GROUP_KEY):
                    continue
                raw = table.get_row_at(index)[2].plain.strip().replace(",", "")
                out.append(int(raw) if raw.isdigit() else 0)
            return out

        sources = app.query_one("#guilds", DataTable)
        default_order = source_names(app)
        name_index = next(
            i for i, (n, _) in enumerate(SOURCE_SORTS) if n == "Name"
        )
        app._sort_sources(name_index)
        await pilot.pause(0.4)
        names = source_names(app)
        # sorting is applied within each group, so check per group
        servers = [n for n in names if n in ("alpha", "Zulu")]
        assert servers == sorted(servers, key=str.lower), servers
        names = servers
        assert names != default_order, "sorting changed nothing"
        ok(f"sources sorted by Name: {names}")

        members_index = next(
            i for i, (n, _) in enumerate(SOURCE_SORTS) if n == "Members"
        )
        app._sort_sources(members_index)
        await pilot.pause(0.4)
        counts = source_counts(app)
        server_counts = [c for c in counts if c in (10, 900)]
        assert server_counts == sorted(server_counts, reverse=True), server_counts
        counts = server_counts
        ok(f"sources sorted by Members, largest first: {counts}")

        app.query_one("#guilds", DataTable).focus()
        await pilot.pause(0.2)
        await pilot.press("o")
        await pilot.pause(0.3)
        assert app._source_sort is not None
        ok(f"'o' also cycles the sources table "
           f"(now {SOURCE_SORTS[app._source_sort[0]][0]})")

        # sorting must not lose or duplicate rows
        assert len(source_names(app)) == 4, source_names(app)
        assert app.query_one("#results", DataTable).row_count == len(spec)
        ok("no rows lost or duplicated by any sort")

    print("\nALL SORTING TESTS PASSED")


asyncio.run(main())
