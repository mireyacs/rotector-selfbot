"""Kick/ban from the results table, including the guardrails.

Discord is stubbed (nothing is actually kicked); the Rotector half is live, so
verdicts and reasons are built from real findings.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rsb.tui.app as appmod
from rsb.config import Config
from rsb.discord.gateway import GuildMember
from rsb.discord.http import DiscordForbidden, Guild, MAX_REASON, _reason_header
from rsb.tui.dialogs import ModerationDialog
from rsb.verdict import Verdict
from textual.widgets import Button, Checkbox, Input, RadioButton

ok = lambda m: print(f"[ok] {m}")


def select_source(app, kind):
    """Move the sources cursor to the first source of ``kind``.

    Sources sit under collapsible group headers, so a source's display row is
    not its index in app.sources.
    """
    from rsb.tui.app import GROUP_KEY
    from textual.widgets import DataTable
    table = app.query_one("#guilds", DataTable)
    key = next(k for k in app._source_rows if k.startswith(f"{kind}:"))
    table.move_cursor(row=app._source_rows.index(key))
    return next(s for s in app.sources if f"{s.kind}:{s.id}" == key)


# "1" is Confirmed in Rotector; the rest have no findings
MEMBERS = {
    "1": GuildMember(id="1", username="flagged", nick="Flagged Guy"),
    "900000000000000001": GuildMember(id="900000000000000001", username="clean"),
}


class FakeHTTP:
    def __init__(self, token, **kw):
        self.kicks = []
        self.bans = []
        self.fail_with = None

    async def me(self): return {"username": "t", "global_name": "T", "id": "9"}
    async def guilds(self):
        return [Guild(id="111", name="Srv", owner=True, permissions=8,
                      member_count=len(MEMBERS), presence_count=2)]
    async def relationships(self): return []
    async def widget(self, gid): return None
    async def private_channels(self): return []
    async def channels(self, gid):
        from rsb.discord.http import Channel
        return [Channel(id="c1", name="general", type=0, position=0,
                        everyone_can_view=True)]
    async def kick(self, guild_id, user_id, reason):
        if self.fail_with:
            raise self.fail_with
        self.kicks.append((guild_id, user_id, reason))
    async def ban(self, guild_id, user_id, reason, delete_message_seconds=0):
        if self.fail_with:
            raise self.fail_with
        self.bans.append((guild_id, user_id, reason, delete_message_seconds))
    async def aclose(self): pass


class FakeGateway:
    def __init__(self, token): self.user = None
    async def connect(self, timeout=45.0): return {}
    async def fetch_members(self, gid, channels, expected=None, on_progress=None,
                            on_members=None, **kwargs):
        if on_members: on_members(list(MEMBERS.values()))
        return dict(MEMBERS)
    async def close(self): pass


async def open_dialog(app, pilot, key):
    await pilot.press(key)
    for _ in range(40):
        await pilot.pause(0.1)
        if isinstance(app.screen, ModerationDialog):
            return app.screen
    raise AssertionError(f"moderation dialog did not open for {key!r}")


async def close_dialog(app, pilot):
    for _ in range(40):
        await pilot.pause(0.1)
        if not isinstance(app.screen, ModerationDialog):
            return
    raise AssertionError("dialog never closed")


async def main():
    appmod.DiscordHTTP = FakeHTTP
    appmod.DiscordGateway = FakeGateway

    cfg = Config()
    cfg.token = "fake"
    app = appmod.ScannerApp(cfg)

    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.pause(0.8)
        select_source(app, "guild")
        await pilot.pause(0.2)
        await pilot.press("s")
        for _ in range(80):
            await pilot.pause(0.2)
            if len(app.rows) >= len(MEMBERS) and not app._activity:
                break

        app.filter_mode = appmod.FilterMode.ALL
        app._rebuild_table()
        await pilot.pause(0.3)
        table = app.query_one("#results", appmod.DataTable)
        assert table.row_count == len(MEMBERS)

        order = list(app._shown)
        threat_index = next(
            i for i, k in enumerate(order)
            if app.rows[k].report.verdict is Verdict.THREAT
        )
        clean_index = next(
            i for i, k in enumerate(order)
            if app.rows[k].report.verdict is not Verdict.THREAT
        )
        ok(f"scanned {len(app.rows)} members; "
           f"{order[threat_index]} is a THREAT, {order[clean_index]} is not")

        # --- kicking a THREAT: allowed, no override needed
        table.move_cursor(row=threat_index)
        await pilot.pause(0.2)
        dialog = await open_dialog(app, pilot, "k")
        assert not dialog.eligibility.needs_override
        assert dialog.eligibility.allowed
        preview = dialog._current_reason()
        assert "rotector.com" in preview and "Confirmed" in preview
        ok(f"kick dialog for an actionable finding, reason: {preview[:70]}...")

        dialog.query_one("#confirm", Button).press()
        await close_dialog(app, pilot)
        assert len(app.http.kicks) == 1, app.http.kicks
        guild_id, user_id, reason = app.http.kicks[0]
        assert guild_id == "111" and user_id == order[threat_index]
        assert "rotector.com" in reason and len(reason) <= MAX_REASON
        ok(f"kick issued with an attributed reason ({len(reason)} chars)")

        row = app.rows[order[threat_index]]
        assert row.actioned == "kicked"
        assert row.report.error is None, "action must not masquerade as a failed lookup"
        ok("row marked 'kicked' without corrupting the lookup result")

        # --- banning a non-actionable member: blocked by require_threat
        table.move_cursor(row=clean_index)
        await pilot.pause(0.2)
        dialog = await open_dialog(app, pilot, "b")
        assert dialog.eligibility.needs_override, "no warning on a non-actionable member"
        assert not dialog.eligibility.allowed, "require_threat should block this"
        ok(f"ban dialog warns: {dialog.eligibility.explanation[:70]}...")

        dialog.query_one("#confirm", Button).press()
        await pilot.pause(0.4)
        assert isinstance(app.screen, ModerationDialog), "closed without the override"
        assert not app.http.bans, "banned without acknowledgement"
        ok("confirming without ticking the acknowledgement is refused")

        dialog.query_one("#override", Checkbox).value = True
        dialog.query_one("#confirm", Button).press()
        await pilot.pause(0.4)
        assert isinstance(app.screen, ModerationDialog), "require_threat bypassed"
        assert not app.http.bans, "require_threat did not block the ban"
        ok("even acknowledged, require_threat=true still blocks the ban")

        dialog.dismiss(None)
        await close_dialog(app, pilot)

        # --- with require_threat off, the same action proceeds
        app.config.moderation.require_threat = False
        table.move_cursor(row=clean_index)
        await pilot.pause(0.2)
        dialog = await open_dialog(app, pilot, "b")
        assert dialog.eligibility.needs_override and dialog.eligibility.allowed
        dialog.query_one("#reason-custom", RadioButton).value = True
        dialog.query_one("#reason-text", Input).value = "Manual review: raiding"
        dialog.query_one("#override", Checkbox).value = True
        dialog.query_one("#delete-seconds", Input).value = "3600"
        dialog.query_one("#confirm", Button).press()
        await close_dialog(app, pilot)

        assert len(app.http.bans) == 1, app.http.bans
        _, banned_id, ban_reason, seconds = app.http.bans[0]
        assert banned_id == order[clean_index]
        assert ban_reason.startswith("Manual review: raiding")
        assert "rotector.com" in ban_reason, "custom reason lost its attribution"
        assert seconds == 3600
        ok(f"custom-reason ban issued: {ban_reason!r}, purge {seconds}s")

        # --- permission failure is reported, not swallowed
        app.http.fail_with = DiscordForbidden("/guilds/111/members/1: 403")
        table.move_cursor(row=threat_index)
        await pilot.pause(0.2)
        dialog = await open_dialog(app, pilot, "k")
        dialog.query_one("#confirm", Button).press()
        await close_dialog(app, pilot)
        await pilot.pause(0.4)
        assert "lacks" in app._status_text or "permission" in app._status_text
        ok(f"a 403 is explained, not swallowed: {app._status_text[:70]}...")

    # audit-log reasons must survive header encoding
    header = _reason_header("Flagged: naughty / bits & pieces\nnewline")
    assert "\n" not in header["X-Audit-Log-Reason"]
    assert "%" in header["X-Audit-Log-Reason"], "reason not percent-encoded"
    ok("audit-log reason is percent-encoded and newline-free")

    print("\nALL MODERATION TESTS PASSED")


asyncio.run(main())
