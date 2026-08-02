"""Settings editor, setup wizard, diagnostics, paging, and the command palette."""
import asyncio
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rsb.tui.app as appmod
from rsb.config import Config
from rsb.discord.gateway import GuildMember
from rsb.discord.http import Channel, Guild
from rsb.migrate import SCHEMA
from rsb.rotector import MemberReport
from rsb.tui.app import GROUP_KEY, PAGE_SIZE, Row
from rsb.tui.commands import BindingCommands, _unpack
from rsb.tui.settings import (
    DiagnosticsScreen,
    SettingsScreen,
    SetupWizard,
    _coerce,
    _widget_id,
    run_checks,
)
from textual.widgets import Button, Checkbox, DataTable, Input

ok = lambda m: print(f"[ok] {m}")

# Two dots so run_checks accepts the shape, but nothing like a real
# token. A convincing fake is as bad as the real thing here: secret
# scanners cannot tell them apart, and a blocked push is a worse
# outcome than an ugly fixture.
GOOD_TOKEN = "fake.test.token"


class FakeHTTP:
    def __init__(self, token, **kw): pass
    async def me(self): return {"username": "t", "global_name": "T", "id": "9"}
    async def guilds(self):
        return [Guild(id="111", name="Srv", owner=False, permissions=0,
                      member_count=5, presence_count=1)]
    async def relationships(self): return []
    async def widget(self, gid): return None
    async def private_channels(self): return []
    async def channels(self, gid):
        return [Channel(id="c1", name="general", type=0, position=0,
                        everyone_can_view=True)]
    async def aclose(self): pass


class FakeGateway:
    def __init__(self, token, bot=False): self.user = None
    async def connect(self, timeout=45.0): return {}
    async def fetch_members(self, *a, **kw): return {}
    async def close(self): pass


def test_coercion():
    assert _coerce("true", False) is True and _coerce("no", True) is False
    assert _coerce("42", 0) == 42 and _coerce("nonsense", 7) == 7
    assert _coerce("1.5", 0.0) == 1.5
    assert _coerce("csv, txt", []) == ["csv", "txt"]
    assert _coerce("  ", []) == []
    assert _coerce("hello", "") == "hello"
    ok("field values coerce back to the type their default implies")


def test_checks():
    cfg = Config()
    failing = [c for c in run_checks(cfg) if not c.ok]
    assert any(c.name == "Discord token" for c in failing)
    assert all(c.fix for c in failing), "a failing check with no remedy"
    ok(f"a blank config fails {len(failing)} check(s), each with a fix")

    cfg.token = GOOD_TOKEN
    assert not [c for c in run_checks(cfg) if not c.ok]
    ok("a valid config passes every check")

    cfg.token = "not-a-token"
    assert any(c.name == "Token shape" and not c.ok for c in run_checks(cfg))
    ok("a bot-shaped token is called out specifically")

    cfg.token = GOOD_TOKEN
    cfg.rotector.reserve = 999
    assert any(c.name == "Rate limit" and not c.ok for c in run_checks(cfg))
    cfg.rotector.reserve = 5
    cfg.proxy.enabled = True
    # point at a path that does not exist, so a real proxies.txt in the working
    # directory cannot make this pass by accident
    cfg.proxy.file = str(Path(tempfile.mkdtemp()) / "none.txt")
    assert any(c.name == "Proxies" and not c.ok for c in run_checks(cfg))
    ok("nonsensical rate limits and empty proxy lists are both caught")


async def test_wizard_and_settings():
    appmod.DiscordHTTP = FakeHTTP
    appmod.DiscordGateway = FakeGateway

    workdir = Path(tempfile.mkdtemp())
    cfg = Config()
    cfg.source = workdir / "config.toml"
    app = appmod.ScannerApp(cfg)

    async with app.run_test(size=(150, 46)) as pilot:
        # --- no token: the wizard must appear before anything else
        for _ in range(40):
            await pilot.pause(0.1)
            if isinstance(app.screen, SetupWizard):
                break
        assert isinstance(app.screen, SetupWizard), type(app.screen)
        ok("a config with no token opens the setup wizard")

        wizard = app.screen
        wizard.query_one("#wiz-token", Input).value = GOOD_TOKEN
        wizard.query_one("#wiz-key", Input).value = "testkey"
        wizard.query_one("#confirm" if False else "#save", Button).press()
        for _ in range(40):
            await pilot.pause(0.1)
            if not isinstance(app.screen, SetupWizard):
                break

        saved = tomllib.loads((workdir / "config.toml").read_text())
        assert saved["discord"]["token"] == GOOD_TOKEN
        assert saved["rotector"]["api_key"] == "testkey"
        assert saved["scan"]["hide_no_detections"] is True
        ok("the wizard writes a working config.toml")
        assert app.config.token == GOOD_TOKEN
        ok("and the running app picks it up without a restart")

        for _ in range(50):
            await pilot.pause(0.1)
            if app.sources:
                break
        assert app.sources, "did not continue to connect after setup"
        ok("startup continues straight into the app")

        # --- settings editor
        await pilot.press("ctrl+s")
        for _ in range(40):
            await pilot.pause(0.1)
            if isinstance(app.screen, SettingsScreen):
                break
        assert isinstance(app.screen, SettingsScreen)
        ok("ctrl+s opens the settings editor")

        screen = app.screen
        missing = []
        for section in SCHEMA:
            for setting in section.settings:
                try:
                    screen.query_one(f"#{_widget_id(section.name, setting.name)}")
                except Exception:
                    missing.append(f"{section.name}.{setting.name}")
        assert not missing, missing
        total = sum(len(s.settings) for s in SCHEMA)
        ok(f"every one of the {total} schema settings has a field")

        assert isinstance(
            screen.query_one(f"#{_widget_id('scan', 'skip_bots')}"), Checkbox
        )
        assert isinstance(
            screen.query_one(f"#{_widget_id('rotector', 'rate_limit')}"), Input
        )
        ok("booleans render as checkboxes, everything else as text fields")

        screen.query_one(f"#{_widget_id('rotector', 'rate_limit')}").value = "75"
        screen.query_one(f"#{_widget_id('scan', 'skip_bots')}").value = False
        screen.query_one(f"#{_widget_id('export', 'formats')}").value = "csv, json"
        screen.query_one("#save", Button).press()
        for _ in range(40):
            await pilot.pause(0.1)
            if not isinstance(app.screen, SettingsScreen):
                break

        saved = tomllib.loads((workdir / "config.toml").read_text())
        assert saved["rotector"]["rate_limit"] == 75
        assert saved["scan"]["skip_bots"] is False
        assert saved["export"]["formats"] == ["csv", "json"]
        ok("edits are written back with the right types")
        assert app.config.rotector.rate_limit == 75
        assert app.config.export.formats == ["csv", "json"]
        ok("and applied to the live config")

        assert saved["discord"]["token"] == GOOD_TOKEN
        ok("the token survives a save it was not part of")

    print()


async def test_paging_and_palette():
    appmod.DiscordHTTP = FakeHTTP
    appmod.DiscordGateway = FakeGateway

    cfg = Config()
    cfg.token = GOOD_TOKEN
    app = appmod.ScannerApp(cfg)

    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause(0.8)

        total = PAGE_SIZE * 2 + 40
        for i in range(total):
            member = GuildMember(id=str(i), username=f"user{i:05d}")
            app.rows[member.id] = Row(
                member=member,
                report=MemberReport(discord_id=member.id),
                checked=False,
            )
        app.filter_mode = appmod.FilterMode.ALL
        app._page = 0
        app._rebuild_table()
        await pilot.pause(0.3)

        table = app.query_one("#results", DataTable)
        assert table.row_count == PAGE_SIZE, table.row_count
        assert app.page_count == 3, app.page_count
        ok(f"{total:,} rows render {PAGE_SIZE} at a time across "
           f"{app.page_count} pages")

        first = table.get_row_at(0)[0].plain
        await pilot.press("n")
        await pilot.pause(0.3)
        assert app._page == 1
        assert app.query_one("#results", DataTable).get_row_at(0)[0].plain != first
        ok("'n' advances a page")

        await pilot.press("n")
        await pilot.pause(0.3)
        assert app._page == 2
        assert app.query_one("#results", DataTable).row_count == 40, "last page size"
        ok(f"the final page holds the remainder ({40} rows)")

        await pilot.press("n")
        await pilot.pause(0.3)
        assert app._page == 2 and "last page" in app._status_text
        ok("paging past the end says so instead of blanking the table")

        await pilot.press("N")
        await pilot.pause(0.3)
        assert app._page == 1
        ok("'N' goes back")

        # searching re-pages from the top and narrows the count
        app.query_one("#results", DataTable).focus()
        await pilot.pause(0.2)
        await pilot.press("slash")
        await pilot.pause(0.2)
        for ch in "user0000":
            await pilot.press(ch)
        await pilot.pause(0.5)
        assert app._page == 0, "search did not reset to the first page"
        assert 0 < app._matching < total, app._matching
        assert app.query_one("#results", DataTable).row_count == app._matching
        ok(f"search narrows to {app._matching} rows on one page, from page 1")

        await pilot.press("escape")
        await pilot.pause(0.4)
        assert app._matching == total
        ok(f"clearing the search restores all {total:,} rows")

        # --- command palette carries the same bindings
        described = [b for b in appmod.ScannerApp.BINDINGS if _unpack(b)[2]]
        provider = BindingCommands(app.screen)
        found = provider._commands()
        assert len(found) == len(described), (len(found), len(described))
        ok(f"the palette exposes all {len(found)} described bindings")

        names = {d for d, _, _ in found}
        for expected in ("Settings", "Export", "Next page", "List members only"):
            assert expected in names, expected
        ok("including the ones a narrow footer would clip")

        actions = {a for _, a, _ in found}
        for action in actions:
            base = action.split("(")[0]
            assert hasattr(app, f"action_{base}"), f"palette lists a dead action: {action}"
        ok(f"every palette command maps to a real action ({len(actions)} actions)")

    print()


test_coercion()
test_checks()
print()


async def test_scrollable_footer():
    """The bar must be visible, and the arrows must actually move it."""
    from rsb.tui.commands import ScrollableFooter, ScrollArrow, StripContent

    appmod.DiscordHTTP = FakeHTTP
    appmod.DiscordGateway = FakeGateway
    cfg = Config()
    cfg.token = GOOD_TOKEN
    app = appmod.ScannerApp(cfg)

    # narrow on purpose: this is the case that used to clip, then vanish
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause(1.0)
        footer = app.query_one(ScrollableFooter)
        bar = app.query_one("#keys-content", StripContent)
        strip = footer.view

        assert footer.size.height == 1, footer.size
        assert bar.size.height == 1, bar.size
        assert strip.size.width > 0, strip.size
        ok(f"the key strip is {strip.size.width} cells of visible row, not "
           f"swallowed by a scrollbar")

        content = bar.size.width
        assert content > strip.size.width, (content, strip.size.width)
        ok(f"key bar is {content} cells wide in a {strip.size.width}-cell strip")

        left = app.query_one("#keys-left")
        right = app.query_one("#keys-right")
        assert left.size.width == 3 and right.size.width == 3
        ok("both arrows are rendered at either end")

        assert left.has_class("exhausted"), "left arrow lit up at the start"
        assert not right.has_class("exhausted"), "right arrow dimmed with more to see"
        ok("at the start, only the right arrow is lit")

        before = footer.scrolled_to
        footer.nudge(1)
        await pilot.pause(0.3)
        after = footer.scrolled_to
        assert after > before, (before, after)
        ok(f"the right arrow scrolls the bar ({before} -> {after})")

        for _ in range(40):
            footer.nudge(1)
        await pilot.pause(0.3)
        assert footer.scrolled_to == int(strip.max_scroll_x), (
            footer.scrolled_to, strip.max_scroll_x
        )
        assert app.query_one("#keys-right").has_class("exhausted")
        ok("at the far end the right arrow dims, so you know it is the end")

        footer.nudge(-1)
        await pilot.pause(0.3)
        assert not app.query_one("#keys-right").has_class("exhausted")
        ok("and lights again once there is more to the right")

        content_text = getattr(bar, "_Static__content", None)
        plain = content_text.plain if hasattr(content_text, "plain") else ""
        assert "|" in plain, "no separators between actions"
        for _keys, _action, description in [
            (k, a, d) for k, a, d in
            [_unpack(b) for b in appmod.ScannerApp.BINDINGS] if d
        ]:
            assert description in plain, description
        ok(f"all {len(plain.split('|'))} actions present, separated by |, however "
           f"narrow the window")

    print("\nALL SETTINGS/PAGING TESTS PASSED")

asyncio.run(test_wizard_and_settings())
asyncio.run(test_paging_and_palette())
asyncio.run(test_scrollable_footer())
