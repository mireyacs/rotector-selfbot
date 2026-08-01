"""Friends, friend requests and group DMs as scannable sources.

Discord is stubbed; the Rotector half is live, so verdicts are real. Discord id
"1" is Confirmed in Rotector, which is what makes the seeded friend a THREAT.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rsb.tui.app as appmod
from rsb.config import Config
from rsb.discord.http import (
    BLOCKED,
    FRIEND,
    GROUP_DM_CHANNEL,
    DM_CHANNEL,
    INCOMING_REQUEST,
    OUTGOING_REQUEST,
    Guild,
    PrivateChannel,
    Relationship,
)
from rsb.sources import (
    KIND_FRIENDS,
    KIND_GROUP,
    KIND_GUILD,
    KIND_REQUESTS,
    build_sources,
)
from rsb.tui.dialogs import ModerationDialog
from rsb.verdict import Verdict
from textual.widgets import Button

ok = lambda m: print(f"[ok] {m}")


def rel(uid, type_, name=None):
    return Relationship(
        user_id=str(uid), username=name or f"user{uid}", global_name=None,
        discriminator="0", nickname=None, type=type_,
    )


RELATIONSHIPS = [
    rel(1, FRIEND, "flaggedfriend"),
    rel(900000000000000001, FRIEND),
    rel(900000000000000002, FRIEND),
    rel(2, INCOMING_REQUEST, "pending"),
    rel(900000000000000003, BLOCKED),
    rel(900000000000000004, OUTGOING_REQUEST),
]

CHANNELS = [
    PrivateChannel(
        id="g1", type=GROUP_DM_CHANNEL, name="Squad", owner_id="9",
        recipients=[
            {"id": "3", "username": "gm1"},
            {"id": "900000000000000005", "username": "gm2"},
        ],
    ),
    PrivateChannel(
        id="g2", type=GROUP_DM_CHANNEL, name=None, owner_id="9",
        recipients=[{"id": "900000000000000006", "username": "solo"}],
    ),
    PrivateChannel(
        id="d1", type=DM_CHANNEL, name=None, owner_id=None,
        recipients=[{"id": "900000000000000007", "username": "dm"}],
    ),
]


def test_build_sources():
    guilds = [Guild(id="111", name="Srv", owner=False, permissions=0,
                    member_count=50, presence_count=5)]
    sources = build_sources(guilds, RELATIONSHIPS, CHANNELS)
    kinds = [s.kind for s in sources]

    assert kinds[0] == KIND_REQUESTS, kinds
    ok(f"source order puts friend requests first: {kinds}")

    friends = next(s for s in sources if s.kind == KIND_FRIENDS)
    assert friends.member_count == 3, friends.member_count
    assert {m.id for m in friends.members} == {
        "1", "900000000000000001", "900000000000000002"
    }
    ok(f"friends source holds {friends.member_count} friends; blocked and "
       f"outgoing requests excluded")

    requests = next(s for s in sources if s.kind == KIND_REQUESTS)
    assert [m.id for m in requests.members] == ["2"]
    ok("incoming requests are their own source; outgoing ones are not listed")

    groups = [s for s in sources if s.kind == KIND_GROUP]
    assert len(groups) == 2, f"{len(groups)} groups (1:1 DMs must not appear)"
    named = next(g for g in groups if g.name == "Squad")
    unnamed = next(g for g in groups if g.name != "Squad")
    assert named.member_count == 2
    assert unnamed.name == "solo", unnamed.name
    ok(f"group DMs listed ({[g.name for g in groups]}); unnamed ones "
       f"fall back to recipient names")

    assert all(s.is_complete for s in sources if s.kind != KIND_GUILD)
    assert not next(s for s in sources if s.kind == KIND_GUILD).is_complete
    ok("friends/requests/groups report complete coverage; guilds do not")

    assert build_sources(guilds, [], []) == build_sources(guilds)
    assert len(build_sources(guilds)) == 1
    ok("an account with no friends or group DMs still lists its servers")


class FakeHTTP:
    def __init__(self, token, **kw):
        self.removed = []
        self.blocked = []
        self.group_removed = []

    async def me(self): return {"username": "t", "global_name": "T", "id": "9"}
    async def guilds(self):
        return [Guild(id="111", name="Srv", owner=False, permissions=0,
                      member_count=2, presence_count=1)]
    async def relationships(self): return list(RELATIONSHIPS)
    async def private_channels(self): return list(CHANNELS)
    async def channels(self, gid): return []
    async def remove_friend(self, user_id): self.removed.append(user_id)
    async def block_user(self, user_id): self.blocked.append(user_id)
    async def remove_group_recipient(self, channel_id, user_id):
        self.group_removed.append((channel_id, user_id))
    async def aclose(self): pass


class FakeGateway:
    def __init__(self, token): self.user = None
    async def connect(self, timeout=45.0): return {}
    async def fetch_members(self, *a, **kw): return {}
    async def close(self): pass


async def scan_kind(app, pilot, kind):
    """Select the first source of ``kind`` and scan it."""
    table = app.query_one("#guilds", appmod.DataTable)
    index = next(i for i, s in enumerate(app.sources) if s.kind == kind)
    table.move_cursor(row=index)
    await pilot.pause(0.3)
    app.rows.clear()
    await pilot.press("s")
    for _ in range(80):
        await pilot.pause(0.2)
        if app.rows and not app._activity:
            break
    return app.sources[index]


async def main():
    test_build_sources()
    print()

    appmod.DiscordHTTP = FakeHTTP
    appmod.DiscordGateway = FakeGateway

    cfg = Config()
    cfg.token = "fake"
    app = appmod.ScannerApp(cfg)

    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.pause(0.8)
        table = app.query_one("#guilds", appmod.DataTable)
        assert table.row_count == len(app.sources) == 5, table.row_count
        ok(f"source list shows {table.row_count} entries: "
           f"{[(s.kind, s.name) for s in app.sources]}")

        # --- scanning friends needs no gateway at all
        source = await scan_kind(app, pilot, KIND_FRIENDS)
        assert len(app.rows) == 3, f"{len(app.rows)} of 3 friends scanned"
        ok(f"scanned {len(app.rows)} friends without touching the gateway")

        threats = [r for r in app.rows.values()
                   if r.report.verdict is Verdict.THREAT]
        assert threats, "seeded flagged friend produced no THREAT"
        ok(f"{len(threats)} friend flagged as THREAT")

        app.filter_mode = appmod.FilterMode.ALL
        app._rebuild_table()
        await pilot.pause(0.3)

        # --- 'k' means unfriend here, not kick
        threat_id = threats[0].member.id
        index = app._shown.index(threat_id)
        app.query_one("#results", appmod.DataTable).move_cursor(row=index)
        await pilot.pause(0.2)

        await pilot.press("k")
        for _ in range(40):
            await pilot.pause(0.1)
            if isinstance(app.screen, ModerationDialog):
                break
        dialog = app.screen
        assert isinstance(dialog, ModerationDialog)
        assert dialog.action == "remove friend", dialog.action
        assert not dialog.wants_purge, "unfriending offered a message purge"
        assert not dialog.eligibility.needs_override, "unfriending was gated"
        assert "block instead" in dialog.note, dialog.note
        ok(f"'k' on a friend offers {dialog.action!r}, with: {dialog.note[:60]}...")

        dialog.query_one("#confirm", Button).press()
        for _ in range(40):
            await pilot.pause(0.1)
            if not isinstance(app.screen, ModerationDialog):
                break
        assert app.http.removed == [threat_id], app.http.removed
        assert not app.http.blocked
        ok(f"unfriended {threat_id} via the relationships endpoint")
        assert app.rows[threat_id].actioned == "unfriended"
        ok("row marked 'unfriended'")

        # --- 'b' blocks
        app.query_one("#results", appmod.DataTable).move_cursor(row=index)
        await pilot.pause(0.2)
        await pilot.press("b")
        for _ in range(40):
            await pilot.pause(0.1)
            if isinstance(app.screen, ModerationDialog):
                break
        dialog = app.screen
        assert dialog.action == "block", dialog.action
        dialog.query_one("#confirm", Button).press()
        for _ in range(40):
            await pilot.pause(0.1)
            if not isinstance(app.screen, ModerationDialog):
                break
        assert app.http.blocked == [threat_id], app.http.blocked
        ok(f"'b' on a friend blocks instead of banning")

        # --- group DM: members come from the recipient list
        source = await scan_kind(app, pilot, KIND_GROUP)
        assert len(app.rows) == 2, f"{len(app.rows)} of 2 group members"
        ok(f"scanned group DM {source.name!r}: {len(app.rows)} recipients")

        app.filter_mode = appmod.FilterMode.ALL
        app._rebuild_table()
        await pilot.pause(0.3)
        app.query_one("#results", appmod.DataTable).move_cursor(row=0)
        await pilot.pause(0.2)
        await pilot.press("k")
        for _ in range(40):
            await pilot.pause(0.1)
            if isinstance(app.screen, ModerationDialog):
                break
        dialog = app.screen
        assert dialog.action == "remove from group", dialog.action
        assert not dialog.eligibility.needs_override, (
            "removing someone from your own group DM should not require "
            "Rotector's endorsement"
        )
        ok(f"'k' in a group DM offers {dialog.action!r}, ungated")
        dialog.query_one("#confirm", Button).press()
        for _ in range(40):
            await pilot.pause(0.1)
            if not isinstance(app.screen, ModerationDialog):
                break
        assert app.http.group_removed and app.http.group_removed[0][0] == "g1"
        ok(f"removed from the right channel: {app.http.group_removed[0]}")

        # --- friend requests scan too
        await scan_kind(app, pilot, KIND_REQUESTS)
        assert len(app.rows) == 1
        ok("incoming friend requests scan as their own source")

    print("\nALL SOURCE TESTS PASSED")


asyncio.run(main())
