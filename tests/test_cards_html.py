"""Profile cards, HTML export, asset folders, and the collapsible panes."""
import asyncio
import html as htmllib
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

import rsb.tui.app as appmod
from rsb.config import Config
from rsb.discord.gateway import GuildMember
from rsb.discord.http import Channel, Guild
from rsb.export import DEFAULT_COLUMNS, ExportRow, export
from rsb.htmlrender import VERDICT_CSS, render_html
from rsb.imagerender import CARD_W, VERDICT_COLOURS, render_card, render_cards
from rsb.profiles import DEFAULT_AVATAR_COLOURS, Profile, _snowflake_date, fetch_profiles
from rsb.rotector import MemberReport, RobloxAccount, TrackedServer
from rsb.tui.dialogs import ExportDialog
from rsb.verdict import Verdict
from textual.widgets import Button, DataTable, RadioButton, RichLog, Static

ok = lambda m: print(f"[ok] {m}")


def image_bytes(size, colour):
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, "PNG")
    return buffer.getvalue()


def make_row(index=0, flag=2, reason="Condo Activity", servers=1):
    uid = str(288471029384756000 + index)
    report = MemberReport(discord_id=uid)
    report.accounts.append(
        RobloxAccount(
            user_id=8423569713 + index, username=f"user{index}_rblx",
            flag_type=flag, category=5, confidence=1.0,
            reasons={reason: {"message": "[Trap3] entered a condo game",
                              "evidence": ["Game: furre"]}} if reason else {},
        )
    )
    for n in range(servers):
        report.servers.append(
            TrackedServer(f"s{n}", "condo hub", None, None, True, False)
        )
    return ExportRow(uid, f"user{index}", f"User {index}", report)


def make_profile(row, with_images=True):
    return Profile(
        user_id=row.discord_id,
        username=row.username,
        global_name=row.display_name,
        avatar_bytes=image_bytes((160, 160), (91, 124, 250)) if with_images else None,
        banner_bytes=image_bytes((600, 240), (40, 44, 70)) if with_images else None,
        created_at="2018-05-11",
        bio="a short bio",
    )


# --------------------------------------------------------------------------
# profiles
# --------------------------------------------------------------------------

def test_profile_helpers():
    # Discord ids encode their creation time
    assert _snowflake_date("80351110224678912").startswith("2015-")
    assert _snowflake_date("not-an-id") == ""
    ok(f"account age is read from the id itself "
       f"({_snowflake_date('80351110224678912')})")

    profile = Profile(user_id="288471029384756000")
    assert profile.fallback_colour in DEFAULT_AVATAR_COLOURS
    assert Profile(user_id="junk").fallback_colour in DEFAULT_AVATAR_COLOURS
    ok("a missing avatar falls back to Discord's own default palette")

    assert Profile(user_id="1", global_name="Nice").display_name == "Nice"
    assert Profile(user_id="1", username="raw").display_name == "raw"
    assert Profile(user_id="7").display_name == "7"
    ok("display name falls back sensibly when fields are missing")


async def test_profile_fetch_tolerates_failure():
    class BrokenHTTP:
        async def user(self, user_id, guild_id=None):
            raise RuntimeError("no route to host")

    got = await fetch_profiles(BrokenHTTP(), ["1", "2"], with_images=False)
    assert set(got) == {"1", "2"}
    assert all(p.errors for p in got.values())
    assert all(p.created_at for p in got.values())
    ok("a profile that cannot be fetched still yields a card-able record, "
       "with the failure recorded on it")

    assert await fetch_profiles(BrokenHTTP(), [], with_images=False) == {}
    ok("asking for nothing costs nothing")


# --------------------------------------------------------------------------
# cards
# --------------------------------------------------------------------------

def test_card_render():
    row = make_row(flag=2)
    card = render_card(row, make_profile(row))
    assert card.width == CARD_W and card.height > 300
    ok(f"a card renders {card.width}x{card.height}")

    colours = {c for _n, c in card.convert("RGB").getcolors(1 << 18)}
    assert VERDICT_COLOURS[Verdict.THREAT] in colours
    ok("the verdict ribbon carries the threat colour")

    clear = render_card(make_row(flag=0, reason=""), make_profile(make_row(flag=0)))
    clear_colours = {c for _n, c in clear.convert("RGB").getcolors(1 << 18)}
    assert VERDICT_COLOURS[Verdict.THREAT] not in clear_colours
    ok("and a clear member's card does not")

    # no avatar, no banner, no profile at all
    bare = render_card(make_row(), None)
    assert bare.width == CARD_W and bare.height > 200
    ok("a member with no profile data still renders, with initials")

    rich = render_card(
        make_row(servers=6), make_profile(make_row())
    )
    assert rich.height >= card.height
    ok("cards grow with what there is to say")


def test_cards_written_per_member():
    out = Path(tempfile.mkdtemp())
    rows = [make_row(i) for i in range(4)]
    profiles = {r.discord_id: make_profile(r) for r in rows}
    files = render_cards(rows, out, "stem", profiles)
    assert len(files) == 4, files
    assert all(f.suffix == ".png" for f in files)
    assert "user0" in files[0].name and files[0].name.startswith("stem.1-")
    ok(f"one card per member, named in order: {[f.name for f in files][:2]}")
    for path in files:
        assert Image.open(path).format == "PNG"
    ok("all four open as PNGs")


# --------------------------------------------------------------------------
# html
# --------------------------------------------------------------------------

def test_html_is_one_self_contained_page():
    out = Path(tempfile.mkdtemp())
    rows = [make_row(i) for i in range(6)]
    profiles = {r.discord_id: make_profile(r) for r in rows}
    files = render_html(
        rows, out, "page", DEFAULT_COLUMNS,
        guild_name="Roblox Trading Hub", stamp="20260802T120000Z",
        scope="filter: Threats only", profiles=profiles,
    )
    assert len(files) == 1, files
    ok(f"one file, never segmented: {files[0].name}")

    body = files[0].read_text(encoding="utf-8")
    assert body.startswith("<!doctype html>") and body.rstrip().endswith("</html>")
    ok("it is a complete document")

    assert body.count('<tr data-hay') == 6
    assert body.count('class="card"') == 6
    ok("both views are present: 6 table rows and 6 cards")

    import re

    assert "data:image/png;base64," in body
    remote = re.findall(r'src\s*=\s*[\'"](?!data:)([^\'"]+)', body)
    assert not remote, remote
    ok("every image is an inlined data URI; nothing is loaded remotely")

    assert "<style>" in body and "<script>" in body
    assert "<link" not in body and "src=\"http" not in body
    ok("CSS and JS are inline, so the file opens correctly on its own")

    # the only outbound links are the ones a reader is meant to follow
    links = set(re.findall(r'href\s*=\s*[\'"]([^\'"]+)', body))
    assert all("rotector.com" in l or "roblox.com" in l for l in links), links
    ok(f"the only links are the appeal and profile ones: {sorted(links)[:2]}")

    assert VERDICT_CSS[Verdict.THREAT] in body
    ok("verdict colours match the terminal and the PNG")

    assert "rotector.com" in body and "24 hours" in body
    ok("attribution and the retention limit are carried into the page")


def test_html_escapes_hostile_content():
    out = Path(tempfile.mkdtemp())
    row = make_row(0, reason="<script>alert(1)</script>")
    row.report.accounts[0].username = "</td><script>bad()</script>"
    profile = make_profile(row, with_images=False)
    profile.bio = "<img src=x onerror=alert(2)>"
    body = render_html(
        [row], out, "x", DEFAULT_COLUMNS, profiles={row.discord_id: profile}
    )[0].read_text(encoding="utf-8")

    # what matters is that no raw tag from member data survives; the escaped
    # text may still read like an attack, and that is fine -- it is just text
    for raw in ("<script>alert(1)", "<img src=x", "</td><script>"):
        assert raw not in body, raw
    for escaped in ("&lt;script&gt;alert(1)", "&lt;img src=x"):
        assert escaped in body, escaped
    ok("member-supplied text is escaped rather than injected -- names, bios "
       "and reasons all come from strangers")

    # only the two <script>/<style> blocks we wrote are real tags
    assert body.count("<script>") == 1 and body.count("<style>") == 1
    ok("exactly one script and one style block, both ours")


# --------------------------------------------------------------------------
# folder layout
# --------------------------------------------------------------------------

def test_assets_get_their_own_folders():
    out = Path(tempfile.mkdtemp())
    rows = [make_row(i) for i in range(3)]
    profiles = {r.discord_id: make_profile(r) for r in rows}
    manifest = export(
        rows, guild_name="Srv", guild_id="1", base_directory=out,
        formats=["csv", "txt", "png", "html"], columns=DEFAULT_COLUMNS,
        stamp="20260802T120000Z", png_style="cards", profiles=profiles,
    )
    root = manifest.directory
    assert (root / "png").is_dir() and (root / "html").is_dir()
    ok("png/ and html/ are their own folders")

    loose = [f.name for f in root.iterdir() if f.is_file()]
    assert not any(n.endswith(".png") for n in loose), loose
    assert not any(n.endswith(".html") for n in loose), loose
    assert any(n.endswith(".csv") for n in loose)
    ok(f"nothing is left loose beside them: {sorted(loose)}")

    assert len(list((root / "png").glob("*.png"))) == 3
    assert len(list((root / "html").glob("*.html"))) == 1
    ok("3 cards and 1 page, in their places")

    readme = (root / "README.txt").read_text(encoding="utf-8")
    assert "png/" in readme and "html/" in readme
    ok("the README lists them by their real paths")


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

MEMBERS = {"1": GuildMember(id="1", username="flagged")}


class FakeHTTP:
    def __init__(self, token, **kw): pass
    async def me(self): return {"username": "t", "global_name": "T", "id": "9"}
    async def guilds(self):
        return [Guild(id="111", name="Srv", owner=False, permissions=0,
                      member_count=1, presence_count=1)]
    async def relationships(self): return []
    async def widget(self, gid): return None
    async def private_channels(self): return []
    async def channels(self, gid):
        return [Channel(id="c1", name="general", type=0, position=0,
                        everyone_can_view=True)]
    async def aclose(self): pass


class FakeGateway:
    def __init__(self, token): self.user = None
    async def connect(self, timeout=45.0): return {}
    async def fetch_members(self, *a, **kw): return {}
    async def close(self): pass


async def test_panes_and_log_and_click_away():
    appmod.DiscordHTTP = FakeHTTP
    appmod.DiscordGateway = FakeGateway
    cfg = Config()
    cfg.token = "fake.test.token"
    app = appmod.ScannerApp(cfg)

    async with app.run_test(size=(150, 44)) as pilot:
        await pilot.pause(0.9)

        # --- sources pane folds away
        pane = app.query_one("#servers-pane")
        wide = pane.size.width
        await pilot.press("ctrl+b")
        await pilot.pause(0.3)
        assert app._sources_hidden and pane.size.width < wide
        assert app.query_one("#guilds", DataTable).display is False
        ok(f"ctrl+b folds the sources pane away ({wide} -> {pane.size.width})")

        results = app.query_one("#results-pane")
        assert results.size.width > app.size.width - 12, (
            f"results pane is {results.size.width} of {app.size.width}; the "
            f"space the sources pane gave up was left blank"
        )
        ok(f"the results pane takes the space back ({results.size.width} of "
           f"{app.size.width} columns)")

        await pilot.press("ctrl+b")
        await pilot.pause(0.3)
        assert not app._sources_hidden and pane.size.width == wide
        ok("and unfolds it to the same width")

        # --- detail pane resizes and folds, like the sources pane
        detail = app.query_one("#detail")
        divider = app.query_one("#detail-divider")
        assert divider.size.height == 1
        ok("the detail pane has its own drag handle")

        normal = detail.size.height
        app.set_detail_height(normal + 8)
        await pilot.pause(0.3)
        assert detail.size.height > normal
        ok(f"dragging it resizes the pane ({normal} -> {detail.size.height})")

        # the pane has a top border, so its rendered size is one row under the
        # height it was set to; the clamp is on the intent
        app.set_detail_height(1)
        await pilot.pause(0.3)
        assert app._detail_height == appmod.DetailDivider.MIN_HEIGHT
        ok(f"clamped to a usable minimum ({app._detail_height}), not squashed away")

        app.set_detail_height(10_000)
        await pilot.pause(0.3)
        assert app._detail_height < app.size.height - 6, app._detail_height
        ok(f"and clamped so the results table survives ({app._detail_height} "
           f"of {app.size.height})")

        kept = app._detail_height
        await pilot.press("ctrl+d")
        await pilot.pause(0.3)
        assert app._detail_hidden and not detail.display
        ok("ctrl+d folds the detail pane away entirely")

        await pilot.press("ctrl+d")
        await pilot.pause(0.3)
        assert not app._detail_hidden and detail.display
        assert app._detail_height == kept
        ok(f"unfolding restores the height it was dragged to ({kept})")

        # --- debug log
        panel = app.query_one("#logpanel", RichLog)
        assert not panel.display
        app.log_debug("a thing happened", "net")
        app._set_status("something worth keeping")
        await pilot.press("ctrl+l")
        await pilot.pause(0.3)
        assert panel.display
        assert app.log_lines >= 2, app.log_lines
        ok(f"ctrl+l shows the debug log, holding {app.log_lines} line(s) "
           f"recorded before it was opened")

        app._set_status("a repeated line")
        before = app.log_lines
        app._set_status("a repeated line")
        assert app.log_lines == before, (before, app.log_lines)
        ok("the same message twice in a row is logged once")

        app._set_status("a different line")
        app._set_status("a repeated line")
        assert app.log_lines == before + 2
        ok("but a genuine recurrence still appears, rather than being swallowed")

        # --- clicking outside a dialog closes it
        app.rows["1"] = appmod.Row(
            member=GuildMember(id="1", username="x"),
            report=MemberReport(discord_id="1"),
        )
        app.current_source = app.sources[-1]
        app.run_export()
        for _ in range(40):
            await pilot.pause(0.1)
            if isinstance(app.screen, ExportDialog):
                break
        assert isinstance(app.screen, ExportDialog)
        panel_region = app.screen.query_one("#panel").region
        assert not panel_region.contains(2, 1), "test click point is inside the panel"
        await pilot.click(offset=(2, 1))
        for _ in range(40):
            await pilot.pause(0.1)
            if not isinstance(app.screen, ExportDialog):
                break
        assert not isinstance(app.screen, ExportDialog), "clicking away did nothing"
        ok("clicking outside a dialog closes it")


async def main():
    test_profile_helpers()
    await test_profile_fetch_tolerates_failure()
    print()
    test_card_render()
    test_cards_written_per_member()
    print()
    test_html_is_one_self_contained_page()
    test_html_escapes_hostile_content()
    print()
    test_assets_get_their_own_folders()
    test_png_style_both()
    await test_avatar_survives_a_refused_profile()
    print()
    await test_panes_and_log_and_click_away()
    await test_export_dialog_buttons_stay_reachable()
    print()
    test_super_properties_shared()
    await test_popout_profile_is_parsed()




async def test_avatar_survives_a_refused_profile():
    """A 403 on the profile route must not cost the avatar."""
    from rsb.discord.http import DiscordForbidden

    class Refusing:
        calls = 0

        async def user(self, user_id, guild_id=None):
            Refusing.calls += 1
            raise DiscordForbidden("403 Forbidden")

    async def run():
        seeded = await fetch_profiles(
            Refusing(), ["288471029384756000"], with_images=False,
            seed_avatars={"288471029384756000": "abc123"},
        )
        bare = await fetch_profiles(Refusing(), ["1"], with_images=False)
        return seeded["288471029384756000"], bare["1"]

    with_seed, without = await run()

    assert with_seed.avatar_url and "abc123" in with_seed.avatar_url
    ok(f"a refused profile still yields an avatar from the member list: "
       f"...{with_seed.avatar_url[-28:]}")
    assert with_seed.errors == ["banner unavailable"], with_seed.errors
    ok("and says only the banner is missing, rather than crying failure")

    assert without.avatar_url is None
    assert any("DiscordForbidden" in e for e in without.errors)
    ok("with nothing to fall back on, the refusal is reported plainly")

    # the hash really is in the member payload we already receive
    member = GuildMember.parse(
        {"user": {"id": "1", "username": "x", "avatar": "hash1"}, "avatar": "guild1"}
    )
    assert member.avatar == "hash1" and member.guild_avatar == "guild1"
    ok("member payloads carry both the account and per-guild avatar hashes")


def test_png_style_both():
    out = Path(tempfile.mkdtemp())
    rows = [make_row(i) for i in range(3)]
    profiles = {r.discord_id: make_profile(r) for r in rows}

    counts = {}
    for index, style in enumerate(("table", "cards", "both")):
        manifest = export(
            rows, guild_name="S", guild_id="1", base_directory=out,
            formats=["png"], columns=DEFAULT_COLUMNS,
            stamp=f"2026080{index + 1}T120000Z",
            png_style=style, profiles=profiles,
        )
        counts[style] = len(list((manifest.directory / "png").glob("*.png")))

    assert counts["table"] == 1, counts
    assert counts["cards"] == 3, counts
    assert counts["both"] == 4, counts
    ok(f"png styles produce table={counts['table']}, cards={counts['cards']}, "
       f"both={counts['both']} (every card plus the table)")



async def test_export_dialog_buttons_stay_reachable():
    """The new options must not push Export and Cancel off the panel."""
    appmod.DiscordHTTP = FakeHTTP
    appmod.DiscordGateway = FakeGateway
    cfg = Config()
    cfg.token = "fake.test.token"
    app = appmod.ScannerApp(cfg)

    # a short terminal, which is where the buttons went out of reach
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.9)
        app.rows["1"] = appmod.Row(
            member=GuildMember(id="1", username="x"),
            report=MemberReport(discord_id="1"),
        )
        app.current_source = app.sources[-1]
        app.run_export()
        for _ in range(40):
            await pilot.pause(0.1)
            if isinstance(app.screen, ExportDialog):
                break
        dialog = app.screen
        assert isinstance(dialog, ExportDialog)

        panel = dialog.query_one("#panel")
        buttons = dialog.query_one("#buttons")
        confirm = dialog.query_one("#confirm", Button)

        assert buttons.region.bottom <= panel.region.bottom, (
            f"buttons at {buttons.region.bottom} fall below the panel at "
            f"{panel.region.bottom} -- out of reach"
        )
        assert confirm.region.height > 0 and confirm.region.width > 0
        ok(f"in a {app.size.height}-row terminal the buttons sit inside the "
           f"panel and are clickable")

        fields = dialog.query_one("#fields")
        assert fields.max_scroll_y > 0, (
            "the options do not overflow, so this test is not exercising "
            "the case it exists for"
        )
        ok(f"the options overflow by {fields.max_scroll_y} rows and scroll "
           f"instead of pushing the buttons away")

        # the style radios are reachable by scrolling
        fields.scroll_end(animate=False)
        await pilot.pause(0.3)
        assert dialog.query_one("#png-both", RadioButton) is not None
        ok("every option, including the new PNG styles, can be scrolled to")

        confirm.press()
        for _ in range(40):
            await pilot.pause(0.1)
            if not isinstance(app.screen, ExportDialog):
                break
        assert not isinstance(app.screen, ExportDialog)
        ok("and pressing Export still works")

    print("\nALL CARD/HTML TESTS PASSED")



# the real popout response shape, with identifiers replaced
POPOUT = {
    "user": {
        "id": "543787937737867264", "username": "friendpoog",
        "global_name": "MightyBity", "avatar": "a_9925413e75b475bde63c8abd07a718bf",
        "banner": "a_7511a2c4fef01f1b5fd34f7c758e78d2",
        "accent_color": 14857507, "bio": "**GeT OuT oF mE SwOmP!**\n19",
    },
    "user_profile": {
        "bio": "**GeT OuT oF mE SwOmP!**\n19", "accent_color": 14857507,
        "pronouns": "He/Him", "banner": "a_7511a2c4fef01f1b5fd34f7c758e78d2",
    },
    "badges": [
        {"id": "hypesquad_house_2", "description": "HypeSquad Brilliance"},
        {"id": "guild_booster_lvl1", "description": "Server boosting since 7/26/26"},
    ],
    "connected_accounts": [
        {"type": "steam", "name": "Mightybity", "verified": True},
        {"type": "spotify", "name": "GucciFlipFlops", "verified": True},
        {"type": "epicgames", "name": "Mightybity", "verified": False},
    ],
    "mutual_guilds": [{"id": "1"}, {"id": "2"}, {"id": "3"}, {"id": "4"}],
    "premium_since": "2025-10-07T17:05:13.216000+00:00",
    "guild_member": {
        "roles": ["1", "2"], "joined_at": "2026-04-16T15:08:07.535000+00:00",
        "nick": None,
        "communication_disabled_until": "2026-06-25T14:01:54.851000+00:00",
    },
}


def test_super_properties_shared():
    """HTTP and the gateway must describe the client identically."""
    import base64
    import json as _json
    from rsb.discord.http import super_properties, super_properties_header

    blob = _json.loads(base64.b64decode(super_properties_header()))
    assert blob == super_properties()
    ok("x-super-properties decodes to the same descriptor the code builds")

    for key in ("os", "browser", "client_build_number", "browser_user_agent"):
        assert key in blob, key
    assert isinstance(blob["client_build_number"], int)
    ok(f"it carries what Discord looks for (build {blob['client_build_number']})")

    from rsb.discord.http import DiscordHTTP

    client = DiscordHTTP("fake.test.token")
    headers = client._http.headers
    assert "x-super-properties" in headers
    assert headers["x-super-properties"] == super_properties_header()
    ok("every HTTP request carries it -- its absence is what returned 403")
    assert "discord/1.0.144" in headers["user-agent"]
    ok("and the user agent matches the client it claims to be")


async def test_popout_profile_is_parsed():
    from rsb.discord.http import DiscordHTTP

    captured = {}

    class Popout(DiscordHTTP):
        def __init__(self):
            pass

        async def _get(self, path, **params):
            captured["path"] = path
            captured["params"] = params
            return POPOUT

    data = await Popout().user("543787937737867264", guild_id="1147089171723321454")
    assert captured["path"].endswith("/profile"), captured["path"]
    assert captured["params"]["guild_id"] == "1147089171723321454"
    assert captured["params"]["type"] == "popout"
    ok(f"the popout route is used, scoped to the guild: {captured['params']}")

    profiles = await fetch_profiles(
        type("S", (), {"user": staticmethod(lambda uid, guild_id=None: None)})(),
        [], with_images=False,
    )
    assert profiles == {}

    class Stub:
        async def user(self, user_id, guild_id=None):
            return data

    got = await fetch_profiles(
        Stub(), ["543787937737867264"], with_images=False,
        guild_id="1147089171723321454",
    )
    p = got["543787937737867264"]

    assert p.pronouns == "He/Him"
    assert p.avatar_url and p.avatar_url.endswith(".gif?size=160"), p.avatar_url
    ok("an animated avatar is requested as a .gif, not a broken .png")
    assert p.banner_url and ".gif" in p.banner_url
    ok("same for an animated banner")

    assert [b[1] for b in p.badges] == [
        "HypeSquad Brilliance", "Server boosting since 7/26/26"
    ]
    ok(f"badges parsed: {[b[1] for b in p.badges]}")

    assert ("steam", "Mightybity", True) in p.connections
    assert ("epicgames", "Mightybity", False) in p.connections
    ok(f"{len(p.connections)} linked accounts, verified flag kept")

    assert p.mutual_guilds == 4 and p.has_nitro and p.premium_since == "2025-10-07"
    ok(f"{p.mutual_guilds} mutual servers, Nitro since {p.premium_since}")

    assert p.guild_joined_at == "2026-04-16"
    assert p.timed_out_until == "2026-06-25"
    ok(f"guild membership: joined {p.guild_joined_at}, timed out until "
       f"{p.timed_out_until}")

    # and it all reaches the rendered output
    row = make_row(0)
    row.discord_id = "543787937737867264"
    card = render_card(row, p)
    assert card.width == CARD_W
    ok("a card renders with the extra context")

    out = Path(tempfile.mkdtemp())
    body = render_html(
        [row], out, "x", DEFAULT_COLUMNS, profiles={row.discord_id: p}
    )[0].read_text(encoding="utf-8")
    for expected in ("He/Him", "HypeSquad Brilliance", "Mightybity",
                     "4 mutual server", "timed out"):
        assert expected in body, expected
    ok("and the HTML shows pronouns, badges, links, mutuals and the timeout")

if __name__ == "__main__":
    asyncio.run(main())
