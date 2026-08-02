"""Listing members without checking them, checking one at a time, collapsible
source groups, and the live incoming-message watcher.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rsb.tui.app as appmod
from rsb.config import Config
from rsb.discord.gateway import DiscordGateway, GuildMember
from rsb.discord.http import GROUP_DM_CHANNEL, Channel, Guild, PrivateChannel
from rsb.sources import GROUPS, KIND_INBOX, group_for
from rsb.tui.app import GROUP_KEY
from rsb.verdict import Verdict
from textual.widgets import DataTable

ok = lambda m: print(f"[ok] {m}")

# "1" is Confirmed in Rotector; the padding ids are not tracked
MEMBERS = {
    "1": GuildMember(id="1", username="flagged"),
    **{
        str(900_000_000_000_000_000 + n): GuildMember(
            id=str(900_000_000_000_000_000 + n), username=f"u{n}"
        )
        for n in range(9)
    },
}


class FakeHTTP:
    def __init__(self, token, **kw): pass
    async def me(self): return {"username": "t", "global_name": "T", "id": "9"}
    async def guilds(self):
        return [Guild(id="111", name="Srv", owner=False, permissions=0,
                      member_count=len(MEMBERS), presence_count=3)]
    async def relationships(self): return []
    async def widget(self, gid): return None
    async def private_channels(self):
        return [PrivateChannel(id="g1", type=GROUP_DM_CHANNEL, name="Squad",
                               owner_id="9", recipients=[{"id": "1"}])]
    async def channels(self, gid):
        return [Channel(id="c1", name="general", type=0, position=0,
                        everyone_can_view=True)]
    async def aclose(self): pass


class FakeGateway:
    def __init__(self, token, bot=False):
        self.user = None
        self.watch_calls = 0
        self.authors = []

    async def connect(self, timeout=45.0): return {}

    async def fetch_members(self, gid, channels, expected=None, on_progress=None,
                            on_members=None, **kwargs):
        if on_members:
            on_members(list(MEMBERS.values()))
        return dict(MEMBERS)

    async def watch_messages(self, on_author, should_stop, own_id=None,
                             include_bots=False, poll=0.1):
        self.watch_calls += 1
        for member in self.authors:
            if should_stop():
                break
            on_author(member)
            await asyncio.sleep(0.15)
        while not should_stop():
            await asyncio.sleep(0.05)
        return len(self.authors)

    async def close(self): pass


def select_source(app, kind):
    table = app.query_one("#guilds", DataTable)
    key = next(k for k in app._source_rows if k.startswith(f"{kind}:"))
    table.move_cursor(row=app._source_rows.index(key))
    return next(s for s in app.sources if f"{s.kind}:{s.id}" == key)


def visible_keys(app):
    return [k for k in app._source_rows if not k.startswith(GROUP_KEY)]


async def main():
    appmod.DiscordHTTP = FakeHTTP
    appmod.DiscordGateway = FakeGateway

    cfg = Config()
    cfg.token = "fake"
    app = appmod.ScannerApp(cfg)

    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause(0.8)
        table = app.query_one("#guilds", DataTable)

        # --- grouping and collapse
        headers = [k[len(GROUP_KEY):] for k in app._source_rows
                   if k.startswith(GROUP_KEY)]
        assert headers == [t for t, _ in GROUPS if any(
            group_for(s.kind) == t for s in app.sources
        )], headers
        ok(f"sources grouped under: {headers}")

        assert not app._source_rows[0].startswith(GROUP_KEY) or table.cursor_row > 0
        assert not app._source_rows[table.cursor_row].startswith(GROUP_KEY)
        ok("the cursor starts on a real source, not a group header")

        before = len(visible_keys(app))
        header_row = next(i for i, k in enumerate(app._source_rows)
                          if k.endswith("Servers"))
        table.move_cursor(row=header_row)
        await pilot.pause(0.2)
        await pilot.press("s")
        await pilot.pause(0.4)
        assert "Servers" in app._collapsed, app._collapsed
        after = len(visible_keys(app))
        assert after < before, (before, after)
        ok(f"collapsing 'Servers' hides its sources ({before} -> {after} visible)")
        assert any(k.endswith("Servers") for k in app._source_rows)
        ok("the group header itself stays, so it can be reopened")

        header_row = next(i for i, k in enumerate(app._source_rows)
                          if k.endswith("Servers"))
        table.move_cursor(row=header_row)
        await pilot.pause(0.2)
        await pilot.press("s")
        await pilot.pause(0.4)
        assert "Servers" not in app._collapsed
        assert len(visible_keys(app)) == before
        ok("expanding restores them")

        # a group DM must be selectable -- its key also starts with "group"
        group_source = select_source(app, "group")
        await pilot.pause(0.2)
        assert app._selected_source() is not None
        assert app._selected_source().id == group_source.id
        ok(f"group DM {group_source.name!r} is selectable, not mistaken for a header")

        # --- list members only
        select_source(app, "guild")
        await pilot.pause(0.3)
        await pilot.press("m")
        for _ in range(60):
            await pilot.pause(0.15)
            if app.rows and not app._activity:
                break

        assert len(app.rows) == len(MEMBERS), f"{len(app.rows)} of {len(MEMBERS)}"
        assert all(not r.checked for r in app.rows.values())
        ok(f"listed all {len(app.rows)} members without checking any")

        assert "Listed" in app._status_text and "nothing looked up" in app._status_text
        ok(f"status is explicit: {app._status_text[:60]}...")

        results = app.query_one("#results", DataTable)
        assert results.row_count == len(MEMBERS), results.row_count
        ok("unchecked members are all listed, not hidden by the findings filter")
        assert results.get_row_at(0)[1].plain == "not checked"
        ok("their verdict column reads 'not checked' rather than a verdict")

        # threat-only filters exclude unchecked members, since there is no verdict
        app.filter_mode = appmod.FilterMode.THREATS
        app._rebuild_table()
        await pilot.pause(0.2)
        assert app.query_one("#results", DataTable).row_count == 0
        ok("'Threats only' excludes unchecked members rather than guessing")
        app.filter_mode = appmod.FilterMode.ALL
        app._rebuild_table()
        await pilot.pause(0.2)

        # --- check a single member
        flagged_index = app._shown.index("1")
        app.query_one("#results", DataTable).move_cursor(row=flagged_index)
        await pilot.pause(0.2)
        await pilot.press("S")
        for _ in range(60):
            await pilot.pause(0.2)
            if app.rows["1"].checked:
                break

        assert app.rows["1"].checked, "single-member scan did not complete"
        assert app.rows["1"].report.verdict is Verdict.THREAT
        ok(f"scanning one member returned a real verdict: {app._status_text[:60]}...")

        others = [r for k, r in app.rows.items() if k != "1"]
        assert all(not r.checked for r in others)
        ok(f"the other {len(others)} members were left unchecked")

        # --- live inbox
        gateway = app.gateway
        gateway.authors = [
            GuildMember(id="1", username="flagged"),
            GuildMember(id="900000000000000099", username="stranger"),
        ]
        select_source(app, KIND_INBOX)
        await pilot.pause(0.3)
        app.rows.clear()
        await pilot.press("s")
        for _ in range(80):
            await pilot.pause(0.15)
            if len(app.rows) >= 2:
                break

        assert gateway.watch_calls == 1, gateway.watch_calls
        assert len(app.rows) == 2, len(app.rows)
        ok(f"the inbox watcher checked {len(app.rows)} senders as they arrived")

        assert app.rows["1"].report.verdict is Verdict.THREAT
        ok("a flagged sender is surfaced immediately")

        # the watcher label alternates with lookup progress, so sample a few
        seen_watch_label = False
        for _ in range(20):
            await pilot.pause(0.1)
            if app._activity and "x to stop" in app._activity:
                seen_watch_label = True
                break
        assert seen_watch_label, f"never said how to stop: {app._activity!r}"
        ok("it says how to stop while watching")

        await pilot.press("x")
        await pilot.pause(0.5)
        assert app._activity is None, app._activity
        ok("'x' stops the live watcher")




async def test_collapse_ux():
    """One click to fold, and the cursor stays where it was."""
    import rsb.tui.app as m
    from rsb.tui.app import GROUP_KEY, SourceTable
    from rsb.tui.commands import ScrollingStrip, StatusStrip

    m.DiscordHTTP = FakeHTTP
    m.DiscordGateway = FakeGateway
    cfg = Config()
    # shaped for run_checks, deliberately not token-like (see test_units)
    cfg.token = "fake.test.token"
    app = m.ScannerApp(cfg)

    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause(0.9)
        table = app.query_one("#guilds", SourceTable)

        start_key = app._source_rows[table.cursor_row]
        assert not start_key.startswith(GROUP_KEY)
        ok(f"opens with the cursor on a source ({start_key})")

        # --- a single click folds the group
        header_row = next(i for i, k in enumerate(app._source_rows)
                          if k.endswith("Servers"))
        table.move_cursor(row=0)
        await pilot.pause(0.2)
        assert "Servers" not in app._collapsed

        table.hover_coordinate = m.Coordinate(header_row, 0)
        table.post_message(SourceTable.GroupClicked("Servers"))
        await pilot.pause(0.5)
        assert "Servers" in app._collapsed, app._collapsed
        ok("one click folds the group -- no second click to focus first")

        # --- and the cursor is left on that header, not thrown to the top
        cursor_key = app._source_rows[table.cursor_row]
        assert cursor_key == f"{GROUP_KEY}Servers", cursor_key
        ok(f"the cursor stays on the group that was folded ({cursor_key})")

        table.post_message(SourceTable.GroupClicked("Servers"))
        await pilot.pause(0.5)
        assert "Servers" not in app._collapsed
        assert app._source_rows[table.cursor_row] == f"{GROUP_KEY}Servers"
        ok("unfolding keeps it there too")

        # --- folding the group the cursor is inside falls back to its header
        source_row = next(i for i, k in enumerate(app._source_rows)
                          if k.startswith("guild:"))
        table.move_cursor(row=source_row)
        await pilot.pause(0.2)
        app._toggle_group("Servers")
        await pilot.pause(0.5)
        cursor_key = app._source_rows[table.cursor_row]
        assert cursor_key == f"{GROUP_KEY}Servers", cursor_key
        ok("folding the group you were inside leaves you on its header")
        app._toggle_group("Servers")
        await pilot.pause(0.4)

        # --- the status line scrolls rather than clipping
        strip = app.query_one(StatusStrip)
        app._set_status("short")
        await pilot.pause(0.3)
        assert strip.view.max_scroll_x == 0
        left = app.query_one("#status-left")
        assert left.has_class("hidden"), "arrows shown for text that fits"
        ok("a short status shows no arrows")

        app._set_status("x" * 400)
        await pilot.pause(0.3)
        assert strip.view.max_scroll_x > 0, strip.view.max_scroll_x
        assert not app.query_one("#status-left").has_class("hidden")
        ok(f"a long status becomes scrollable ({strip.view.max_scroll_x} cells) "
           f"and the arrows appear")

        before = strip.scrolled_to
        strip.nudge(1)
        await pilot.pause(0.3)
        assert strip.scrolled_to > before
        ok(f"its arrows scroll it ({before} -> {strip.scrolled_to})")

        content = app.query_one("#status-content")
        text = getattr(content, "_Static__content", None)
        assert text is not None and len(text.plain) > 300
        ok("the full message is present, not truncated to fit")

    print("\nALL LISTING/INBOX TESTS PASSED")

asyncio.run(main())
asyncio.run(test_collapse_ux())
